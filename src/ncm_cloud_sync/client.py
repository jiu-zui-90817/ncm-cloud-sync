"""网易云音乐云盘接口客户端。

这是整个项目里最脆弱的部分：网易云没有公开上传 API，weapi 参数与 NOS
上传流程可能随时变化。所有协议细节集中在这里，出现问题时优先检查
check / token alloc / NOS / info / pub 五个步骤的返回。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .cookies import cookie_header, ensure_defaults
from .crypto import anonymous_username, encrypt_eapi, encrypt_weapi
from .errors import ApiError, AuthError, NosUnavailableError, RateLimitError, UploadError
from .hashing import file_md5
from .metadata import display_album, display_artist, read_tags, sanitize_filename

logger = logging.getLogger(__name__)

BASE_URL = "https://music.163.com"
NOS_BUCKET = "jd-musicrep-privatecloud-audio-public"
NOS_LBS_URLS = (
    "https://wanproxy.127.net/lbs?version=1.0&bucketname=" + NOS_BUCKET,
    "http://wanproxy.127.net/lbs?version=1.0&bucketname=" + NOS_BUCKET,
)
NOS_FALLBACK_HOSTS = ("nosup-jd1.127.net", "nosup-hz1.127.net")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
    "ape": "audio/x-ape",
}

AUTH_ERROR_CODES = {301, 302}
ANON_APPVER = "3.1.17.204416"
ANON_OSVER = "Microsoft-Windows-10-Professional-build-19045-64bit"
EAPI_PC_URL = "https://interfacepc.music.163.com"
EAPI_UPLOAD_URL = "http://musicupload.netease.com"
EAPI_PUB_URL = "https://interface.music.163.com"
WHALE_ALLOC_URL = "http://musicupload.netease.com/api/whale/token/alloc"
WHALE_CHANNEL = 3
EAPI_PC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Safari/537.36 Chrome/91.0.4472.164 "
    "NeteaseMusicDesktop/3.1.29.205117"
)
# 云盘写入接口的当前可用形态（SPlayer-Next）：
# interfacepc eapi + PC 客户端 appver + eapi header 里带 NMTID。
# 0.2.14 用 interface.music.163.com + appver 3.1.17 + 不带 NMTID，
# info/v2 仍然返回 [400] 接口调用失败。
EAPI_CLOUD_APPVER = "3.1.29.205117"
EAPI_IPHONE_USER_AGENT = "NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)"
# 原版桌面客户端原生上传器（cloudmusic_util）的 eapi 头：只带
# os/appver/deviceId/requestId/clientSign 五个字段，且没有 e_r。
EAPI_NATIVE_APPVER = "3.1.42.205594"
EAPI_NATIVE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Safari/537.36 Chrome/91.0.4472.164 "
    "NeteaseMusicDesktop/3.1.42.205594"
)


@dataclass
class UploadResult:
    uploaded: bool
    song_id: int | None = None
    reason: str = ""
    cloud_name: str | None = None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_nos_host(value: Any) -> str:
    """把 LBS 返回的节点（可能带 http:// 或路径）规范化成 host。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    return (parsed.netloc or parsed.path).strip().strip("/")


def parse_nos_host(body: Any) -> str:
    """兼容 LBS 的两种返回：顶层 upload 字符串列表，或 data.upload 对象列表。"""
    if not isinstance(body, dict):
        return ""
    containers: list[Any] = [body]
    data = body.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for key in ("upload", "hosts", "nodes"):
            group = container.get(key)
            items = group if isinstance(group, (list, tuple)) else [group]
            for item in items:
                if isinstance(item, dict):
                    candidate = item.get("host") or item.get("url") or item.get("domain")
                else:
                    candidate = item
                host = _normalize_nos_host(candidate)
                if host:
                    return host
    return ""


class CloudClient:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        timeout: float = 30.0,
        retries: int = 3,
        rate_limit: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookies = ensure_defaults(dict(cookies))
        self.retries = max(0, retries)
        self.rate_limit = max(0.0, rate_limit)
        self._nos_host: str | None = None
        self._nos_host_at = 0.0
        self._device_ip: str | None = None
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": BASE_URL,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CloudClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- HTTP / weapi -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Referer": BASE_URL}
        if self._device_ip:
            headers["X-Real-IP"] = self._device_ip
            headers["X-Forwarded-For"] = self._device_ip
        if self.cookies:
            headers["Cookie"] = cookie_header(self.cookies)
        return headers

    @staticmethod
    def _check_body(body: Any, url: str) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError("接口返回了非 JSON 对象", url=url)
        code = body.get("code", 200)
        code_int = _as_int(code, -1)
        if code_int in AUTH_ERROR_CODES:
            raise AuthError(
                f"登录状态无效（code={code}），请重新登录或更新 MUSIC_U"
            )
        if code_int in {405, 429}:
            message = body.get("msg") or body.get("message") or "操作频繁"
            raise RateLimitError(str(message), code=code, url=url)
        if code_int not in {200, -1}:
            message = body.get("msg") or body.get("message") or "接口调用失败"
            raise ApiError(str(message), code=code, url=url)
        return body

    def _throttle(self) -> None:
        """让所有 weapi 请求共享一个最小间隔，避免短时间内触发风控。"""
        if self.rate_limit <= 0:
            return
        with self._throttle_lock:
            now = time.monotonic()
            wait = self.rate_limit - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_request_at = now

    def _post_json(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        check: bool = True,
        cookie_sink: list[str] | None = None,
    ) -> dict[str, Any]:
        merged = self._headers()
        if headers:
            merged.update(headers)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                self._throttle()
                response = self._http.post(url, data=data, headers=merged)
                self._absorb_cookies(response)
                if cookie_sink is not None:
                    cookie_sink.extend(response.headers.get_list("set-cookie"))
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"服务端错误 {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError as exc:
                    raise UploadError(f"接口返回内容无法解析: {url}") from exc
                return self._check_body(body, url) if check else body
            except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = min(2 ** attempt, 8)
                logger.warning("请求失败，%s 秒后重试: %s", delay, exc)
                time.sleep(delay)
        raise UploadError(f"请求失败: {url}: {last_error}") from last_error

    def _absorb_cookies(self, response: httpx.Response) -> None:
        # 不能遍历 response.cookies：网易云同一个名字会在不同 Path 下重复
        # 下发（例如 MUSIC_A_T），httpx 会抛 CookieConflict。直接解析原始
        # Set-Cookie 行，保留最后一个非空值。
        self._absorb_cookie_lines(response.headers.get_list("set-cookie"))

    def _absorb_cookie_lines(self, lines: list[str]) -> None:
        """吸收原始 Set-Cookie 行，保留最后一个非空值。"""
        for line in lines:
            pair = line.split(";", 1)[0].strip()
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                self.cookies[key] = value

    def _weapi(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        host: str = BASE_URL,
        check: bool = True,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # 网易云网页端约定：加密的 weapi 请求统一走 /weapi/ 路径。
        # NCMApi 在发送前会把 /api/xxx 改写成 /weapi/xxx；直接向
        # /api/xxx 发送 params/encSecKey 会被网关当成明文参数校验，
        # 返回 [400] 参数错误。
        if path.startswith("/api/"):
            path = "/weapi/" + path[len("/api/"):]
        url = host + path
        form = encrypt_weapi(payload or {})
        form["csrf_token"] = self.cookies.get("__csrf", "")
        request_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
        }
        if headers:
            request_headers.update(headers)
        return self._post_json(
            url,
            form,
            headers=request_headers,
            check=check,
        )

    def _eapi_pc(
        self,
        uri: str,
        payload: dict[str, Any],
        *,
        check: bool = True,
    ) -> dict[str, Any]:
        """用 PC 客户端身份调用 interfacepc eapi。

        2026-10 起网页 weapi 的扫码 unikey 会被风控（-462）；当前可用的
        流程是 eapi + PC 头 + type=3，参考 api-enhanced / Ncrust。
        """
        device_id = self._ensure_anonymous_device()
        now_ms = int(time.time() * 1000)
        header = {
            "osver": ANON_OSVER,
            "deviceId": device_id,
            "os": "pc",
            "appver": ANON_APPVER,
            "versioncode": "140",
            "mobilename": "",
            "buildver": str(now_ms)[:10],
            "resolution": "1920x1080",
            "__csrf": "",
            "channel": "netease",
            "requestId": f"{now_ms}_{secrets.randbelow(10000):04d}",
        }
        body = dict(payload)
        body["e_r"] = False
        body["header"] = header
        form = encrypt_eapi(uri, body)
        cookie = "; ".join(
            f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
            for key, value in header.items()
        )
        lines: list[str] = []
        result = self._post_json(
            EAPI_PC_URL + "/eapi/" + uri[len("/api/"):],
            form,
            headers={
                "User-Agent": EAPI_PC_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie,
            },
            check=check,
            cookie_sink=lines,
        )
        self._absorb_cookie_lines(lines)
        return result

    def _eapi_api(
        self,
        uri: str,
        payload: dict[str, Any],
        *,
        check: bool = True,
        base_url: str = EAPI_PC_URL,
    ) -> dict[str, Any]:
        """用 PC 客户端 eapi 调用云盘写入接口。

        2026-11 起云盘写入必须走 eapi。查重、pub/v2 使用
        interfacepc/interface.music.163.com；info/v2 必须使用上传域名
        musicupload.netease.com，并且 Cookie 要带完整登录 Cookie。
        另参考 HyPlayer：info 的 resourceId/songid 传字符串、bitrate 传
        整数，字节已上传时还要带 objectKey={bucket}/{objectKey}。
        """
        device_id = self._ensure_anonymous_device()
        now_ms = int(time.time() * 1000)
        header = {
            "osver": str(self.cookies.get("osver") or ANON_OSVER),
            "deviceId": device_id,
            "os": str(self.cookies.get("os") or "pc"),
            "appver": EAPI_CLOUD_APPVER,
            "versioncode": str(self.cookies.get("versioncode") or "140"),
            "mobilename": str(self.cookies.get("mobilename") or ""),
            "buildver": str(self.cookies.get("buildver") or int(now_ms / 1000)),
            "resolution": str(self.cookies.get("resolution") or "1920x1080"),
            "__csrf": str(self.cookies.get("__csrf") or ""),
            "channel": str(self.cookies.get("channel") or "netease"),
            "requestId": f"{now_ms}_{secrets.randbelow(1000):04d}",
        }
        for name in ("MUSIC_U", "MUSIC_A"):
            value = self.cookies.get(name)
            if value:
                header[name] = str(value)
        body = dict(payload)
        body["e_r"] = False
        body["header"] = header
        form = encrypt_eapi(uri, body)
        # 完整登录 Cookie 才是 eapi 的鉴权来源；body.header 只放设备字段。
        cookie = cookie_header(self.cookies)
        lines: list[str] = []
        result = self._post_json(
            base_url + "/eapi/" + uri[len("/api/"):],
            form,
            headers={
                "User-Agent": EAPI_IPHONE_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie,
            },
            check=check,
            cookie_sink=lines,
        )
        self._absorb_cookie_lines(lines)
        return result

    def _eapi_native(
        self,
        uri: str,
        payload: dict[str, Any],
        *,
        check: bool = True,
        base_url: str = EAPI_UPLOAD_URL,
    ) -> dict[str, Any]:
        """模拟原版客户端原生上传器的 eapi 请求。

        cloudmusic_util 的 body 只有业务字段 + header，header 仅
        os/appver/deviceId/requestId/clientSign，没有 e_r，也没有
        网页端那套 osver/versioncode/channel 字段。
        """
        device_id = self._ensure_anonymous_device()
        body = dict(payload)
        body["header"] = {
            "os": "pc",
            "appver": EAPI_NATIVE_APPVER,
            "deviceId": device_id,
            "requestId": secrets.token_hex(4).upper(),
            "clientSign": str(self.cookies.get("clientSign") or ""),
        }
        form = encrypt_eapi(uri, body)
        cookie = cookie_header(self.cookies)
        lines: list[str] = []
        result = self._post_json(
            base_url + "/eapi/" + uri[len("/api/"):],
            form,
            headers={
                "User-Agent": EAPI_NATIVE_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie,
            },
            check=check,
            cookie_sink=lines,
        )
        self._absorb_cookie_lines(lines)
        return result

    # -- 匿名设备上下文 ---------------------------------------------------
    @staticmethod
    def _gen_device_id() -> str:
        return "".join(secrets.choice("0123456789ABCDEF") for _ in range(52))

    @staticmethod
    def _ip_for_device(device_id: str) -> str:
        try:
            seed = int(device_id[:8], 16)
        except (TypeError, ValueError):
            seed = 0
        return (
            f"116.{25 + seed % 70}."
            f"{1 + (seed // 70) % 255}.{1 + (seed // (70 * 255)) % 255}"
        )

    @staticmethod
    def _gen_client_sign(device_id: str) -> str:
        """按 open-orpheus 的方式生成原版客户端 clientSign。

        原版 getADDeviceID 返回 ``MAC@@@十六进制硬件串@@@@@@sha256``，
        服务端只校验格式与稳定性，因此用 deviceId 派生一个固定 MAC。
        """
        seed = hashlib.sha256(("ncm-cloud-sync:" + device_id).encode("utf-8")).digest()
        mac = ":".join(f"{byte:02X}" for byte in seed[:6])
        mac = f"{int(mac[:2], 16) & 0xFC:02X}" + mac[2:]
        legacy = hashlib.sha256(f"{device_id}:{mac}".encode("utf-8")).hexdigest().upper()[:32]
        legacy = "_".join(legacy[index:index + 4] for index in range(0, 32, 4))
        encoded = legacy.encode("utf-8").hex().upper()
        sign_str = f"{mac}@@@{encoded}"
        digest = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()
        return f"{sign_str}@@@@@@{digest}"

    def _ensure_anonymous_device(self) -> str:
        """注入匿名设备 Cookie，供扫码接口校验。

        网易云扫码接口并不是完全公开的：没有 deviceId / _ntes_nuid 等匿名
        上下文时，二维码能生成，但手机确认后会提示“数据加载失败”。做法与
        NEMbox / api-enhanced 一致，且同一 CloudClient 内保持不变。
        """
        device_id = str(self.cookies.get("deviceId") or "")
        if len(device_id) != 52 or any(ch not in "0123456789ABCDEF" for ch in device_id):
            device_id = self._gen_device_id()
        self.cookies["deviceId"] = device_id
        client_sign = str(self.cookies.get("clientSign") or "")
        if not client_sign:
            client_sign = self._gen_client_sign(device_id)
        self.cookies["clientSign"] = client_sign

        nuid = self.cookies.get("_ntes_nuid") or secrets.token_hex(32)
        now_ms = int(time.time() * 1000)
        defaults = {
            "os": "pc",
            "appver": ANON_APPVER,
            "osver": ANON_OSVER,
            "channel": "netease",
            "__remember_me": "true",
            "_ntes_nuid": nuid,
            "_ntes_nnid": f"{nuid},{now_ms}",
            "WNMCID": f"{''.join(secrets.choice(string.ascii_lowercase) for _ in range(6))}.{now_ms}.01.0",
            "WEVNSM": "1.0.0",
        }
        for name, value in defaults.items():
            self.cookies.setdefault(name, value)
        # 扫码接口按桌面客户端版本校验，覆盖 ensure_defaults() 的旧版本号。
        self.cookies["appver"] = ANON_APPVER
        self._device_ip = self._ip_for_device(device_id)
        logger.debug("匿名设备: deviceId=%s ip=%s", device_id, self._device_ip)
        return device_id

    # -- 账号 -------------------------------------------------------------
    def login_status(self) -> dict[str, Any]:
        body = self._weapi("/weapi/w/nuser/account/get", {})
        profile = body.get("profile") or {}
        if not profile and not body.get("account"):
            raise AuthError("未登录：MUSIC_U 缺失或已失效，请重新登录")
        return profile

    def is_logged_in(self) -> bool:
        try:
            self.login_status()
        except AuthError:
            return False
        return True

    def user_id(self) -> int | None:
        try:
            profile = self.login_status()
        except AuthError:
            return None
        return _as_int(profile.get("userId"), 0) or None

    # -- 扫码 / 手机号登录 ------------------------------------------------
    def qr_unikey(self) -> str:
        # 2026-10 起网页 weapi 扫码会被风控（-462），改走 eapi PC 身份 + type=3。
        body = self._eapi_pc("/api/login/qrcode/unikey", {"type": 3})
        key = body.get("unikey") or body.get("key")
        if not key:
            raise AuthError(f"获取二维码 key 失败: {body}")
        return str(key)

    def qr_status(self, key: str) -> dict[str, Any]:
        # 800/801/802/803/8821 都是业务状态，不走 code==200 校验。
        return self._eapi_pc(
            "/api/login/qrcode/client/login",
            {"key": key, "type": 3},
            check=False,
        )

    def phone_login(self, phone: str, password: str, countrycode: str = "86") -> dict[str, Any]:
        return self._weapi(
            "/weapi/login/cellphone",
            {
                "phone": phone,
                "countrycode": countrycode,
                "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
                "rememberLogin": "true",
            },
        )

    # -- 云盘 -------------------------------------------------------------
    def cloud_songs(self, page_size: int = 100) -> list[dict[str, Any]]:
        """拉取云盘全部歌曲（用于本地对账 / 去重保险）。"""
        songs: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = self._weapi(
                "/api/v1/cloud/get",
                {"limit": page_size, "offset": offset},
            )
            data = body.get("data") or []
            if not isinstance(data, list):
                raise ApiError("云盘列表返回格式异常", url=BASE_URL + "/api/v1/cloud/get")
            songs.extend(item for item in data if isinstance(item, dict))
            count = _as_int(body.get("count"), 0)
            offset += len(data)
            if not data or len(data) < page_size:
                break
            if count and offset >= count:
                break
        return songs

    def cloud_md5_set(self) -> set[str]:
        return {
            str(song.get("md5", "")).lower()
            for song in self.cloud_songs()
            if song.get("md5")
        }

    # -- 上传流程 ---------------------------------------------------------
    def check_upload(self, md5: str, size: int, ext: str) -> dict[str, Any]:
        payload = {
            "bitrate": "999000",
            # 官方网页端查重固定传空 ext，类型由服务端按 md5/大小判断。
            "ext": "",
            "length": size,
            "md5": md5,
            "songId": "0",
            "version": 1,
        }
        try:
            # 查重也必须走 eapi：服务端只对 eapi 请求下发 NMTID，
            # 后续 info/v2 / pub/v2 没有 NMTID 会直接返回 400 接口调用失败。
            body = self._eapi_api("/api/cloud/upload/check", payload)
        except ApiError as exc:
            # 400 参数错误通常意味着接口路径/字段又变了，把请求形状写进日志。
            logger.error(
                "云盘查重失败: size=%s ext=%s path=/api/cloud/upload/check error=%s",
                size,
                ext,
                exc,
            )
            raise
        raw_song_id = body.get("songId")
        return {
            "need_upload": bool(body.get("needUpload")),
            # 保留 0：新文件查重会返回 songId=0，官方客户端仍会把它作为
            # songid="0" 提交；只有响应里完全没有 songId 时才省略。
            "song_id": _as_int(raw_song_id, 0) if raw_song_id is not None else None,
            # 原版前端把 check 响应里的 songId 原样提交，所以保留原始类型。
            "song_id_raw": raw_song_id,
            "raw": body,
        }

    def whale_alloc(self, md5: str, filename: str) -> dict[str, Any]:
        """申请新版鲸鱼上传 token（HyPlayer 的 musicupload 流程）。

        老 /weapi/nos/token/alloc 返回的 resourceId 在 info/v2 已不被接受；
        新版必须先拿鲸鱼 token，上传字节后再把同一个 resourceId 和
        objectKey 提交给 musicupload 的 info/v2。
        """
        biz_key = f"{secrets.randbelow(61439) + 4096:x}{secrets.randbelow(65535):x}"
        payload = {
            "channel": str(WHALE_CHANNEL),
            "filename": filename,
            "md5": md5,
            "type": "audio",
            "bucket": NOS_BUCKET,
            "bizKey": biz_key,
        }
        body = self._post_json(
            WHALE_ALLOC_URL,
            payload,
            check=False,
        )
        code = _as_int(body.get("code"), -1)
        message = str(body.get("message") or body.get("msg") or body)
        if code in {405, 429}:
            raise RateLimitError(message, code=code, url=WHALE_ALLOC_URL)
        if code not in (200, -1):
            raise UploadError(f"鲸鱼上传 token 申请失败: code={code} {message}")
        result = body.get("data")
        if not isinstance(result, dict):
            result = body.get("result")
        if not isinstance(result, dict):
            raise UploadError(f"鲸鱼上传 token 响应异常: {body}")
        for key in ("objectKey", "token", "resourceId"):
            if not result.get(key):
                raise UploadError(f"鲸鱼上传 token 缺少 {key}: {body}")
        return result

    def alloc_token(
        self,
        md5: str,
        name: str,
        ext: str,
        *,
        bucket: str = "",
    ) -> dict[str, Any]:
        filename = sanitize_filename(name).replace(" ", "").replace(".", "_")
        body = self._weapi(
            "/weapi/nos/token/alloc",
            {
                "bucket": bucket,
                "ext": ext,
                "filename": filename,
                "local": False,
                "nos_product": 3,
                "type": "audio",
                "md5": md5,
            },
        )
        result = body.get("result")
        if not isinstance(result, dict):
            raise UploadError(f"获取上传 token 失败: {body}")
        if bucket:
            if not result.get("objectKey") or not result.get("token"):
                raise UploadError(f"获取 NOS 上传 token 失败: {body}")
        elif not result.get("resourceId"):
            raise UploadError(f"获取 resourceId 失败: {body}")
        return result

    def _get_nos_host(self) -> str:
        now = time.time()
        if self._nos_host and now - self._nos_host_at < 300:
            return self._nos_host
        headers = {"User-Agent": USER_AGENT, "Referer": BASE_URL}
        failures: list[str] = []
        for url in NOS_LBS_URLS:
            try:
                response = self._http.get(url, headers=headers, timeout=15.0)
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{url}: {type(exc).__name__}: {exc}")
                logger.warning("获取 NOS 上传节点失败，尝试下一个地址: %s (%s)", url, exc)
                continue
            host = parse_nos_host(body)
            if host:
                self._nos_host = host
                self._nos_host_at = now
                logger.info("NOS 上传节点: %s", host)
                return host
            failures.append(f"{url}: 响应中没有 upload 节点: {body}")
            logger.warning("NOS 上传节点响应异常: %s", body)
        for host in NOS_FALLBACK_HOSTS:
            if self._nos_host_reachable(host):
                logger.warning("LBS 不可用，改用固定 NOS 上传节点: %s", host)
                self._nos_host = host
                self._nos_host_at = now
                return host
            failures.append(f"{host}: 连接失败")
        raise NosUnavailableError("无法获取网易云上传节点；" + "；".join(failures))

    def _nos_host_reachable(self, host: str) -> bool:
        try:
            self._http.head(
                f"http://{host}/",
                headers={"User-Agent": USER_AGENT, "Referer": BASE_URL},
                timeout=8.0,
            )
            return True
        except httpx.HTTPError as exc:
            logger.warning("固定 NOS 节点不可用: %s (%s)", host, exc)
            return False

    def upload_to_nos(self, token: dict[str, Any], path: str | Path, md5: str, ext: str) -> None:
        object_key = str(token["objectKey"])
        url = (
            f"http://{self._get_nos_host()}/{NOS_BUCKET}/"
            f"{quote(object_key, safe='')}?offset=0&complete=true&version=1.0"
        )
        with open(path, "rb") as fh:
            content = fh.read()
        headers = {
            "x-nos-token": str(token["token"]),
            "Content-MD5": md5,
            "Content-Type": CONTENT_TYPES.get(ext, "application/octet-stream"),
            "Content-Length": str(len(content)),
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._http.post(url, content=content, headers=headers)
                if response.status_code >= 300:
                    raise UploadError(
                        f"NOS 上传失败 HTTP {response.status_code}: {response.text[:200]}"
                    )
                return
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(2 ** attempt, 8))
        raise UploadError(f"NOS 上传失败: {last_error}") from last_error

    def finish_upload(
        self,
        *,
        md5: str,
        song_id: int | str | None,
        filename: str,
        song: str,
        album: str,
        artist: str,
        resource_id: int | str,
        object_key: str | None = None,
        bitrate: int | str = 999000,
    ) -> int:
        payload: dict[str, Any] = {
            "album": album,
            "artist": artist,
            "bitrate": _as_int(bitrate, 999000),
            "filename": filename,
            "md5": md5,
            "song": song,
            # 原版前端把查重返回的 songId 原样提交；新文件是数字 0。
            "songid": song_id if song_id is not None else 0,
        }
        if object_key:
            # 新流程要求 objectKey 为 {bucket}/{objectKey}，且与鲸鱼 token 对应；
            # needUpload=false 时原版客户端不提交 objectKey/resourceId。
            payload["objectKey"] = object_key
            payload["resourceId"] = _as_int(resource_id, 0)
        logger.debug("提交云盘信息: %s", payload)
        try:
            body = self._eapi_native(
                "/api/upload/cloud/info/v2",
                payload,
                base_url=EAPI_UPLOAD_URL,
            )
        except ApiError as exc:
            logger.error(
                "提交云盘信息失败: filename=%r songid=%r resourceId=%r host=%s appver=%s cookie=%s error=%s",
                filename,
                payload.get("songid"),
                payload.get("resourceId"),
                EAPI_UPLOAD_URL,
                EAPI_NATIVE_APPVER,
                "yes" if self.cookies.get("MUSIC_U") else "no",
                exc,
            )
            raise
        new_id = _as_int(body.get("songId") or body.get("songid") or song_id, 0)
        if new_id <= 0:
            raise UploadError(f"提交云盘信息失败: {body}")
        return new_id

    def publish(self, song_id: int) -> None:
        # pub/v2 仍走 interface eapi，songid 用字符串；201 表示重复发布，
        # 对同步来说同样是成功，不能当成失败。
        body = self._eapi_api(
            "/api/cloud/pub/v2",
            {"songid": str(song_id)},
            base_url=EAPI_PUB_URL,
            check=False,
        )
        code = _as_int(body.get("code"), 200)
        if code not in (200, 201):
            message = str(body.get("message") or body.get("msg") or "接口调用失败")
            raise ApiError(message, code=code, url=EAPI_PUB_URL + "/eapi/cloud/pub/v2")

    def upload(
        self,
        path: str | Path,
        *,
        md5: str | None = None,
        dry_run: bool = False,
    ) -> UploadResult:
        """完整上传一首歌；调用方负责本地状态与并发控制。"""
        path = Path(path)
        ext = path.suffix.lower().lstrip(".")
        size = path.stat().st_size
        md5 = md5 or file_md5(path)
        check = self.check_upload(md5, size, ext)
        logger.info(
            "云盘查重结果: md5=%s size=%s ext=%s needUpload=%s songId=%s",
            md5,
            size,
            ext,
            check["need_upload"],
            check["song_id"],
        )
        logger.debug("云盘查重响应: %s", check.get("raw"))
        if dry_run:
            return UploadResult(False, reason="dry_run")

        tags = read_tags(path)
        name = sanitize_filename(tags.get("title") or path.stem)
        token: dict[str, Any] | None = None
        if check["need_upload"]:
            # 新版流程先申请鲸鱼 token，再上传字节并用同一个 token 的
            # resourceId + objectKey 提交 info/v2。旧的 /weapi/nos/token/alloc
            # 已经不再被 info/v2 接受，而且会触发 405 限流，因此不再回退。
            token = self.whale_alloc(md5, path.name)
            logger.info(
                "鲸鱼上传 token: resourceId=%s objectKey=%s",
                token.get("resourceId"),
                token.get("objectKey"),
            )
            self.upload_to_nos(token, path, md5, ext)
        else:
            # needUpload=false 说明文件字节已在网易云对象存储；原版客户端
            # 不再申请 token，直接补建云盘条目（info/v2 + pub/v2）。
            logger.info("文件内容已在网易云对象存储，补建云盘条目: %s", path)
        object_key = None
        resource_id = 0
        if token:
            resource_id = token.get("resourceId", 0)
            if token.get("objectKey"):
                object_key = f"{NOS_BUCKET}/{token['objectKey']}"
        song_id = self.finish_upload(
            md5=md5,
            song_id=check.get("song_id_raw", check["song_id"]),
            filename=path.name,
            song=name,
            # 原版客户端直接提交标签里的 album/artist，缺失时为空字符串。
            album=str(tags.get("album") or ""),
            artist=str(tags.get("artist") or ""),
            resource_id=resource_id,
            object_key=object_key,
        )
        self.publish(song_id)
        return UploadResult(True, song_id=song_id, reason="uploaded", cloud_name=name)

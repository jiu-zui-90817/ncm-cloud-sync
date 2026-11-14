"""网易云音乐云盘接口客户端。

这是整个项目里最脆弱的部分：网易云没有公开上传 API，weapi 参数与 NOS
上传流程可能随时变化。所有协议细节集中在这里，出现问题时优先检查
check / token alloc / NOS / info / pub 五个步骤的返回。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .cookies import cookie_header, ensure_defaults
from .crypto import encrypt_weapi
from .errors import ApiError, AuthError, UploadError
from .hashing import file_md5
from .metadata import display_album, display_artist, read_tags, sanitize_filename

logger = logging.getLogger(__name__)

BASE_URL = "https://music.163.com"
INTERFACE_URL = "https://interface.music.163.com"
NOS_BUCKET = "jd-musicrep-privatecloud-audio-public"
NOS_LBS_URL = "https://wanproxy.127.net/lbs?version=1.0&bucketname=" + NOS_BUCKET
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


class CloudClient:
    def __init__(
        self,
        cookies: dict[str, str],
        *,
        timeout: float = 30.0,
        retries: int = 3,
        rate_limit: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookies = ensure_defaults(dict(cookies))
        self.retries = max(0, retries)
        self.rate_limit = max(0.0, rate_limit)
        self._nos_host: str | None = None
        self._nos_host_at = 0.0
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
        if code_int not in {200, -1}:
            message = body.get("msg") or body.get("message") or "接口调用失败"
            raise ApiError(str(message), code=code, url=url)
        return body

    def _post_json(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        check: bool = True,
    ) -> dict[str, Any]:
        merged = self._headers()
        if headers:
            merged.update(headers)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._http.post(url, data=data, headers=merged)
                self._absorb_cookies(response)
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
        for key, value in response.cookies.items():
            self.cookies[key] = value

    def _weapi(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        host: str = BASE_URL,
        check: bool = True,
    ) -> dict[str, Any]:
        url = host + path
        form = encrypt_weapi(payload or {})
        form["csrf_token"] = self.cookies.get("__csrf", "")
        return self._post_json(
            url,
            form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
            },
            check=check,
        )

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
        body = self._weapi(
            "/weapi/login/qrcode/unikey", {"type": 1, "noCheckToken": True}
        )
        key = body.get("unikey")
        if not key:
            raise AuthError(f"获取二维码 key 失败: {body}")
        return str(key)

    def qr_status(self, key: str) -> dict[str, Any]:
        # 800/801/802/803 都是正常状态，不走统一的 code==200 校验。
        return self._weapi(
            "/weapi/login/qrcode/client/login",
            {"key": key, "type": 1, "noCheckToken": True},
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
        body = self._weapi(
            "/api/cloud/upload/check",
            {
                "bitrate": "999000",
                "ext": ext,
                "length": str(size),
                "md5": md5,
                "songId": "0",
                "version": 1,
            },
            host=INTERFACE_URL,
        )
        return {
            "need_upload": bool(body.get("needUpload")),
            "song_id": _as_int(body.get("songId"), 0) or None,
            "raw": body,
        }

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
        response = self._http.get(NOS_LBS_URL, headers=self._headers())
        response.raise_for_status()
        body = response.json()
        upload_list = (body.get("data") or {}).get("upload") or []
        if not upload_list or not upload_list[0].get("host"):
            raise UploadError(f"NOS 上传节点获取失败: {body}")
        self._nos_host = str(upload_list[0]["host"])
        self._nos_host_at = now
        return self._nos_host

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
        song_id: int,
        name: str,
        album: str,
        artist: str,
        resource_id: str,
        bitrate: str = "999000",
    ) -> int:
        body = self._weapi(
            "/api/upload/cloud/info/v2",
            {
                "md5": md5,
                "songid": song_id,
                "filename": name,
                "song": name,
                "album": album,
                "artist": artist,
                "bitrate": bitrate,
                "resourceId": resource_id,
            },
        )
        new_id = _as_int(body.get("songId") or body.get("songid") or song_id, 0)
        if new_id <= 0:
            raise UploadError(f"提交云盘信息失败: {body}")
        return new_id

    def publish(self, song_id: int) -> None:
        self._weapi(
            "/api/cloud/pub/v2",
            {"songid": song_id},
            host=INTERFACE_URL,
        )

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
        if not check["need_upload"]:
            return UploadResult(
                False,
                song_id=check["song_id"],
                reason="remote_exists",
            )
        if dry_run:
            return UploadResult(False, reason="dry_run")

        tags = read_tags(path)
        name = sanitize_filename(tags.get("title") or path.stem)
        resource = self.alloc_token(md5, name, ext, bucket="")
        nos_token = self.alloc_token(md5, name, ext, bucket=NOS_BUCKET)
        self.upload_to_nos(nos_token, path, md5, ext)
        song_id = self.finish_upload(
            md5=md5,
            song_id=check["song_id"] or 0,
            name=name,
            album=display_album(tags),
            artist=display_artist(tags),
            resource_id=str(resource.get("resourceId", "")),
        )
        self.publish(song_id)
        if self.rate_limit:
            time.sleep(self.rate_limit)
        return UploadResult(True, song_id=song_id, reason="uploaded", cloud_name=name)

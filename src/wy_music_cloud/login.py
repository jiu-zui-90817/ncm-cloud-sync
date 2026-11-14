"""扫码 / 手机号登录。"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from .client import CloudClient
from .cookies import parse_cookie
from .errors import AuthError

QR_LOGIN_URL = "https://music.163.com/login?codekey={key}&chainId={chain_id}"


def make_chain_id() -> str:
    """网易云新版扫码登录要求的 chainId，格式与网页端一致。"""
    return f"v1_{secrets.token_hex(26).upper()}_web_login_{int(time.time() * 1000)}"


def build_qr_url(key: str, chain_id: str | None = None) -> str:
    return "https://music.163.com/login?" + urlencode(
        {"codekey": key, "chainId": chain_id or make_chain_id()}
    )


def _merge_cookie_payload(target: dict[str, str], raw: Any) -> None:
    if not raw:
        return
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                target[str(item["name"])] = str(item.get("value") or "")
            elif isinstance(item, str):
                target.update(parse_cookie(item))
        return
    target.update(parse_cookie(raw))


def apply_qr_cookies(client: CloudClient, body: Any) -> bool:
    """把 803 响应里的 cookie/cookies 合并进客户端，返回是否拿到 MUSIC_U。

    网易云不同版本分别返回 cookie 字符串、cookies 列表，或只通过 Set-Cookie
    下发；httpx 客户端会自动吸收 Set-Cookie，所以最后统一检查客户端 cookie。
    """
    if isinstance(body, dict):
        merged: dict[str, str] = {}
        for key in ("cookie", "cookies"):
            _merge_cookie_payload(merged, body.get(key))
        if merged:
            client.cookies.update(merged)
    return bool(client.cookies.get("MUSIC_U"))


def _status_code(body: dict[str, Any]) -> int:
    try:
        return int(body.get("code", -1))
    except (TypeError, ValueError):
        return -1


def _render_qr(text: str) -> str:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise AuthError("缺少 qrcode 依赖，无法显示二维码") from exc
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    lines = []
    for row in qr.get_matrix():
        lines.append("".join("██" if cell else "  " for cell in row))
    return "\n".join(lines)


def _save_qr(text: str, path: Path) -> None:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise AuthError("缺少 qrcode 依赖，无法生成二维码图片") from exc
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    qr.make_image(fill_color="black", back_color="white").save(path)


def qr_login(
    client: CloudClient,
    *,
    timeout: int = 180,
    qr_path: Path | None = None,
    output: Callable[[str], None] = print,
) -> tuple[dict[str, str], dict[str, Any]]:
    key = client.qr_unikey()
    url = build_qr_url(key)
    output("请用网易云音乐 App 扫码登录：")
    output(_render_qr(url))
    output(f"二维码内容: {url}")
    if qr_path is not None:
        _save_qr(url, qr_path)
        output(f"二维码图片已保存: {qr_path}")

    deadline = time.monotonic() + timeout
    last_code: int | None = None
    while time.monotonic() < deadline:
        body = client.qr_status(key)
        if not isinstance(body, dict):
            raise AuthError("扫码状态返回异常，请重新执行 login")
        code = _status_code(body)
        if code == 803:
            if not apply_qr_cookies(client, body):
                raise AuthError("扫码成功但未拿到 MUSIC_U，请重试")
            profile = client.login_status()
            output(f"登录成功: {profile.get('nickname', '')} (userId={profile.get('userId')})")
            return client.cookies, profile
        if code == 800:
            raise AuthError("二维码已过期，请重新执行 login")
        if code == 8821 and last_code != 8821:
            output("需要在手机上完成安全验证，完成后会自动继续")
        elif code == 802 and last_code != 802:
            output("已扫码，请在手机上确认登录")
        elif code == 801 and last_code != 801:
            output("等待扫码…")
        last_code = code
        time.sleep(2)
    raise AuthError("二维码登录超时，请重新执行 login")


def phone_login(
    client: CloudClient,
    phone: str,
    password: str,
    *,
    countrycode: str = "86",
) -> tuple[dict[str, str], dict[str, Any]]:
    client.phone_login(phone, password, countrycode)
    profile = client.login_status()
    return client.cookies, profile

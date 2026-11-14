"""扫码 / 手机号登录。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .client import CloudClient
from .cookies import parse_cookie
from .errors import AuthError

QR_LOGIN_URL = "https://music.163.com/login?codekey={key}"


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
    url = QR_LOGIN_URL.format(key=key)
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
        code = int(body.get("code", -1))
        if code == 803:
            raw = body.get("cookies")
            if raw:
                client.cookies.update(parse_cookie(raw))
            profile = client.login_status()
            output(f"登录成功: {profile.get('nickname', '')} (userId={profile.get('userId')})")
            return client.cookies, profile
        if code == 800:
            raise AuthError("二维码已过期，请重新执行 login")
        if code == 802 and last_code != 802:
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

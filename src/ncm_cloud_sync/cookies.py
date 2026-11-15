"""Cookie 解析与处理。"""

from __future__ import annotations

import json
from http.cookies import SimpleCookie
from urllib.parse import quote

from .errors import ConfigError

DEFAULT_COOKIES = {
    "os": "pc",
    "appver": "2.7.1.198277",
}


def parse_cookie(raw: str | dict | None) -> dict[str, str]:
    """接受 Cookie 头、JSON 对象、或单独的 MUSIC_U 值。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}

    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Cookie JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("Cookie JSON 必须是对象")
        return {str(k): str(v) for k, v in data.items() if v is not None}

    if "=" in text:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(text)
        except Exception:
            jar = SimpleCookie()
        if jar:
            return {key: morsel.value for key, morsel in jar.items()}
        # 手工兜底，处理个别含特殊字符的 Cookie。
        result: dict[str, str] = {}
        for part in text.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    # 用户只给了 MUSIC_U 的值。
    return {"MUSIC_U": text}


def ensure_defaults(cookies: dict[str, str]) -> dict[str, str]:
    result = dict(cookies)
    for key, value in DEFAULT_COOKIES.items():
        result.setdefault(key, value)
    return result


def cookie_header(cookies: dict[str, str], *, quote_values: bool = True) -> str:
    """序列化 Cookie；默认与 NeteaseCloudMusicApi 一样做 URL 编码。

    扫码登录需要和桌面客户端完全一致，传入 quote_values=False 发送原始值
    （尤其是含逗号的 _ntes_nnid）。
    """
    if not quote_values:
        return "; ".join(f"{key}={value}" for key, value in cookies.items())
    return "; ".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
        for key, value in cookies.items()
    )


def redact(cookies: dict[str, str]) -> dict[str, str]:
    result = dict(cookies)
    for key in ("MUSIC_U", "MUSIC_A", "__csrf"):
        if result.get(key):
            value = result[key]
            result[key] = value[:4] + "***" if len(value) > 4 else "***"
    return result

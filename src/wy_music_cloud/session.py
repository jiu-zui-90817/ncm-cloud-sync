"""登录会话的本地持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .cookies import parse_cookie
from .errors import ConfigError

SESSION_VERSION = 1


def save_session(
    path: Path,
    cookies: dict[str, str],
    profile: dict | None = None,
) -> None:
    """原子写入 session.json，权限尽量收紧到 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SESSION_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "cookies": cookies,
        "profile": profile or {},
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".session-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def load_session(path: Path) -> tuple[dict[str, str], dict]:
    """读取 session.json；不存在或损坏时返回空会话。"""
    if not path.is_file():
        return {}, {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(data, dict) or data.get("version") != SESSION_VERSION:
        return {}, {}
    cookies = data.get("cookies") or {}
    if not isinstance(cookies, dict):
        return {}, {}
    profile = data.get("profile") or {}
    if not isinstance(profile, dict):
        profile = {}
    return {str(k): str(v) for k, v in cookies.items()}, profile


def resolve_cookies(config) -> tuple[dict[str, str], str]:
    """按 环境变量/配置文件 > cookie_file > session.json 的顺序取登录凭证。

    返回 (cookies, source)。source 用于日志提示，不会打印内容。
    """
    raw = (config.cookie or "").strip()
    if raw:
        return parse_cookie(raw), "config"

    if config.cookie_file:
        try:
            text = Path(config.cookie_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"读取 cookie_file 失败: {exc}") from exc
        if text:
            return parse_cookie(text), "cookie_file"

    cookies, _ = load_session(config.session_path)
    if cookies:
        return cookies, "session"
    return {}, "none"

"""网易云每日签到。

签到只使用账号已有 Cookie 调用网页端接口，默认关闭。签到失败不会影响同步。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .errors import ConfigError
from .state import StateStore

logger = logging.getLogger(__name__)

SIGNIN_DATE = "signin_date"
SIGNIN_STATUS = "signin_status"
SIGNIN_MESSAGE = "signin_message"
SIGNIN_POINTS = "signin_points"
SIGNIN_AT = "signin_at"


def local_today() -> str:
    """按运行机器的本地时区取日期，避免 UTC 跨日导致一天签到两次。"""
    return datetime.now().astimezone().date().isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_points(value: str) -> int | None:
    try:
        points = int(value)
    except (TypeError, ValueError):
        return None
    return points if points > 0 else None


def signin_status(state: StateStore | None = None, *, enabled: bool = False) -> dict[str, Any]:
    """返回签到状态；没有状态库时返回未签到。"""
    date = ""
    status = ""
    message = ""
    points: int | None = None
    at = ""
    if state is not None:
        date = state.get_meta(SIGNIN_DATE, "") or ""
        status = state.get_meta(SIGNIN_STATUS, "") or ""
        message = state.get_meta(SIGNIN_MESSAGE, "") or ""
        points = _as_points(state.get_meta(SIGNIN_POINTS, "") or "")
        at = state.get_meta(SIGNIN_AT, "") or ""
    return {
        "enabled": enabled,
        "date": date,
        "status": status,
        "message": message,
        "points": points,
        "at": at,
        "today": date == local_today(),
    }


def _record(
    state: StateStore,
    *,
    date: str,
    status: str,
    message: str,
    points: int | None,
) -> None:
    state.set_meta(SIGNIN_DATE, date)
    state.set_meta(SIGNIN_STATUS, status)
    state.set_meta(SIGNIN_MESSAGE, message[:500])
    state.set_meta(SIGNIN_POINTS, str(points or ""))
    state.set_meta(SIGNIN_AT, _utc_now())


def attempt_daily_signin(
    config: Config,
    client: Any,
    state: StateStore,
    *,
    manual: bool = False,
    raise_errors: bool = False,
) -> dict[str, Any]:
    """执行一次签到。

    自动模式每天最多尝试一次（失败也等第二天）；手动模式允许在当天失败后重试，
    但已经签到成功时不会重复请求。
    """
    if not config.daily_signin:
        raise ConfigError("请先启用每日签到并保存设置")
    today = local_today()
    current = signin_status(state, enabled=True)
    if current["today"] and current["status"] in {"success", "already"}:
        return {**current, "skipped": True}
    if current["today"] and not manual:
        return {**current, "skipped": True}
    try:
        result = client.daily_signin()
    except Exception as exc:
        _record(
            state,
            date=today,
            status="failed",
            message=str(exc),
            points=None,
        )
        logger.warning("每日签到失败：%s", exc)
        if raise_errors:
            raise
        return {**signin_status(state, enabled=True), "skipped": False}
    status = "already" if result.get("already") else "success"
    points = result.get("points")
    message = str(result.get("message") or ("今天已经签到过了" if status == "already" else "签到成功"))
    _record(state, date=today, status=status, message=message, points=points)
    if status == "already":
        logger.info("每日签到：%s", message)
    else:
        logger.info("每日签到成功：%s%s", message, f"，云贝 +{points}" if points else "")
    return {**signin_status(state, enabled=True), "skipped": False}


def maybe_daily_signin(config: Config, client: Any, state: StateStore) -> dict[str, Any] | None:
    """同步流程内的自动签到；未启用时返回 None。"""
    if not config.daily_signin:
        return None
    return attempt_daily_signin(config, client, state)

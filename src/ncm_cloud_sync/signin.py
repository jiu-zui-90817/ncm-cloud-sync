"""网易云每日签到。

签到只使用账号已有 Cookie 调用网页端接口，默认关闭。签到失败不会影响同步。
每天在本地时间 08:00–23:00 之间随机选择一个时刻，避免每天固定时间请求。
"""

from __future__ import annotations

import logging
import secrets
import threading
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
SIGNIN_TARGET = "signin_target"

SIGNIN_WINDOW_START_HOUR = 8
SIGNIN_WINDOW_END_HOUR = 23

_SIGNIN_LOCK = threading.Lock()


def _local_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def local_today(now: datetime | None = None) -> str:
    """按运行机器的本地时区取日期，避免 UTC 跨日导致一天签到两次。"""
    return _local_now(now).date().isoformat()


def _parse_datetime(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _target_for_today(state: StateStore, now: datetime) -> datetime:
    """读取或生成今天的随机签到时刻。同一日期内不会重新抽取。"""
    raw = state.get_meta(SIGNIN_TARGET, "") or ""
    parsed = _parse_datetime(raw)
    if parsed is not None and parsed.astimezone().date() == now.date():
        return parsed
    hour = SIGNIN_WINDOW_START_HOUR + secrets.randbelow(
        SIGNIN_WINDOW_END_HOUR - SIGNIN_WINDOW_START_HOUR
    )
    minute = 1 + secrets.randbelow(59)
    second = secrets.randbelow(60)
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    state.set_meta(SIGNIN_TARGET, target.isoformat(timespec="seconds"))
    logger.info("今日签到随机时间：%s", target.strftime("%H:%M:%S"))
    return target


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_points(value: str) -> int | None:
    try:
        points = int(value)
    except (TypeError, ValueError):
        return None
    return points if points > 0 else None


def signin_status(
    state: StateStore | None = None,
    *,
    enabled: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回签到状态；没有状态库时返回未签到。"""
    now = _local_now(now)
    date = ""
    status = ""
    message = ""
    points: int | None = None
    at = ""
    target = ""
    if state is not None:
        date = state.get_meta(SIGNIN_DATE, "") or ""
        status = state.get_meta(SIGNIN_STATUS, "") or ""
        message = state.get_meta(SIGNIN_MESSAGE, "") or ""
        points = _as_points(state.get_meta(SIGNIN_POINTS, "") or "")
        at = state.get_meta(SIGNIN_AT, "") or ""
        raw_target = state.get_meta(SIGNIN_TARGET, "") or ""
        parsed_target = _parse_datetime(raw_target)
        if parsed_target is not None and parsed_target.astimezone().date() == now.date():
            target = raw_target
    return {
        "enabled": enabled,
        "date": date,
        "status": status,
        "message": message,
        "points": points,
        "at": at,
        "target": target,
        "window_start": f"{SIGNIN_WINDOW_START_HOUR:02d}:00",
        "window_end": f"{SIGNIN_WINDOW_END_HOUR:02d}:00",
        "today": date == now.date().isoformat(),
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


def _attempt_locked(
    config: Config,
    client: Any,
    state: StateStore,
    *,
    now: datetime,
    manual: bool,
    raise_errors: bool,
) -> dict[str, Any]:
    today = now.date().isoformat()
    current = signin_status(state, enabled=True, now=now)
    if current["today"] and current["status"] in {"success", "already"}:
        return {**current, "skipped": True, "pending": False}
    if current["today"] and current["status"] == "failed" and not manual:
        return {**current, "skipped": True, "pending": False}
    if not manual:
        target = _target_for_today(state, now)
        if now < target:
            logger.debug("今日签到时间未到：%s", target.isoformat(timespec="seconds"))
            return {
                **signin_status(state, enabled=True, now=now),
                "skipped": True,
                "pending": True,
                "target": target.isoformat(timespec="seconds"),
            }
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
        return {**signin_status(state, enabled=True, now=now), "skipped": False, "pending": False}
    status = "already" if result.get("already") else "success"
    points = result.get("points")
    message = str(result.get("message") or ("今天已经签到过了" if status == "already" else "签到成功"))
    _record(state, date=today, status=status, message=message, points=points)
    if status == "already":
        logger.info("每日签到：%s", message)
    else:
        logger.info("每日签到成功：%s%s", message, f"，云贝 +{points}" if points else "")
    return {**signin_status(state, enabled=True, now=now), "skipped": False, "pending": False}


def attempt_daily_signin(
    config: Config,
    client: Any,
    state: StateStore,
    *,
    manual: bool = False,
    raise_errors: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """执行一次签到。

    自动模式每天最多尝试一次（失败也等第二天），并且要等到当天随机时刻；
    手动模式忽略随机时刻，允许在当天失败后重试，但已经签到成功时不会重复请求。
    """
    if not config.daily_signin:
        raise ConfigError("请先启用每日签到并保存设置")
    local = _local_now(now)
    with _SIGNIN_LOCK:
        return _attempt_locked(
            config,
            client,
            state,
            now=local,
            manual=manual,
            raise_errors=raise_errors,
        )


def maybe_daily_signin(
    config: Config,
    client: Any,
    state: StateStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """同步流程内的自动签到；未启用时返回 None。"""
    if not config.daily_signin:
        return None
    return attempt_daily_signin(config, client, state, now=now)

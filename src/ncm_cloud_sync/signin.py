"""网易云每日签到。

签到只使用账号已有 Cookie 调用网页端接口，默认关闭。签到失败不会影响同步。
每天在用户设定的本地时间范围内（默认 08:00–23:00）随机选择一个时刻，避免固定时间请求。
可选同时调用手机端（type=0）任务；两端独立发奖，同一天同一端重复请求会返回已签到。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config, normalize_signin_window
from .errors import ApiError, ConfigError
from .state import StateStore

logger = logging.getLogger(__name__)

SIGNIN_DATE = "signin_date"
SIGNIN_STATUS = "signin_status"
SIGNIN_MESSAGE = "signin_message"
SIGNIN_POINTS = "signin_points"
SIGNIN_AT = "signin_at"
SIGNIN_TARGET = "signin_target"
SIGNIN_TARGET_WINDOW = "signin_target_window"
SIGNIN_DETAILS = "signin_details"

DEFAULT_SIGNIN_WINDOW_START = "08:00"
DEFAULT_SIGNIN_WINDOW_END = "23:00"

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


def _parse_details(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _target_for_today(
    state: StateStore,
    now: datetime,
    window_start: str,
    window_end: str,
) -> datetime:
    """读取或生成今天的随机签到时刻；窗口变化或跨日时重新抽取。"""
    start_text, end_text = normalize_signin_window(window_start, window_end)
    signature = f"{start_text}-{end_text}"
    raw = state.get_meta(SIGNIN_TARGET, "") or ""
    parsed = _parse_datetime(raw)
    if (
        parsed is not None
        and parsed.astimezone().date() == now.date()
        and state.get_meta(SIGNIN_TARGET_WINDOW, "") == signature
    ):
        return parsed
    start_hour, start_minute = (int(part) for part in start_text.split(":"))
    end_hour, end_minute = (int(part) for part in end_text.split(":"))
    start_offset = start_hour * 3600 + start_minute * 60
    end_offset = end_hour * 3600 + end_minute * 60
    offset = secrets.randbelow(end_offset - start_offset)
    target = now.replace(
        hour=start_hour, minute=start_minute, second=0, microsecond=0
    ) + timedelta(seconds=offset)
    state.set_meta(SIGNIN_TARGET, target.isoformat(timespec="seconds"))
    state.set_meta(SIGNIN_TARGET_WINDOW, signature)
    logger.info(
        "今日签到随机时间：%s（窗口 %s–%s）",
        target.strftime("%H:%M:%S"),
        start_text,
        end_text,
    )
    return target


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_points(value: Any) -> int | None:
    try:
        points = int(value)
    except (TypeError, ValueError):
        return None
    return points if points > 0 else None


def _detail_message(details: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in details:
        label = str(item.get("label") or "签到")
        message = str(item.get("message") or "")
        points = _as_points(item.get("points"))
        text = f"{label}：{message}" if message else label
        if points:
            text += f" +{points}"
        parts.append(text)
    return "；".join(parts)


def signin_status(
    state: StateStore | None = None,
    *,
    enabled: bool = False,
    window_start: str = DEFAULT_SIGNIN_WINDOW_START,
    window_end: str = DEFAULT_SIGNIN_WINDOW_END,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回签到状态；没有状态库时返回未签到。"""
    now = _local_now(now)
    start_text, end_text = normalize_signin_window(window_start, window_end)
    signature = f"{start_text}-{end_text}"
    date = ""
    status = ""
    message = ""
    points: int | None = None
    at = ""
    target = ""
    details: list[dict[str, Any]] = []
    if state is not None:
        date = state.get_meta(SIGNIN_DATE, "") or ""
        status = state.get_meta(SIGNIN_STATUS, "") or ""
        message = state.get_meta(SIGNIN_MESSAGE, "") or ""
        points = _as_points(state.get_meta(SIGNIN_POINTS, "") or "")
        at = state.get_meta(SIGNIN_AT, "") or ""
        details = _parse_details(state.get_meta(SIGNIN_DETAILS, "") or "")
        raw_target = state.get_meta(SIGNIN_TARGET, "") or ""
        parsed_target = _parse_datetime(raw_target)
        if (
            parsed_target is not None
            and parsed_target.astimezone().date() == now.date()
            and state.get_meta(SIGNIN_TARGET_WINDOW, "") == signature
        ):
            target = raw_target
    return {
        "enabled": enabled,
        "date": date,
        "status": status,
        "message": message,
        "points": points,
        "at": at,
        "target": target,
        "window_start": start_text,
        "window_end": end_text,
        "details": details,
        "today": date == now.date().isoformat(),
    }


def _record(
    state: StateStore,
    *,
    date: str,
    status: str,
    message: str,
    points: int | None,
    details: list[dict[str, Any]],
) -> None:
    state.set_meta(SIGNIN_DATE, date)
    state.set_meta(SIGNIN_STATUS, status)
    state.set_meta(SIGNIN_MESSAGE, message[:500])
    state.set_meta(SIGNIN_POINTS, str(points or ""))
    state.set_meta(SIGNIN_AT, _utc_now())
    state.set_meta(SIGNIN_DETAILS, json.dumps(details, ensure_ascii=False))


def _signin_one(client: Any, signin_type: int, label: str) -> dict[str, Any]:
    try:
        result = client.daily_signin(signin_type)
    except Exception as exc:
        return {
            "platform": "mobile" if signin_type == 0 else "desktop",
            "label": label,
            "signin_type": signin_type,
            "status": "failed",
            "message": str(exc),
            "points": None,
        }
    already = bool(result.get("already"))
    status = "already" if already else "success"
    message = str(
        result.get("message")
        or ("今天已经签到过了" if already else "签到成功")
    )
    return {
        "platform": "mobile" if signin_type == 0 else "desktop",
        "label": label,
        "signin_type": signin_type,
        "status": status,
        "already": already,
        "message": message,
        "points": _as_points(result.get("points")),
    }


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
    window_start = config.signin_window_start
    window_end = config.signin_window_end

    def status_snapshot() -> dict[str, Any]:
        return signin_status(
            state,
            enabled=True,
            window_start=window_start,
            window_end=window_end,
            now=now,
        )

    current = status_snapshot()
    if current["today"] and current["status"] in {"success", "already"}:
        return {**current, "skipped": True, "pending": False}
    if current["today"] and current["status"] in {"failed", "partial"} and not manual:
        return {**current, "skipped": True, "pending": False}
    if not manual:
        target = _target_for_today(state, now, window_start, window_end)
        if now < target:
            logger.debug("今日签到时间未到：%s", target.isoformat(timespec="seconds"))
            return {
                **status_snapshot(),
                "skipped": True,
                "pending": True,
                "target": target.isoformat(timespec="seconds"),
            }

    targets = [(1, "桌面端")]
    if config.signin_mobile:
        targets.append((0, "手机端"))
    details = [_signin_one(client, signin_type, label) for signin_type, label in targets]
    successes = [item for item in details if item["status"] in {"success", "already"}]
    failures = [item for item in details if item["status"] == "failed"]
    if not failures:
        status = "already" if all(item["status"] == "already" for item in details) else "success"
    elif not successes:
        status = "failed"
    else:
        status = "partial"
    points = sum(
        int(item["points"])
        for item in details
        if _as_points(item.get("points")) is not None
    )
    message = _detail_message(details)
    _record(
        state,
        date=today,
        status=status,
        message=message,
        points=points or None,
        details=details,
    )
    if status == "failed":
        logger.warning("每日签到失败：%s", message)
    elif status == "partial":
        logger.warning("每日签到部分成功：%s", message)
    elif status == "already":
        logger.info("每日签到：%s", message)
    else:
        logger.info("每日签到成功：%s", message)
    if raise_errors and status == "failed":
        raise ApiError(message)
    return {**status_snapshot(), "skipped": False, "pending": False}


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

    自动模式每天最多尝试一次（失败或部分成功也等第二天），并且要等到当天随机时刻；
    手动模式忽略随机时刻，允许在当天失败/部分成功后重试，但已经签到成功时不会重复请求。
    默认先签桌面端（type=1），开启 signin_mobile 后再签手机端（type=0）。
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

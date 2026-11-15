"""同步结果推送通知：不依赖第三方 SDK，全部走标准 HTTP/SMTP。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import smtplib
import time
import uuid
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

logger = logging.getLogger(__name__)

ALLOWED_TYPES = (
    "telegram",
    "bark",
    "ntfy",
    "gotify",
    "discord",
    "slack",
    "dingtalk",
    "serverchan",
    "pushplus",
    "pushover",
    "pushdeer",
    "webhook",
    "smtp",
)

TYPE_LABELS = {
    "telegram": "Telegram",
    "bark": "Bark",
    "ntfy": "ntfy",
    "gotify": "Gotify",
    "discord": "Discord",
    "slack": "Slack",
    "dingtalk": "钉钉机器人",
    "serverchan": "Server酱",
    "pushplus": "PushPlus",
    "pushover": "Pushover",
    "pushdeer": "PushDeer",
    "webhook": "通用 Webhook",
    "smtp": "SMTP 邮件",
}

FIELDS = {
    "telegram": ("bot_token", "chat_id", "api_base"),
    "bark": ("url", "group"),
    "ntfy": ("server", "topic", "token"),
    "gotify": ("server", "token", "priority"),
    "discord": ("webhook_url",),
    "slack": ("webhook_url",),
    "dingtalk": ("webhook_url", "secret", "at_mobiles"),
    "serverchan": ("sendkey",),
    "pushplus": ("token", "topic"),
    "pushover": ("token", "user"),
    "pushdeer": ("pushkey",),
    "webhook": ("url",),
    "smtp": ("host", "port", "username", "password", "mail_from", "mail_to", "security"),
}

REQUIRED = {
    "telegram": ("bot_token", "chat_id"),
    "bark": ("url",),
    "ntfy": ("topic",),
    "gotify": ("server", "token"),
    "discord": ("webhook_url",),
    "slack": ("webhook_url",),
    "dingtalk": ("webhook_url",),
    "serverchan": ("sendkey",),
    "pushplus": ("token",),
    "pushover": ("token", "user"),
    "pushdeer": ("pushkey",),
    "webhook": ("url",),
    "smtp": ("host", "mail_from", "mail_to"),
}

SECRET_FIELDS = {
    "bot_token",
    "token",
    "password",
    "sendkey",
    "pushkey",
    "webhook_url",
    "url",
    "secret",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_channels(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, (list, tuple)):
        raise ValueError("通知渠道必须是列表")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 个通知渠道格式不正确")
        channel_type = _text(item.get("type") or item.get("channel_type"))
        if channel_type not in FIELDS:
            raise ValueError(f"第 {index + 1} 个通知渠道类型不支持: {channel_type or '(空)'}")
        raw_config = item.get("config") if isinstance(item.get("config"), dict) else item
        config: dict[str, str] = {}
        for field in FIELDS[channel_type]:
            field_value = _text(raw_config.get(field))
            if field_value:
                config[field] = field_value
        enabled = bool(item.get("enabled", True))
        label = _text(item.get("name")) or TYPE_LABELS[channel_type]
        if enabled:
            missing = [field for field in REQUIRED[channel_type] if not config.get(field)]
            if missing:
                raise ValueError(f"通知渠道「{label}」缺少必填项: {', '.join(missing)}")
        channel_id = _text(item.get("id")) or uuid.uuid4().hex[:12]
        result.append(
            {
                "id": channel_id,
                "type": channel_type,
                "name": label,
                "enabled": enabled,
                "config": config,
            }
        )
    return result


def mask_channels(channels: Any) -> list[dict[str, Any]]:
    masked: list[dict[str, Any]] = []
    for item in channels or []:
        if not isinstance(item, dict):
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        safe_config = dict(config)
        for key in SECRET_FIELDS:
            if safe_config.get(key):
                safe_config[key] = "***"
        masked.append({**item, "config": safe_config})
    return masked


def _check_response(response: httpx.Response, label: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text.strip().replace("\n", " ")[:240]
    raise RuntimeError(f"{label} HTTP {response.status_code}: {detail}")


def _request_json(
    url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None
) -> httpx.Response:
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        response = client.post(url, json=payload, headers=headers or {})
        _check_response(response, "通知接口")
        return response


def _request_form(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        response = client.post(url, data=payload, headers=headers or {})
        _check_response(response, "通知接口")


def _url(value: str, label: str) -> str:
    url = value.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError(f"{label} 必须是 http:// 或 https:// 地址")
    return url


def _dingtalk_signed_url(url: str, secret: str) -> str:
    if not secret:
        return url
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(digest).decode("ascii"))
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}timestamp={timestamp}&sign={sign}"


def send_channel(channel: dict[str, Any], title: str, content: str) -> None:
    channel_type = _text(channel.get("type"))
    config = channel.get("config") if isinstance(channel.get("config"), dict) else {}
    label = _text(channel.get("name")) or TYPE_LABELS.get(channel_type, channel_type)

    if channel_type == "telegram":
        base = _url(_text(config.get("api_base")) or "https://api.telegram.org", label)
        url = base.rstrip("/") + "/bot" + _text(config.get("bot_token")) + "/sendMessage"
        _request_json(url, {"chat_id": _text(config.get("chat_id")), "text": title + "\n\n" + content})
        return
    if channel_type == "bark":
        url = _url(_text(config.get("url")), label)
        payload: dict[str, Any] = {"title": title, "body": content}
        if _text(config.get("group")):
            payload["group"] = _text(config.get("group"))
        _request_json(url, payload)
        return
    if channel_type == "ntfy":
        server = _url(_text(config.get("server")) or "https://ntfy.sh", label)
        headers = {}
        if _text(config.get("token")):
            headers["Authorization"] = "Bearer " + _text(config.get("token"))
        _request_json(
            server.rstrip("/"),
            {"topic": _text(config.get("topic")), "title": title[:120], "message": content, "priority": 3},
            headers=headers,
        )
        return
    if channel_type == "gotify":
        server = _url(_text(config.get("server")), label)
        priority = 5
        try:
            priority = int(_text(config.get("priority")) or "5")
        except ValueError:
            priority = 5
        _request_json(
            server.rstrip("/") + "/message?token=" + quote(_text(config.get("token")), safe=""),
            {"title": title, "message": content, "priority": priority},
        )
        return
    if channel_type == "discord":
        _request_json(_url(_text(config.get("webhook_url")), label), {"content": (title + "\n\n" + content)[:1900]})
        return
    if channel_type == "slack":
        _request_json(_url(_text(config.get("webhook_url")), label), {"text": "*" + title + "*\n" + content})
        return
    if channel_type == "dingtalk":
        url = _url(_text(config.get("webhook_url")), label)
        url = _dingtalk_signed_url(url, _text(config.get("secret")))
        payload: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": "### " + title + "\n\n" + content},
        }
        mobiles = [
            item.strip()
            for item in _text(config.get("at_mobiles")).replace(";", ",").split(",")
            if item.strip()
        ]
        if mobiles:
            payload["at"] = {"atMobiles": mobiles, "isAtAll": False}
        response = _request_json(url, payload)
        try:
            result = response.json()
        except Exception as exc:
            raise RuntimeError(f"{label} 返回了无法解析的响应") from exc
        errcode = int(result.get("errcode") or 0)
        if errcode:
            raise RuntimeError(f"{label} 返回错误 {errcode}: {_text(result.get('errmsg'))[:200]}")
        return
    if channel_type == "serverchan":
        key = _text(config.get("sendkey"))
        url = key if key.startswith("http") else "https://sctapi.ftqq.com/" + quote(key, safe="") + ".send"
        _request_form(_url(url, label), {"title": title, "desp": content})
        return
    if channel_type == "pushplus":
        payload = {"token": _text(config.get("token")), "title": title, "content": content, "template": "txt"}
        if _text(config.get("topic")):
            payload["topic"] = _text(config.get("topic"))
        _request_json("https://www.pushplus.plus/send", payload)
        return
    if channel_type == "pushover":
        _request_form(
            "https://api.pushover.net/1/messages.json",
            {
                "token": _text(config.get("token")),
                "user": _text(config.get("user")),
                "title": title,
                "message": content,
            },
        )
        return
    if channel_type == "pushdeer":
        _request_form(
            "https://api2.pushdeer.com/message/push",
            {"pushkey": _text(config.get("pushkey")), "text": title, "desp": content},
        )
        return
    if channel_type == "webhook":
        payload = {"title": title, "content": content, "text": title + "\n\n" + content}
        _request_json(_url(_text(config.get("url")), label), payload)
        return
    if channel_type == "smtp":
        _send_smtp(config, title, content, label)
        return
    raise RuntimeError(f"不支持的通知类型: {channel_type}")


def _send_smtp(config: dict[str, Any], title: str, content: str, label: str) -> None:
    host = _text(config.get("host"))
    security = (_text(config.get("security")) or "starttls").lower()
    try:
        port = int(_text(config.get("port")) or ("465" if security == "ssl" else "587"))
    except ValueError:
        port = 465 if security == "ssl" else 587
    mail_from = _text(config.get("mail_from"))
    recipients = [
        item.strip()
        for item in _text(config.get("mail_to")).replace(";", ",").split(",")
        if item.strip()
    ]
    if not host or not mail_from or not recipients:
        raise RuntimeError(f"{label} 缺少 SMTP 主机、发件人或收件人")
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message.set_content(content)
    if security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        smtp = smtplib.SMTP(host, port, timeout=15)
    try:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if _text(config.get("username")):
            smtp.login(_text(config.get("username")), _text(config.get("password")))
        smtp.send_message(message)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def send_test(channel: dict[str, Any]) -> None:
    send_channel(channel, "网易云云盘同步 · 测试通知", "这是一条测试通知，收到即表示通知渠道配置可用。")


def _stat(stats: Any, name: str, default: Any = 0) -> Any:
    return getattr(stats, name, default)


def notify_run(channels: Any, notify_on: str, stats: Any, error: str | None = None) -> list[dict[str, Any]]:
    try:
        normalized = normalize_channels(channels)
    except ValueError as exc:
        logger.warning("通知渠道配置无效，跳过推送: %s", exc)
        return []
    mode = _text(notify_on) or "always"
    if mode == "never":
        return []
    paused = bool(_stat(stats, "paused", False))
    stopped = bool(_stat(stats, "stopped", False))
    failed = int(_stat(stats, "failed", 0) or 0)
    if stopped:
        event = "stopped"
    elif paused:
        event = "paused"
    elif failed or error:
        event = "failure"
    else:
        event = "success"
    if mode == "failure" and event == "success":
        return []
    if mode == "success" and event != "success":
        return []

    titles = {
        "success": "网易云云盘同步完成",
        "failure": "网易云云盘同步有失败",
        "paused": "网易云云盘同步已暂停",
        "stopped": "网易云云盘同步已终止",
    }
    title = titles[event]
    scanned = int(_stat(stats, "scanned", 0) or 0)
    uploaded = int(_stat(stats, "uploaded", 0) or 0)
    skipped = int(_stat(stats, "skipped", 0) or 0)
    permission = int(_stat(stats, "permission_denied", 0) or 0)
    deferred = int(_stat(stats, "deferred", 0) or 0)
    missing = int(_stat(stats, "missing", 0) or 0)
    bytes_uploaded = int(_stat(stats, "bytes_uploaded", 0) or 0)
    duration = float(_stat(stats, "duration", 0.0) or 0.0)
    lines = [
        f"扫描 {scanned} 首，上传 {uploaded} 首，跳过 {skipped} 首，失败 {failed} 首。",
    ]
    if permission:
        lines.append(f"其中无权限 {permission} 首，请在应用中心检查音乐目录权限。")
    if missing:
        lines.append(f"本地缺失 {missing} 首。")
    if deferred:
        lines.append(f"还有 {deferred} 首留待下一轮。")
    lines.append(f"上传 {bytes_uploaded / 1024 / 1024:.1f} MB，耗时 {duration:.1f} 秒。")
    if error:
        lines.append("错误：" + str(error))
    content = "\n".join(lines)

    results: list[dict[str, Any]] = []
    for channel in normalized:
        if not channel.get("enabled"):
            continue
        try:
            send_channel(channel, title, content)
        except Exception as exc:
            logger.warning("通知发送失败（%s）: %s", channel.get("name"), exc)
            results.append({"id": channel.get("id"), "type": channel.get("type"), "ok": False, "error": str(exc)})
        else:
            logger.info("通知已发送（%s）", channel.get("name"))
            results.append({"id": channel.get("id"), "type": channel.get("type"), "ok": True, "error": ""})
    return results

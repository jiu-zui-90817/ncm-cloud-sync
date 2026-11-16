from __future__ import annotations

import base64
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import ncm_cloud_sync.notify as notify


def test_normalize_channels_and_mask() -> None:
    channels = notify.normalize_channels(
        [
            {
                "type": "bark",
                "name": "手机",
                "config": {"url": "https://api.day.app/abc", "group": "sync"},
            }
        ]
    )
    assert len(channels) == 1
    channel = channels[0]
    assert channel["type"] == "bark"
    assert channel["name"] == "手机"
    assert channel["enabled"] is True
    assert len(channel["id"]) == 12
    assert channel["config"]["url"] == "https://api.day.app/abc"
    masked = notify.mask_channels(channels)
    assert masked[0]["config"]["url"] == "***"
    assert masked[0]["config"]["group"] == "sync"


def test_normalize_channels_rejects_missing_required() -> None:
    with pytest.raises(ValueError, match="缺少必填项"):
        notify.normalize_channels([{"type": "bark", "config": {}}])
    with pytest.raises(ValueError, match="不支持"):
        notify.normalize_channels([{"type": "wecom", "config": {}}])


def test_normalize_disabled_channel_skips_required() -> None:
    channels = notify.normalize_channels([{"type": "bark", "enabled": False}])
    assert channels[0]["enabled"] is False
    assert channels[0]["config"] == {}


def test_notify_run_filters_by_result(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    def fake_send(channel, title, content) -> None:
        sent.append((channel["type"], title))

    monkeypatch.setattr(notify, "send_channel", fake_send)
    channels = [{"type": "webhook", "config": {"url": "https://example.com/hook"}}]
    ok = SimpleNamespace(failed=0, paused=False, stopped=False, scanned=3, uploaded=2, permission_denied=0)
    bad = SimpleNamespace(failed=1, paused=False, stopped=False, scanned=3, uploaded=1, permission_denied=0)
    paused = SimpleNamespace(failed=0, paused=True, stopped=False, scanned=3, uploaded=1, permission_denied=0)

    assert len(notify.notify_run(channels, "always", ok)) == 1
    assert sent[-1][1].endswith("任务完成")
    assert notify.notify_run(channels, "failure", ok) == []
    assert len(notify.notify_run(channels, "failure", bad)) == 1
    assert "有失败" in sent[-1][1]
    assert notify.notify_run(channels, "never", bad) == []
    assert len(notify.notify_run(channels, "always", paused)) == 1
    assert "已暂停" in sent[-1][1]
    stopped = SimpleNamespace(failed=0, paused=False, stopped=True, scanned=3, uploaded=1, permission_denied=0)
    assert len(notify.notify_run(channels, "always", stopped)) == 1
    assert "已终止" in sent[-1][1]

    preflight_failed = SimpleNamespace(
        failed=0,
        paused=False,
        stopped=False,
        scanned=3,
        uploaded=0,
        permission_denied=0,
        error="上传前接口检查失败：接口 400",
    )
    assert len(notify.notify_run(channels, "failure", preflight_failed)) == 1
    assert "有失败" in sent[-1][1]


def test_notify_run_skips_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(notify, "send_channel", lambda channel, title, content: sent.append(title))
    channels = [{"type": "webhook", "config": {"url": "https://example.com/hook"}}]
    empty = SimpleNamespace(
        failed=0, paused=False, stopped=False, scanned=3, uploaded=0, linked=0, permission_denied=0
    )
    assert notify.notify_run(channels, "always", empty) == []
    assert sent == []
    assert len(notify.notify_run(channels, "always", empty, notify_empty=True)) == 1
    assert sent == ["NCM 云盘同步任务完成"]


def test_dingtalk_keyword_defaults_and_migrates_to_ncm() -> None:
    channels = notify.normalize_channels(
        [
            {"type": "dingtalk", "config": {"webhook_url": "https://example.com/robot"}},
            {
                "type": "dingtalk",
                "config": {"webhook_url": "https://example.com/robot2", "keyword": "音乐"},
            },
            {
                "type": "dingtalk",
                "config": {"webhook_url": "https://example.com/robot3", "keyword": "网易云"},
            },
        ]
    )
    assert [channel["config"]["keyword"] for channel in channels] == ["NCM", "音乐", "NCM"]


def test_send_test_uses_ncm_title(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "send_channel", lambda channel, title, content: sent.append((title, content)))
    notify.send_test({"type": "webhook", "name": "hook", "config": {"url": "https://example.com/hook"}})
    assert sent[0][0] == "NCM 云盘同步 · 测试通知"


def test_webhook_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(notify.httpx, "Client", FakeClient)
    notify.send_channel(
        {"type": "webhook", "name": "hook", "config": {"url": "https://example.com/hook"}},
        "标题",
        "内容",
    )
    assert captured["url"] == "https://example.com/hook"
    assert captured["json"] == {"title": "标题", "content": "内容", "text": "标题\n\n内容"}


def test_dingtalk_signed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(notify.httpx, "Client", FakeClient)
    secret = "SECtestsecret"
    notify.send_channel(
        {
            "type": "dingtalk",
            "name": "钉钉",
            "config": {
                "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=abc",
                "secret": secret,
                "keyword": "音乐,云盘",
                "at_mobiles": "13800000000,13900000000",
            },
        },
        "同步完成",
        "上传 2 首",
    )
    query = parse_qs(urlparse(str(captured["url"])).query)
    assert query["access_token"] == ["abc"]
    timestamp = query["timestamp"][0]
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
    ).decode()
    assert query["sign"] == [expected]
    payload = captured["json"]
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"] == "同步完成"
    assert payload["markdown"]["text"].startswith("音乐")
    assert payload["at"]["atMobiles"] == ["13800000000", "13900000000"]


def test_dingtalk_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"errcode": 310000, "errmsg": "sign not match"}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url, json=None, headers=None):
            return FakeResponse()

    monkeypatch.setattr(notify.httpx, "Client", FakeClient)
    with pytest.raises(RuntimeError, match="310000"):
        notify.send_channel(
            {"type": "dingtalk", "config": {"webhook_url": "https://example.com/robot"}},
            "标题",
            "内容",
        )


def test_normalize_channels_accepts_tuple() -> None:
    channels = notify.normalize_channels(
        ({"type": "bark", "config": {"url": "https://api.day.app/abc"}},)
    )
    assert channels[0]["type"] == "bark"


def test_notify_run_accepts_tuple_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        notify,
        "send_channel",
        lambda channel, title, content: sent.append(str(channel["id"])),
    )
    results = notify.notify_run(
        ({"id": "abc", "type": "bark", "config": {"url": "https://api.day.app/abc"}},),
        "always",
        SimpleNamespace(uploaded=1),
    )
    assert sent == ["abc"]
    assert results == [{"id": "abc", "type": "bark", "ok": True, "error": ""}]

from __future__ import annotations

from types import SimpleNamespace

import pytest

import wy_music_cloud.notify as notify


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
    assert sent[-1][1].endswith("同步完成")
    assert notify.notify_run(channels, "failure", ok) == []
    assert len(notify.notify_run(channels, "failure", bad)) == 1
    assert "有失败" in sent[-1][1]
    assert notify.notify_run(channels, "never", bad) == []
    assert len(notify.notify_run(channels, "always", paused)) == 1
    assert "已暂停" in sent[-1][1]
    stopped = SimpleNamespace(failed=0, paused=False, stopped=True, scanned=3, uploaded=1, permission_denied=0)
    assert len(notify.notify_run(channels, "always", stopped)) == 1
    assert "已终止" in sent[-1][1]


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

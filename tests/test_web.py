from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import ncm_cloud_sync.notify as notify
from ncm_cloud_sync.config import Config
from ncm_cloud_sync.session import load_session, save_session
from ncm_cloud_sync.state import StateStore
from ncm_cloud_sync.syncer import SyncStats
from ncm_cloud_sync.web import (
    LoginFlow,
    SessionService,
    SettingsStore,
    SyncManager,
    WebApp,
    make_qr_svg,
)


def make_app(tmp_path: Path) -> WebApp:
    music = tmp_path / "music"
    music.mkdir()
    config = Config(music_dirs=[music], data_dir=tmp_path / "data")
    config.ensure_data_dir()
    sessions = SessionService(config)
    return WebApp(
        config=config,
        settings=SettingsStore(tmp_path / "settings.json"),
        sessions=sessions,
        sync=SyncManager(config, sessions),
        login=LoginFlow(config, sessions),
        log_paths=[tmp_path / "app.log"],
        data_dir=config.data_dir,
    )


def call(
    app: WebApp,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    admin: bool = True,
    verify: bool = True,
    query: str = "",
) -> tuple[str, dict[str, str], Any]:
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "REMOTE_ADDR": "127.0.0.1",
    }
    if admin:
        environ["HTTP_X_TRIM_ISADMIN"] = "true"
    if verify:
        environ["HTTP_X_NCM_CLOUD_SYNC"] = "web"
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    payload = b"".join(app(environ, start_response))
    content_type = captured.get("headers", {}).get("Content-Type", "")
    if payload and "json" in content_type:
        parsed = json.loads(payload.decode("utf-8"))
    else:
        parsed = payload.decode("utf-8")
    return captured["status"], captured["headers"], parsed


def test_health_is_public(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/api/health", admin=False)
    assert status == "200 OK"
    assert data["ok"] is True


def test_api_requires_admin(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/api/status", admin=False)
    assert status == "403 Forbidden"
    assert "管理员" in data["error"]


def test_status_shape_and_static(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, headers, data = call(app, "/api/status")
    assert status == "200 OK"
    assert data["logged_in"] is False
    assert data["stats"]["total"] == 0
    assert data["settings"]["music_dirs"] == [str(app.config.music_dirs[0])]
    assert data["settings"]["exclude_dirs"] == []
    assert data["sync"]["state"] == "idle"
    assert data["sync"]["can_pause"] is False
    assert data["runs"] == []
    status, headers, _ = call(app, "/")
    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/html")


def test_gateway_prefix_is_stripped(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/app/ncm-cloud-sync/api/health", admin=False)
    assert status == "200 OK"
    assert data["ok"] is True


def test_sync_requires_header_and_login(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/api/sync", method="POST", body={}, verify=False)
    assert status == "403 Forbidden"
    assert "校验头" in data["error"]
    status, _, data = call(app, "/api/sync", method="POST", body={})
    assert status == "401 Unauthorized"
    assert "登录" in data["error"]


def test_settings_save_updates_config(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    music = tmp_path / "music2"
    music.mkdir()
    excluded = music / "有声书"
    excluded.mkdir()
    status, _, data = call(
        app,
        "/api/settings",
        method="PUT",
        body={
            "music_dirs": [str(music)],
            "exclude_dirs": [str(excluded)],
            "interval": 300,
            "rate_limit": 2.5,
            "log_level": "DEBUG",
        },
    )
    assert status == "200 OK"
    assert data["ok"] is True
    assert app.config.music_dirs == [music]
    assert app.config.interval == 300
    assert app.config.rate_limit == 2.5
    assert app.config.exclude_dirs == (excluded.resolve(),)
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["music_dirs"] == [str(music)]
    assert saved["exclude_dirs"] == [str(excluded.resolve())]
    assert saved["rate_limit"] == 2.5
    assert saved["log_level"] == "DEBUG"


def test_settings_reject_missing_directory(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(
        app,
        "/api/settings",
        method="PUT",
        body={"music_dirs": [str(tmp_path / "nope")], "interval": 3600},
    )
    assert status == "400 Bad Request"
    assert "不存在" in data["error"]


def test_sync_controls_return_conflict_when_idle(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    save_session(app.config.session_path, {"MUSIC_U": "token", "__csrf": "csrf"})
    for path in ("/api/sync/pause", "/api/sync/stop", "/api/sync/resume"):
        status, _, data = call(app, path, method="POST")
        assert status == "409 Conflict"
        assert data["ok"] is False
        assert data["status"]["state"] == "idle"


def test_qr_svg_contains_path() -> None:
    svg = make_qr_svg("https://example.com")
    assert svg.startswith("<svg")
    assert "<path" in svg


def test_login_flow_uses_official_qr_url(tmp_path: Path, monkeypatch: Any) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "ncm_cloud_sync.web.CloudClient.qr_unikey", lambda self: "testkey"
    )
    result = app.login.start()
    try:
        parsed = urlparse(result["qr_url"])
        query = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "music.163.com"
        assert parsed.path == "/login"
        assert query["codekey"] == ["testkey"]
        assert "chainId" not in query
        assert "music.y.163.com" not in result["qr_url"]
    finally:
        app.login.close()


def test_settings_save_notifications_and_auto_sync(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    music = app.config.music_dirs[0]
    status, _, data = call(
        app,
        "/api/settings",
        method="PUT",
        body={
            "music_dirs": [str(music)],
            "exclude_dirs": [],
            "interval": 1800,
            "rate_limit": 1.0,
            "log_level": "INFO",
            "auto_sync": False,
            "notify_on": "failure",
            "notifications": [
                {"type": "bark", "name": "手机", "config": {"url": "https://api.day.app/abc"}}
            ],
        },
    )
    assert status == "200 OK"
    assert data["ok"] is True
    assert app.config.auto_sync is False
    assert app.config.notify_on == "failure"
    assert len(app.config.notify_channels) == 1
    assert app.config.notify_channels[0]["type"] == "bark"
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["auto_sync"] is False
    assert saved["notify_on"] == "failure"
    assert saved["notifications"][0]["config"]["url"] == "https://api.day.app/abc"


def test_settings_reject_invalid_notification(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(
        app,
        "/api/settings",
        method="PUT",
        body={
            "music_dirs": [str(app.config.music_dirs[0])],
            "interval": 3600,
            "notifications": [{"type": "bark", "config": {}}],
        },
    )
    assert status == "400 Bad Request"
    assert "缺少必填项" in data["error"]


def test_problems_api_lists_and_retries(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with StateStore(app.config.db_path) as state:
        state.mark_seen("denied.mp3", 10, 1)
        state.mark_failed("denied.mp3", "读取文件失败：没有读取权限")
        state.mark_seen("boom.mp3", 10, 1)
        state.mark_failed("boom.mp3", "网络错误")

    status, _, data = call(app, "/api/problems", query="kind=permission")
    assert status == "200 OK"
    assert data["counts"] == {"failed": 1, "permission": 1}
    assert [item["path"] for item in data["items"]] == ["denied.mp3"]
    assert data["items"][0]["kind"] == "permission"

    status, _, data = call(app, "/api/problems")
    assert status == "200 OK"
    assert data["kind"] == "all"
    assert {item["path"]: item["kind"] for item in data["items"]} == {
        "boom.mp3": "failed",
        "denied.mp3": "permission",
    }

    status, _, data = call(app, "/api/status")
    assert data["stats"]["failed"] == 1
    assert data["stats"]["permission"] == 1
    assert data["stats"]["problems_total"] == 2

    status, _, data = call(app, "/api/problems/retry", method="POST", body={"kind": "failed"})
    assert status == "200 OK"
    assert data["reset"] == 1
    assert data["counts"] == {"failed": 0, "permission": 1}


def test_notify_types_and_test_endpoint(tmp_path: Path, monkeypatch: Any) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/api/notify/types")
    assert status == "200 OK"
    types = {item["type"]: item for item in data["types"]}
    assert "telegram" in types
    assert "dingtalk" in types
    assert "secret" in types["dingtalk"]["secret"]
    assert "url" in types["bark"]["fields"]
    assert types["bark"]["secret"] == ["url"]

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(notify, "send_test", lambda channel: sent.append(channel))
    status, _, data = call(
        app,
        "/api/notify/test",
        method="POST",
        body={"channel": {"type": "bark", "config": {"url": "https://api.day.app/abc"}}},
    )
    assert status == "200 OK"
    assert data["ok"] is True
    assert sent[0]["type"] == "bark"

    status, _, data = call(
        app,
        "/api/notify/test",
        method="POST",
        body={"type": "bark", "config": {}},
    )
    assert status == "400 Bad Request"
    assert data["ok"] is False


def test_status_total_excludes_ignored(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with StateStore(app.config.db_path) as state:
        state.mark_seen("hidden.mp3", 10, 1)
        state.mark_ignored("hidden.mp3", "命中排除规则")
        state.mark_seen("pending.mp3", 10, 1)
    status, _, data = call(app, "/api/status")
    assert status == "200 OK"
    assert data["stats"]["total"] == 1
    assert data["stats"]["pending"] == 1


def test_update_api_and_force_refresh(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    calls: list[int] = []

    def fake_fetch():
        calls.append(1)
        return {
            "latest": "9.9.9",
            "release_url": "https://example.com/release",
            "published_at": "2026-11-15T00:00:00Z",
            "notes": "更新内容",
        }

    app.updates._fetch = fake_fetch
    status, _, data = call(app, "/api/update")
    assert status == "200 OK"
    assert data["latest"] == "9.9.9"
    assert data["update_available"] is True
    assert data["repo_url"].startswith("https://github.com/")
    assert len(calls) == 1

    status, _, data = call(app, "/api/update", query="force=1")
    assert status == "200 OK"
    assert len(calls) == 2

    app.updates._fetch = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    status, _, data = call(app, "/api/update", query="force=1")
    assert status == "200 OK"
    assert "boom" in data["error"]


def test_static_page_has_about_and_update(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, html = call(app, "/")
    assert status == "200 OK"
    assert 'data-page="about"' in html
    assert "checkUpdateButton" in html
    assert "updateNotes" in html
    assert "favicon.png" in html
    assert "favicon.svg" not in html


def test_scan_only_trigger_is_readonly_and_resets(tmp_path: Path, monkeypatch: Any) -> None:
    app = make_app(tmp_path)
    save_session(app.config.session_path, {"MUSIC_U": "token", "__csrf": "csrf"})
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    class ScanSyncer:
        def __init__(self, *args, **kwargs) -> None:
            captured["scan_only"] = kwargs.get("scan_only")

        def run_once(self):
            stats = SyncStats()
            stats.mode = "scan"
            stats.scanned = 2
            return stats

    monkeypatch.setattr("ncm_cloud_sync.web.CloudClient", FakeClient)
    monkeypatch.setattr("ncm_cloud_sync.web.Syncer", ScanSyncer)
    assert app.sync.trigger(scan_only=True) is True
    app.sync._thread.join(timeout=5)
    assert captured["scan_only"] is True
    status = app.sync.status()
    assert status["state"] == "idle"
    assert status["scan_only"] is False


def test_stop_current_keeps_auto_sync_enabled(tmp_path: Path, monkeypatch: Any) -> None:
    app = make_app(tmp_path)
    save_session(app.config.session_path, {"MUSIC_U": "token", "__csrf": "csrf"})
    started = threading.Event()
    release = threading.Event()

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    class BlockingSyncer:
        def __init__(self, *args, **kwargs) -> None:
            self.stop_event = kwargs["stop_event"]

        def run_once(self):
            started.set()
            release.wait(5)
            stats = SyncStats()
            stats.stopped = self.stop_event.is_set()
            return stats

    monkeypatch.setattr("ncm_cloud_sync.web.CloudClient", FakeClient)
    monkeypatch.setattr("ncm_cloud_sync.web.Syncer", BlockingSyncer)
    assert app.sync.trigger() is True
    assert started.wait(5) is True
    assert app.sync.stop_current() is True
    assert app.sync.status()["state"] == "stopping"
    assert app.sync._auto_paused is False
    release.set()
    app.sync._thread.join(timeout=5)
    status = app.sync.status()
    assert status["state"] == "stopped"
    assert status["auto_paused"] is False
    assert status["auto_sync"] is True


def test_login_flow_saves_cookies(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        def qr_status(self, key: str) -> dict[str, Any]:
            assert key == "key"
            return {"code": 803, "cookies": [{"name": "MUSIC_U", "value": "token"}]}

        def login_status(self) -> dict[str, Any]:
            return {"nickname": "tester", "userId": 42}

        def close(self) -> None:
            pass

    app.login._client = FakeClient()
    app.login._key = "key"
    app.login._created = time.monotonic()
    result = app.login.poll("key")
    assert result["logged_in"] is True
    cookies, profile = load_session(app.config.session_path)
    assert cookies["MUSIC_U"] == "token"
    assert cookies["os"] == "pc"
    assert profile["nickname"] == "tester"


def test_login_flow_accepts_singular_cookie(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        def qr_status(self, key: str) -> dict[str, Any]:
            return {"code": 803, "cookie": "MUSIC_U=singular; __csrf=csrf"}

        def login_status(self) -> dict[str, Any]:
            return {"nickname": "tester", "userId": 42}

        def close(self) -> None:
            pass

    app.login._client = FakeClient()
    app.login._key = "key"
    app.login._created = time.monotonic()
    result = app.login.poll("key")
    assert result["logged_in"] is True
    cookies, _ = load_session(app.config.session_path)
    assert cookies["MUSIC_U"] == "singular"
    assert cookies["__csrf"] == "csrf"
    assert cookies["os"] == "pc"


def test_login_flow_verification_required_keeps_session(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        def qr_status(self, key: str) -> dict[str, Any]:
            return {"code": 8821, "message": "需要安全验证"}

        def close(self) -> None:
            pass

    app.login._client = FakeClient()
    app.login._key = "key"
    app.login._created = time.monotonic()
    result = app.login.poll("key")
    assert result["code"] == 8821
    assert result["verification_required"] is True
    assert app.login._client is not None
    assert not app.config.session_path.exists()


def test_qr_poll_malformed_body_returns_401(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    class FakeClient:
        def __init__(self) -> None:
            self.cookies: dict[str, str] = {}

        def qr_status(self, key: str) -> Any:
            return None

        def close(self) -> None:
            pass

    app.login._client = FakeClient()
    app.login._key = "key"
    app.login._created = time.monotonic()
    status, _, data = call(app, "/api/qr/status", query="key=key")
    assert status == "401 Unauthorized"
    assert "异常" in data["error"]


def test_cookie_login_saves_session(tmp_path: Path, monkeypatch: Any) -> None:
    app = make_app(tmp_path)

    class FakeClient:
        def __init__(self, cookies, **_kwargs) -> None:
            self.cookies = dict(cookies)

        def login_status(self) -> dict[str, Any]:
            return {"nickname": "cookie user", "userId": 7}

        def close(self) -> None:
            pass

    monkeypatch.setattr("ncm_cloud_sync.web.CloudClient", FakeClient)
    status, _, data = call(
        app,
        "/api/login/cookie",
        method="POST",
        body={"cookie": "MUSIC_U=token; __csrf=csrf"},
    )
    assert status == "200 OK"
    assert data["logged_in"] is True
    cookies, profile = load_session(app.config.session_path)
    assert cookies["MUSIC_U"] == "token"
    assert cookies["os"] == "pc"
    assert profile["userId"] == 7


def test_cookie_login_requires_music_u(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(
        app,
        "/api/login/cookie",
        method="POST",
        body={"cookie": "__csrf=csrf"},
    )
    assert status == "401 Unauthorized"
    assert "MUSIC_U" in data["error"]


def test_diagnostics_and_log_paths(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/api/diagnostics")
    assert status == "200 OK"
    assert data["version"]
    assert data["data_dir"] == str(app.config.data_dir)
    assert data["log_paths"][0]["path"].endswith("app.log")
    assert data["log_paths"][0]["exists"] is False
    assert "TRIM_PKGVAR" in data["env"]
    assert isinstance(data["logs"], str)

    status, _, logs = call(app, "/api/logs")
    assert status == "200 OK"
    assert logs["log_paths"]
    assert logs["log_exists"] is False
    assert logs["log_files"][0]["path"].endswith("app.log")
    assert logs["log_files"][0]["exists"] is False
    assert logs["log_limit_bytes"] == 6 * 1024 * 1024


def test_runs_api_and_status(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with StateStore(app.config.db_path) as state:
        for index in range(6):
            run_id = state.start_run()
            state.finish_run(run_id, scanned=index, uploaded=index)

    status, _, data = call(app, "/api/status")
    assert status == "200 OK"
    assert len(data["runs"]) == 5
    assert data["runs"][0]["uploaded"] == 5

    status, _, data = call(app, "/api/runs", query="limit=99")
    assert status == "200 OK"
    assert len(data["runs"]) == 5

    status, _, data = call(app, "/api/runs", query="limit=1")
    assert status == "200 OK"
    assert len(data["runs"]) == 1


def test_run_detail_api(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with StateStore(app.config.db_path) as state:
        run_id = state.start_run("scan")
        state.finish_run(
            run_id,
            stats={"mode": "scan", "to_upload": 1, "cloud_only": 1},
            items=[
                {"kind": "to_upload", "name": "a.mp3", "detail": "待上传"},
                {"kind": "cloud_only", "name": "b", "detail": "仅云端"},
            ],
        )

    status, _, data = call(app, f"/api/runs/{run_id}")
    assert status == "200 OK"
    assert data["run"]["mode"] == "scan"
    assert data["counts"] == {"to_upload": 1, "cloud_only": 1}
    assert data["total"] == 2
    assert len(data["items"]) == 2

    status, _, data = call(app, f"/api/runs/{run_id}", query="kind=cloud_only")
    assert status == "200 OK"
    assert data["total"] == 1
    assert [item["name"] for item in data["items"]] == ["b"]

    status, _, data = call(app, "/api/runs/99999")
    assert status == "404 Not Found"
    status, _, data = call(app, "/api/runs/abc")
    assert status == "400 Bad Request"


def test_static_page_has_cookie_login(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, html = call(app, "/")
    assert status == "200 OK"
    assert "cookieInput" in html
    assert "diagnosticsButton" in html
    assert "copyDiagnosticsDashboard" in html
    assert "logCopyDiagnostics" in html
    assert "forceRecheck" in html
    assert "loginDiagnosticsButton" in html
    assert "copyDiagnosticsButton" in html
    assert "statProblems" in html
    assert "statProblemBreakdown" in html
    assert "rateLimit" in html
    assert "excludeDirs" in html
    assert "pauseButton" in html
    assert "stopButton" in html
    assert "resumeButton" in html
    assert "scanOnlyButton" in html
    assert "runDetailModal" in html
    assert "runDetailList" in html

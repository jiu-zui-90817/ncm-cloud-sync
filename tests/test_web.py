from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from wy_music_cloud.config import Config
from wy_music_cloud.session import load_session
from wy_music_cloud.web import (
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
        environ["HTTP_X_WY_MUSIC"] = "web"
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
    status, headers, _ = call(app, "/")
    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/html")


def test_gateway_prefix_is_stripped(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    status, _, data = call(app, "/app/wy-music-cloud/api/health", admin=False)
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
    status, _, data = call(
        app,
        "/api/settings",
        method="PUT",
        body={
            "music_dirs": [str(music)],
            "interval": 300,
            "dry_run": True,
            "log_level": "DEBUG",
        },
    )
    assert status == "200 OK"
    assert data["ok"] is True
    assert app.config.music_dirs == [music]
    assert app.config.interval == 300
    assert app.config.dry_run is True
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["music_dirs"] == [str(music)]
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


def test_qr_svg_contains_path() -> None:
    svg = make_qr_svg("https://example.com")
    assert svg.startswith("<svg")
    assert "<path" in svg


def test_login_flow_uses_official_qr_url(tmp_path: Path, monkeypatch: Any) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "wy_music_cloud.web.CloudClient.qr_unikey", lambda self: "testkey"
    )
    result = app.login.start()
    try:
        parsed = urlparse(result["qr_url"])
        query = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "music.163.com"
        assert query["codekey"] == ["testkey"]
        assert query["chainId"][0].startswith("v1_")
        assert "_web_login_" in query["chainId"][0]
        assert "music.y.163.com" not in result["qr_url"]
    finally:
        app.login.close()


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

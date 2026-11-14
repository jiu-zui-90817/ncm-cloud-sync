"""内置 Web UI：状态、扫码登录、手动同步、设置与日志。"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from wsgiref.types import StartResponse
else:  # wsgiref.types 只在 Python 3.11+ 存在
    StartResponse = Any

from . import __version__
from .client import CloudClient
from .config import Config
from .cookies import ensure_defaults, parse_cookie
from .errors import ApiError, AuthError, ConfigError, UploadError, WyMusicError
from .login import QR_LOGIN_URL
from .session import load_session, save_session
from .state import StateStore
from .syncer import SyncStats, Syncer

logger = logging.getLogger(__name__)

GATEWAY_PREFIX = "/app/wy-music-cloud"
WEBUI_DIR = Path(__file__).with_name("webui")
DEFAULT_MUSIC_DIR = "/vol1/1000/Music"
DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 1,
    "music_dirs": [],
    "interval": 3600,
    "dry_run": False,
    "log_level": "INFO",
}
ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "application/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("必须是整数") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"必须在 {minimum} 到 {maximum} 之间")
    return number


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_music_dirs(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("music_dirs 必须是数组")
    result: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if "\x00" in text or "\n" in text or "\r" in text:
            raise ValueError("音乐目录包含非法字符")
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ValueError(f"音乐目录必须是绝对路径: {text}")
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"目录不存在: {path}")
        if not os.access(path, os.R_OK):
            raise ValueError(f"目录不可读: {path}")
        item = str(path)
        if item not in result:
            result.append(item)
    if not result:
        raise ValueError("至少需要一个音乐目录")
    return result


def make_qr_svg(text: str) -> str:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise AuthError("缺少 qrcode 依赖，无法显示二维码") from exc
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    cells: list[str] = []
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if filled:
                cells.append(f"M{x} {y}h1v1h-1z")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        'shape-rendering="crispEdges" role="img" aria-label="网易云音乐登录二维码">'
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
        f'<path d="{"".join(cells)}" fill="#111111"/>'
        "</svg>"
    )


def read_log_tail(paths: list[Path], lines: int = 200) -> str:
    lines = max(10, min(1000, lines))
    chunks: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                raw = fh.read()
        except OSError:
            continue
        text = raw.decode("utf-8", "replace")
        chunks.append(text[-128 * 1024 :])
    merged = "\n".join(chunk for chunk in chunks if chunk).splitlines()
    return "\n".join(merged[-lines:])


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        if not self.path.is_file():
            return data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("设置文件损坏，使用默认设置: %s", self.path)
            return data
        if not isinstance(raw, dict):
            return data
        dirs = raw.get("music_dirs")
        if isinstance(dirs, list):
            data["music_dirs"] = [str(item) for item in dirs if str(item).strip()]
        try:
            data["interval"] = _as_int(raw.get("interval", data["interval"]), 3600, 60, 86400)
        except ValueError:
            data["interval"] = 3600
        data["dry_run"] = _as_bool(raw.get("dry_run"), False)
        level = str(raw.get("log_level") or data["log_level"]).upper()
        data["log_level"] = level if level in ALLOWED_LOG_LEVELS else "INFO"
        return data

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        data["music_dirs"] = normalize_music_dirs(payload.get("music_dirs") or [])
        data["interval"] = _as_int(payload.get("interval", 3600), 3600, 60, 86400)
        data["dry_run"] = _as_bool(payload.get("dry_run"), False)
        level = str(payload.get("log_level") or "INFO").upper()
        if level not in ALLOWED_LOG_LEVELS:
            raise ValueError("日志级别必须是 DEBUG/INFO/WARNING/ERROR")
        data["log_level"] = level
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return data


def apply_settings(config: Config, settings: dict[str, Any]) -> None:
    dirs = settings.get("music_dirs") or []
    if dirs:
        config.music_dirs = [Path(str(item)).expanduser() for item in dirs]
    config.interval = int(settings.get("interval", config.interval))
    config.dry_run = bool(settings.get("dry_run", config.dry_run))
    level = str(settings.get("log_level") or config.log_level).upper()
    if level in ALLOWED_LOG_LEVELS:
        config.log_level = level


def bootstrap_settings(config: Config) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if config.music_dirs:
        settings["music_dirs"] = [str(path) for path in config.music_dirs]
    if not settings["music_dirs"]:
        accessible = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
        candidates = [item for item in accessible.split(os.pathsep) if item.strip()]
        if not candidates and Path(DEFAULT_MUSIC_DIR).is_dir():
            candidates = [DEFAULT_MUSIC_DIR]
        settings["music_dirs"] = candidates
    settings["interval"] = config.interval
    settings["dry_run"] = config.dry_run
    settings["log_level"] = config.log_level
    return settings


class SessionService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[dict[str, str], dict[str, Any]]:
        with self._lock:
            return load_session(self.config.session_path)

    def has_session(self) -> bool:
        cookies, _ = self.snapshot()
        return bool(cookies)

    def client(self) -> CloudClient:
        cookies, _ = self.snapshot()
        if not cookies:
            raise AuthError("尚未登录，请先扫码登录")
        return CloudClient(
            cookies,
            timeout=self.config.timeout,
            retries=self.config.retries,
            rate_limit=self.config.rate_limit,
        )

    def save(self, cookies: dict[str, str], profile: dict[str, Any] | None = None) -> None:
        with self._lock:
            save_session(self.config.session_path, cookies, profile)

    def logout(self) -> None:
        with self._lock:
            try:
                self.config.session_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("删除 session.json 失败", exc_info=True)


class LoginFlow:
    def __init__(self, config: Config, sessions: SessionService, *, timeout: int = 240) -> None:
        self.config = config
        self.sessions = sessions
        self.timeout = timeout
        self._lock = threading.Lock()
        self._client: CloudClient | None = None
        self._key: str | None = None
        self._created = 0.0

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._close_locked()
            client = CloudClient(
                {},
                timeout=self.config.timeout,
                retries=self.config.retries,
                rate_limit=0,
            )
            key = client.qr_unikey()
            url = QR_LOGIN_URL.format(key=key)
            svg = make_qr_svg(url)
            self._client = client
            self._key = key
            self._created = time.monotonic()
            return {
                "key": key,
                "qr_svg": svg,
                "qr_url": url,
                "expires_in": self.timeout,
            }

    def poll(self, key: str) -> dict[str, Any]:
        with self._lock:
            if not self._client or not self._key or key != self._key:
                return {"code": 404, "message": "登录会话已失效，请刷新二维码"}
            if time.monotonic() - self._created > self.timeout:
                self._close_locked()
                return {"code": 800, "message": "二维码已过期，请刷新"}
            body = self._client.qr_status(self._key)
            code = int(body.get("code", -1))
            if code == 803:
                raw = body.get("cookies")
                if isinstance(raw, list):
                    cookies = {
                        str(item.get("name")): str(item.get("value"))
                        for item in raw
                        if isinstance(item, dict) and item.get("name") and item.get("value")
                    }
                else:
                    cookies = parse_cookie(raw)
                cookies = ensure_defaults(cookies)
                if "MUSIC_U" not in cookies:
                    raise AuthError("扫码成功但未拿到 MUSIC_U，请重试")
                self._client.cookies.update(cookies)
                profile = self._client.login_status()
                self.sessions.save(self._client.cookies, profile)
                self._close_locked()
                return {"code": 803, "logged_in": True, "profile": profile, "message": "登录成功"}
            if code == 802:
                return {"code": 802, "message": "已扫码，请在手机上确认"}
            if code == 800:
                self._close_locked()
                return {"code": 800, "message": "二维码已过期，请刷新"}
            if code == 801:
                return {"code": 801, "message": "等待扫码"}
            return {"code": code, "message": f"登录状态异常（code={code}）"}

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._key = None
        self._created = 0.0


class SyncManager:
    def __init__(self, config: Config, sessions: SessionService) -> None:
        self.config = config
        self.sessions = sessions
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = False
        self._mode = ""
        self._started_at: str | None = None
        self._error: str | None = None
        self._progress: list[dict[str, Any]] = []
        self._next_run_at: str | None = None
        self._scheduler: threading.Thread | None = None

    def start_scheduler(self) -> None:
        if self._scheduler and self._scheduler.is_alive():
            return
        self._stop.clear()
        self._scheduler = threading.Thread(target=self._scheduler_loop, name="wy-scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._scheduler and self._scheduler.is_alive():
            self._scheduler.join(timeout=3)

    def apply_settings(self, settings: dict[str, Any]) -> None:
        with self._lock:
            apply_settings(self.config, settings)
        self._wake.set()
        logger.info("同步设置已更新，间隔 %s 秒，dry_run=%s", self.config.interval, self.config.dry_run)

    def trigger(self, *, dry_run: bool = False, force: bool = False, source: str = "manual") -> bool:
        if not self.sessions.has_session():
            raise AuthError("尚未登录，请先扫码登录")
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._mode = source
            self._started_at = utc_now()
            self._error = None
            self._progress = []
            effective_dry_run = bool(dry_run or self.config.dry_run)
            self._thread = threading.Thread(
                target=self._run,
                kwargs={"dry_run": effective_dry_run, "force": force},
                name="wy-sync",
                daemon=True,
            )
            self._thread.start()
            return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._running
            mode = self._mode
            started_at = self._started_at
            error = self._error
            progress = list(self._progress[-8:])
        return {
            "running": running,
            "mode": mode,
            "started_at": started_at,
            "error": error,
            "progress": progress,
            "last_run": read_json(self.config.last_run_path),
            "next_run_at": self._next_run_at,
        }

    def _run(self, *, dry_run: bool, force: bool) -> None:
        started = time.monotonic()
        try:
            cookies, _ = self.sessions.snapshot()
            client = CloudClient(
                cookies,
                timeout=self.config.timeout,
                retries=self.config.retries,
                rate_limit=self.config.rate_limit,
            )
            try:
                with StateStore(self.config.db_path) as state:
                    syncer = Syncer(
                        self.config,
                        client,
                        state,
                        dry_run=dry_run,
                        force=force,
                        progress=self._on_progress,
                    )
                    stats = syncer.run_once()
                from .daemon import write_last_run

                write_last_run(self.config.last_run_path, stats)
                logger.info("Web 触发的同步完成: %s", stats.summary())
            finally:
                client.close()
        except AuthError as exc:
            self._error = str(exc)
            logger.warning("同步中止：%s", exc)
        except Exception as exc:
            self._error = str(exc)
            logger.exception("同步失败")
            try:
                from .daemon import write_last_run

                write_last_run(self.config.last_run_path, SyncStats(), error=str(exc))
            except Exception:
                pass
        finally:
            with self._lock:
                self._running = False
                self._mode = ""
                self._progress.append({"event": "finished", "duration": round(time.monotonic() - started, 1)})

    def _on_progress(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._progress.append({"event": event, **payload})
            if len(self._progress) > 40:
                self._progress = self._progress[-40:]

    def _scheduler_loop(self) -> None:
        config = self.config
        next_at = time.time() + min(30, max(5, config.interval))
        self._next_run_at = datetime.fromtimestamp(next_at, timezone.utc).isoformat(timespec="seconds")
        while not self._stop.is_set():
            wait = max(1.0, next_at - time.time())
            if self._wake.wait(wait):
                self._wake.clear()
                next_at = time.time() + max(60, int(config.interval))
                self._next_run_at = datetime.fromtimestamp(next_at, timezone.utc).isoformat(timespec="seconds")
                continue
            if self._stop.is_set():
                break
            try:
                if self.sessions.has_session():
                    self.trigger(source="schedule")
            except Exception:
                logger.warning("定时同步触发失败", exc_info=True)
            next_at = time.time() + max(60, int(config.interval))
            with self._lock:
                self._next_run_at = datetime.fromtimestamp(next_at, timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class WebApp:
    config: Config
    settings: SettingsStore
    sessions: SessionService
    sync: SyncManager
    login: LoginFlow
    log_paths: list[Path]
    data_dir: Path
    allow_local: bool = False

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        try:
            return self._dispatch(environ, start_response)
        except Exception:
            logger.exception("Web 请求处理失败")
            return self._json(start_response, "500 Internal Server Error", {"error": "服务器内部错误"})

    def _dispatch(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        script = str(environ.get("SCRIPT_NAME") or "")
        if script and path.startswith(script):
            path = path[len(script) :] or "/"
        if path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/"):
            path = path[len(GATEWAY_PREFIX) :] or "/"
        if not path.startswith("/"):
            path = "/" + path

        if path == "/api/health":
            return self._json(start_response, "200 OK", {"ok": True, "version": __version__})
        if path == "/" or path.lstrip("/") in STATIC_FILES:
            if method not in {"GET", "HEAD"}:
                return self._json(start_response, "405 Method Not Allowed", {"error": "method not allowed"})
            return self._static(path, environ, start_response)
        if path.startswith("/api/"):
            return self._api(path, method, environ, start_response)
        return self._json(start_response, "404 Not Found", {"error": "not found"})

    def _static(self, path: str, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        key = path.lstrip("/")
        name, content_type = STATIC_FILES.get(key, STATIC_FILES[""])
        file_path = WEBUI_DIR / name
        if not file_path.is_file():
            return self._json(start_response, "500 Internal Server Error", {"error": f"缺少前端文件 {name}"})
        body = file_path.read_bytes()
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache, no-store, must-revalidate"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        start_response("200 OK", headers)
        return [body] if str(environ.get("REQUEST_METHOD", "GET")).upper() != "HEAD" else [b""]

    def _api(self, path: str, method: str, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        if path == "/api/health":
            return self._json(start_response, "200 OK", {"ok": True, "version": __version__})
        if not self._is_admin(environ):
            return self._json(start_response, "403 Forbidden", {"error": "需要 fnOS 管理员权限"})
        if method in {"POST", "PUT", "DELETE"} and environ.get("HTTP_X_WY_MUSIC") != "web":
            return self._json(start_response, "403 Forbidden", {"error": "缺少请求校验头"})

        try:
            if path == "/api/status" and method == "GET":
                return self._json(start_response, "200 OK", self._status())
            if path == "/api/qr" and method == "POST":
                return self._json(start_response, "200 OK", self.login.start())
            if path == "/api/qr/status" and method == "GET":
                query = parse_qs(str(environ.get("QUERY_STRING") or ""))
                key = (query.get("key") or [""])[0]
                if not key:
                    raise ValueError("缺少 key")
                return self._json(start_response, "200 OK", self.login.poll(key))
            if path == "/api/logout" and method == "POST":
                self.sessions.logout()
                return self._json(start_response, "200 OK", {"ok": True})
            if path == "/api/sync" and method == "POST":
                body = self._body(environ)
                started = self.sync.trigger(
                    dry_run=_as_bool(body.get("dry_run"), False),
                    force=_as_bool(body.get("force"), False),
                    source="manual",
                )
                return self._json(
                    start_response,
                    "202 Accepted" if started else "409 Conflict",
                    {"started": started, "status": self.sync.status()},
                )
            if path == "/api/sync/status" and method == "GET":
                return self._json(start_response, "200 OK", self.sync.status())
            if path == "/api/settings" and method == "GET":
                return self._json(start_response, "200 OK", self._settings_payload())
            if path == "/api/settings" and method == "PUT":
                body = self._body(environ)
                settings = self.settings.save(body)
                self.sync.apply_settings(settings)
                self._apply_log_level(settings.get("log_level", "INFO"))
                return self._json(start_response, "200 OK", {"ok": True, "settings": settings})
            if path == "/api/logs" and method == "GET":
                query = parse_qs(str(environ.get("QUERY_STRING") or ""))
                lines = _as_int((query.get("lines") or ["200"])[0], 200, 10, 1000)
                return self._json(start_response, "200 OK", {"logs": read_log_tail(self.log_paths, lines)})
            if path == "/api/account/refresh" and method == "POST":
                with self.sessions.client() as client:
                    profile = client.login_status()
                    cookies = dict(client.cookies)
                self.sessions.save(cookies, profile)
                return self._json(start_response, "200 OK", {"profile": profile})
            return self._json(start_response, "404 Not Found", {"error": "unknown api"})
        except AuthError as exc:
            return self._json(start_response, "401 Unauthorized", {"error": str(exc)})
        except ConfigError as exc:
            return self._json(start_response, "400 Bad Request", {"error": str(exc)})
        except ValueError as exc:
            return self._json(start_response, "400 Bad Request", {"error": str(exc)})
        except (ApiError, UploadError) as exc:
            return self._json(start_response, "502 Bad Gateway", {"error": str(exc)})
        except WyMusicError as exc:
            return self._json(start_response, "500 Internal Server Error", {"error": str(exc)})

    def _status(self) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("music_dirs"):
            settings = bootstrap_settings(self.config)
        cookies, profile = self.sessions.snapshot()
        stats: dict[str, int] = {}
        try:
            with StateStore(self.config.db_path) as state:
                stats = state.stats()
        except Exception:
            logger.warning("读取状态数据库失败", exc_info=True)
        dirs = []
        for directory in self.config.music_dirs:
            dirs.append(
                {
                    "path": str(directory),
                    "exists": directory.is_dir(),
                    "readable": directory.is_dir() and os.access(directory, os.R_OK),
                }
            )
        return {
            "version": __version__,
            "now": utc_now(),
            "logged_in": bool(cookies),
            "profile": profile,
            "account": {
                "nickname": profile.get("nickname") or "",
                "userId": profile.get("userId"),
            },
            "music_dirs": dirs,
            "settings": settings,
            "config": self.config.redacted(),
            "stats": {
                "total": sum(stats.values()),
                "uploaded": stats.get("uploaded", 0),
                "pending": stats.get("pending", 0),
                "failed": stats.get("failed", 0),
                "missing": stats.get("missing", 0),
                "uploading": stats.get("uploading", 0),
                "skipped": stats.get("skipped_local", 0) + stats.get("skipped_remote", 0),
            },
            "sync": self.sync.status(),
        }

    def _settings_payload(self) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("music_dirs"):
            settings = bootstrap_settings(self.config)
        return {
            "settings": settings,
            "music_dirs": [str(path) for path in self.config.music_dirs],
            "interval": self.config.interval,
            "dry_run": self.config.dry_run,
            "log_level": self.config.log_level,
            "settings_path": str(self.settings.path),
        }

    def _body(self, environ: dict[str, Any]) -> dict[str, Any]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 64 * 1024:
            raise ValueError("请求体过大")
        raw = environ.get("wsgi.input").read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _is_admin(self, environ: dict[str, Any]) -> bool:
        if str(environ.get("HTTP_X_TRIM_ISADMIN") or "").lower() == "true":
            return True
        if self.allow_local:
            return str(environ.get("REMOTE_ADDR") or "") in {"127.0.0.1", "::1"}
        return False

    @staticmethod
    def _apply_log_level(level: str) -> None:
        logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))

    @staticmethod
    def _json(start_response: StartResponse, status: str, data: dict[str, Any]) -> list[bytes]:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]


def setup_logging(path: Path | None, level: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_app(config: Config, *, allow_local: bool = False) -> tuple[WebApp, SyncManager]:
    settings_path = Path(
        os.environ.get("WY_MUSIC_SETTINGS") or (config.data_dir / "settings.json")
    ).expanduser()
    store = SettingsStore(settings_path)
    settings = store.load()
    if not settings.get("music_dirs"):
        settings = bootstrap_settings(config)
    apply_settings(config, settings)
    config.ensure_data_dir()
    log_path = Path(os.environ.get("WY_MUSIC_LOG_FILE") or (config.data_dir / "app.log"))
    setup_logging(log_path, config.log_level)
    sessions = SessionService(config)
    sync = SyncManager(config, sessions)
    login = LoginFlow(config, sessions)
    app = WebApp(
        config=config,
        settings=store,
        sessions=sessions,
        sync=sync,
        login=login,
        log_paths=[log_path, config.data_dir / "app.out"],
        data_dir=config.data_dir,
        allow_local=allow_local,
    )
    return app, sync


def main() -> int:
    config = Config.load(validate=False)
    allow_local = _as_bool(os.environ.get("WY_MUSIC_DEV"), False)
    app, sync = build_app(config, allow_local=allow_local)
    sync.start_scheduler()
    socket_path = os.environ.get("WY_MUSIC_SOCKET", "").strip()

    def _handle_signal(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    try:
        try:
            from waitress import serve
        except ImportError as exc:
            raise SystemExit("缺少 waitress 依赖，请安装 requirements-native.txt") from exc
        if socket_path:
            path = Path(socket_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            print(f"wy-music-cloud web listening on unix:{path}", flush=True)
            serve(app, unix_socket=str(path), unix_socket_perms="660", threads=8, channel_timeout=120)
        else:
            host = os.environ.get("WY_MUSIC_HOST", "127.0.0.1")
            port = _as_int(os.environ.get("WY_MUSIC_PORT", "8765"), 8765, 1, 65535)
            print(f"wy-music-cloud web listening on http://{host}:{port}", flush=True)
            serve(app, host=host, port=port, threads=8, channel_timeout=120)
    except KeyboardInterrupt:
        logger.info("收到退出信号，Web UI 停止")
    finally:
        sync.stop()
        try:
            app.login.close()
        except Exception:
            pass
        if socket_path:
            try:
                Path(socket_path).unlink(missing_ok=True)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

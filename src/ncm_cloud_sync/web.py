"""内置 Web UI：状态、扫码登录、手动同步、设置与日志。"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from wsgiref.types import StartResponse
else:  # wsgiref.types 只在 Python 3.11+ 存在
    StartResponse = Any

from . import __version__, notify
from .client import CloudClient
from .config import Config
from .cookies import ensure_defaults, parse_cookie
from .errors import ApiError, AuthError, ConfigError, UploadError, NcmSyncError
from .login import apply_qr_cookies, build_qr_url
from .session import load_session, save_session
from .state import StateStore
from .syncer import SyncStats, Syncer
from .updates import REPO_URL, RELEASES_URL, UpdateChecker

logger = logging.getLogger(__name__)

# 即使 app.log 因权限/路径问题看不到，也能从内存里取到最近日志。
LOG_BUFFER: deque[str] = deque(maxlen=1000)


class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:  # pragma: no cover - 日志本身不能影响主流程
            self.handleError(record)


GATEWAY_PREFIX = "/app/ncm-cloud-sync"
WEBUI_DIR = Path(__file__).with_name("webui")
DEFAULT_MUSIC_DIR = "/vol1/1000/Music"
DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 1,
    "music_dirs": [],
    "interval": 3600,
    "rate_limit": 1.0,
    "dry_run": False,
    "log_level": "INFO",
    "exclude_dirs": [],
    "auto_sync": True,
    "notify_on": "always",
    "notifications": [],
}
ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "application/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
    "favicon.png": ("favicon.png", "image/png"),
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


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("必须是数字") from exc
    if not (minimum <= number <= maximum):
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


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def normalize_exclude_dirs(values: Any, music_dirs: list[str]) -> list[str]:
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError("exclude_dirs 必须是数组")
    roots = [Path(str(item)).expanduser().resolve() for item in music_dirs]
    result: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if "\x00" in text or "\n" in text or "\r" in text:
            raise ValueError("排除目录包含非法字符")
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ValueError(f"排除目录必须是绝对路径: {text}")
        path = path.resolve()
        if path in roots:
            raise ValueError(f"不能排除整个音乐根目录: {path}")
        if not any(_is_within(path, root) for root in roots):
            raise ValueError(f"排除目录必须位于某个音乐目录内: {path}")
        if not path.is_dir():
            raise ValueError(f"排除目录不存在: {path}")
        if not os.access(path, os.R_OK):
            raise ValueError(f"排除目录不可读: {path}")
        item = str(path)
        if item not in result:
            result.append(item)
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
    if not merged:
        merged = list(LOG_BUFFER)
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
        try:
            data["rate_limit"] = _as_float(raw.get("rate_limit", data["rate_limit"]), 1.0, 0.0, 10.0)
        except ValueError:
            data["rate_limit"] = 1.0
        stored_excludes = raw.get("exclude_dirs")
        if isinstance(stored_excludes, list):
            data["exclude_dirs"] = [
                str(item).strip()
                for item in stored_excludes
                if str(item).strip() and Path(str(item)).expanduser().is_absolute()
            ]
        data["dry_run"] = _as_bool(raw.get("dry_run"), False)
        data["auto_sync"] = _as_bool(raw.get("auto_sync"), True)
        notify_on = str(raw.get("notify_on") or "always").lower()
        data["notify_on"] = notify_on if notify_on in {"always", "failure", "success", "never"} else "always"
        try:
            data["notifications"] = notify.normalize_channels(raw.get("notifications"))
        except ValueError as exc:
            logger.warning("通知设置无效，已忽略: %s", exc)
            data["notifications"] = []
        level = str(raw.get("log_level") or data["log_level"]).upper()
        data["log_level"] = level if level in ALLOWED_LOG_LEVELS else "INFO"
        return data

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        data["music_dirs"] = normalize_music_dirs(payload.get("music_dirs") or [])
        data["interval"] = _as_int(payload.get("interval", 3600), 3600, 60, 86400)
        data["rate_limit"] = _as_float(payload.get("rate_limit", 1.0), 1.0, 0.0, 10.0)
        data["exclude_dirs"] = normalize_exclude_dirs(
            payload.get("exclude_dirs") or [], data["music_dirs"]
        )
        data["dry_run"] = _as_bool(payload.get("dry_run"), False)
        data["auto_sync"] = _as_bool(payload.get("auto_sync"), True)
        notify_on = str(payload.get("notify_on") or "always").lower()
        if notify_on not in {"always", "failure", "success", "never"}:
            raise ValueError("通知时机必须是 always/failure/success/never")
        data["notify_on"] = notify_on
        data["notifications"] = notify.normalize_channels(payload.get("notifications") or [])
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
    config.rate_limit = float(settings.get("rate_limit", config.rate_limit))
    if "exclude_dirs" in settings:
        config.exclude_dirs = tuple(
            Path(str(item)).expanduser()
            for item in (settings.get("exclude_dirs") or [])
        )
    config.auto_sync = bool(settings.get("auto_sync", config.auto_sync))
    notify_on = str(settings.get("notify_on") or config.notify_on).lower()
    if notify_on in {"always", "failure", "success", "never"}:
        config.notify_on = notify_on
    config.notify_channels = tuple(settings.get("notifications") or [])
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
    settings["rate_limit"] = config.rate_limit
    settings["exclude_dirs"] = [str(path) for path in config.exclude_dirs]
    settings["auto_sync"] = config.auto_sync
    settings["notify_on"] = config.notify_on
    settings["notifications"] = list(config.notify_channels)
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
            url = build_qr_url(key)
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
            if not isinstance(body, dict):
                raise AuthError("扫码状态返回异常，请刷新二维码重试")
            try:
                code = int(body.get("code", -1))
            except (TypeError, ValueError):
                code = -1
            if code == 803:
                if not apply_qr_cookies(self._client, body):
                    raise AuthError("扫码成功但未拿到 MUSIC_U，请重试")
                self._client.cookies = ensure_defaults(dict(self._client.cookies))
                try:
                    profile = self._client.login_status()
                    self.sessions.save(self._client.cookies, profile)
                except OSError as exc:
                    raise ConfigError(f"保存登录状态失败：{exc}") from exc
                self._close_locked()
                return {"code": 803, "logged_in": True, "profile": profile, "message": "登录成功"}
            if code == 8821:
                return {
                    "code": 8821,
                    "verification_required": True,
                    "message": "需要在手机上完成安全验证，请按手机提示操作",
                }
            if code == 802:
                return {"code": 802, "message": "已扫码，请在手机上确认"}
            if code == 800:
                self._close_locked()
                return {"code": 800, "message": "二维码已过期，请刷新"}
            if code == 801:
                return {"code": 801, "message": "等待扫码"}
            return {"code": code, "message": f"登录状态异常（code={code}）"}

    def cookie_login(self, raw_cookie: str) -> dict[str, Any]:
        text = str(raw_cookie or "").strip()
        if not text:
            raise AuthError("请粘贴包含 MUSIC_U 的网易云 Cookie")
        try:
            cookies = parse_cookie(text)
        except ValueError as exc:
            raise AuthError(f"Cookie 格式无法识别：{exc}") from exc
        if not isinstance(cookies, dict) or not cookies.get("MUSIC_U"):
            raise AuthError("Cookie 中缺少 MUSIC_U，请从已登录的 music.163.com 复制完整 Cookie")
        client = CloudClient(
            cookies,
            timeout=self.config.timeout,
            retries=self.config.retries,
            rate_limit=0,
        )
        try:
            profile = client.login_status()
            client.cookies = ensure_defaults(dict(client.cookies))
            try:
                self.sessions.save(client.cookies, profile)
            except OSError as exc:
                raise ConfigError(f"保存登录状态失败：{exc}") from exc
        finally:
            client.close()
        with self._lock:
            self._close_locked()
        return {"logged_in": True, "profile": profile, "message": "登录成功"}

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
        self._pause_run = threading.Event()
        self._stop_run = threading.Event()
        self._running = False
        self._mode = ""
        self._state = "idle"
        self._auto_paused = False
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
        logger.info(
            "同步设置已更新，间隔 %s 秒，请求间隔 %s 秒，排除目录 %s 个，dry_run=%s",
            self.config.interval,
            self.config.rate_limit,
            len(self.config.exclude_dirs),
            self.config.dry_run,
        )

    def trigger(self, *, dry_run: bool = False, force: bool = False, source: str = "manual") -> bool:
        if not self.sessions.has_session():
            raise AuthError("尚未登录，请先扫码登录")
        with self._lock:
            if self._running:
                return False
            self._pause_run.clear()
            self._stop_run.clear()
            self._running = True
            self._state = "running"
            self._auto_paused = False
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

    def pause(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            self._pause_run.set()
            self._auto_paused = True
            self._state = "pausing"
            return True

    def stop_current(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            self._stop_run.set()
            self._state = "stopping"
            return True

    def resume(self) -> bool:
        with self._lock:
            if self._running or self._state not in ("paused", "stopped"):
                return False
            self._auto_paused = False
        return self.trigger(source="resume")

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._running
            mode = self._mode
            state = self._state
            started_at = self._started_at
            error = self._error
            progress = list(self._progress[-8:])
            auto_paused = self._auto_paused
        return {
            "running": running,
            "state": state,
            "auto_paused": auto_paused,
            "auto_sync": bool(self.config.auto_sync),
            "can_pause": running and state == "running",
            "can_stop": running and state == "running",
            "can_resume": (not running) and state in ("paused", "stopped"),
            "mode": mode,
            "started_at": started_at,
            "error": error,
            "progress": progress,
            "last_run": read_json(self.config.last_run_path),
            "next_run_at": self._next_run_at,
        }

    def _run(self, *, dry_run: bool, force: bool) -> None:
        started = time.monotonic()
        stats: SyncStats | None = None
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
                        stop_event=self._stop_run,
                        pause_event=self._pause_run,
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
            try:
                notify.notify_run(
                    self.config.notify_channels,
                    self.config.notify_on,
                    stats or SyncStats(),
                    self._error,
                )
            except Exception:
                logger.warning("同步结果通知发送失败", exc_info=True)
            with self._lock:
                self._running = False
                self._mode = ""
                if stats is not None and stats.stopped:
                    self._state = "stopped"
                    self._auto_paused = False
                elif stats is not None and stats.paused:
                    self._state = "paused"
                    self._auto_paused = self._pause_run.is_set()
                else:
                    self._state = "idle"
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
            if not config.auto_sync:
                with self._lock:
                    self._next_run_at = None
                self._wake.wait(30)
                self._wake.clear()
                continue
            wait = max(1.0, next_at - time.time())
            if self._wake.wait(wait):
                self._wake.clear()
                next_at = time.time() + max(60, int(config.interval))
                self._next_run_at = datetime.fromtimestamp(next_at, timezone.utc).isoformat(timespec="seconds")
                continue
            if self._stop.is_set():
                break
            try:
                with self._lock:
                    auto_paused = self._auto_paused
                if config.auto_sync and self.sessions.has_session() and not auto_paused:
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
    updates: UpdateChecker = field(init=False)

    def __post_init__(self) -> None:
        self.updates = UpdateChecker(__version__)

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
        if method in {"POST", "PUT", "DELETE"} and environ.get("HTTP_X_NCM_CLOUD_SYNC") != "web":
            return self._json(start_response, "403 Forbidden", {"error": "缺少请求校验头"})

        try:
            if path == "/api/status" and method == "GET":
                return self._json(start_response, "200 OK", self._status())
            if path == "/api/update" and method == "GET":
                query = parse_qs(str(environ.get("QUERY_STRING") or ""))
                force = (query.get("force") or ["0"])[0].lower() in {"1", "true", "yes"}
                result = dict(self.updates.check(force=force))
                result["repo_url"] = REPO_URL
                result["releases_url"] = RELEASES_URL
                return self._json(start_response, "200 OK", result)
            if path == "/api/qr" and method == "POST":
                return self._json(start_response, "200 OK", self.login.start())
            if path == "/api/login/cookie" and method == "POST":
                body = self._body(environ)
                return self._json(
                    start_response,
                    "200 OK",
                    self.login.cookie_login(str(body.get("cookie") or "")),
                )
            if path == "/api/diagnostics" and method == "GET":
                return self._json(start_response, "200 OK", self._diagnostics())
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
            if path == "/api/sync/pause" and method == "POST":
                ok = self.sync.pause()
                return self._json(
                    start_response,
                    "200 OK" if ok else "409 Conflict",
                    {"ok": ok, "status": self.sync.status()},
                )
            if path == "/api/sync/stop" and method == "POST":
                ok = self.sync.stop_current()
                return self._json(
                    start_response,
                    "200 OK" if ok else "409 Conflict",
                    {"ok": ok, "status": self.sync.status()},
                )
            if path == "/api/sync/resume" and method == "POST":
                ok = self.sync.resume()
                return self._json(
                    start_response,
                    "200 OK" if ok else "409 Conflict",
                    {"ok": ok, "status": self.sync.status()},
                )
            if path == "/api/sync/status" and method == "GET":
                return self._json(start_response, "200 OK", self.sync.status())
            if path == "/api/problems" and method == "GET":
                query = parse_qs(str(environ.get("QUERY_STRING") or ""))
                kind = str((query.get("kind") or ["failed"])[0] or "failed")
                limit = _as_int((query.get("limit") or ["200"])[0], 200, 1, 500)
                with StateStore(self.config.db_path) as state:
                    counts = state.problem_counts()
                    items = state.problem_files(kind=kind, limit=limit)
                return self._json(start_response, "200 OK", {"kind": kind, "counts": counts, "items": items})
            if path == "/api/problems/retry" and method == "POST":
                body = self._body(environ)
                kind = str(body.get("kind") or "failed")
                with StateStore(self.config.db_path) as state:
                    reset = state.reset_problems(kind=kind)
                    counts = state.problem_counts()
                return self._json(start_response, "200 OK", {"ok": True, "reset": reset, "counts": counts})
            if path == "/api/notify/types" and method == "GET":
                return self._json(start_response, "200 OK", {"types": self._notify_types()})
            if path == "/api/notify/test" and method == "POST":
                body = self._body(environ)
                raw_channel = body.get("channel") if isinstance(body.get("channel"), dict) else body
                try:
                    channel = notify.normalize_channels([raw_channel])[0]
                    notify.send_test(channel)
                except Exception as exc:
                    return self._json(start_response, "400 Bad Request", {"ok": False, "error": str(exc)})
                return self._json(start_response, "200 OK", {"ok": True})
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
                return self._json(
                    start_response,
                    "200 OK",
                    {
                        "logs": read_log_tail(self.log_paths, lines),
                        "log_paths": [str(item) for item in self.log_paths],
                        "log_exists": any(item.is_file() for item in self.log_paths),
                    },
                )
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
        except NcmSyncError as exc:
            return self._json(start_response, "500 Internal Server Error", {"error": str(exc)})

    def _status(self) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("music_dirs"):
            settings = bootstrap_settings(self.config)
        cookies, profile = self.sessions.snapshot()
        stats: dict[str, int] = {}
        problem_counts: dict[str, int] = {"failed": 0, "permission": 0}
        try:
            with StateStore(self.config.db_path) as state:
                stats = state.stats()
                problem_counts = state.problem_counts()
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
                "total": sum(value for key, value in stats.items() if key != "ignored"),
                "uploaded": stats.get("uploaded", 0),
                "pending": stats.get("pending", 0),
                "failed": problem_counts.get("failed", 0),
                "permission": problem_counts.get("permission", 0),
                "missing": stats.get("missing", 0),
                "uploading": stats.get("uploading", 0),
                "skipped": stats.get("skipped_local", 0) + stats.get("skipped_remote", 0),
            },
            "sync": self.sync.status(),
        }

    def _diagnostics(self) -> dict[str, Any]:
        def path_info(path: Path) -> dict[str, Any]:
            entry: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
            if path.is_file():
                try:
                    entry["size"] = path.stat().st_size
                except OSError:
                    entry["size"] = None
            return entry

        return {
            "version": __version__,
            "data_dir": str(self.data_dir),
            "settings_path": str(self.settings.path),
            "log_paths": [path_info(item) for item in self.log_paths],
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": sys.platform,
            "cwd": os.getcwd(),
            "socket": os.environ.get("NCM_CLOUD_SYNC_SOCKET", ""),
            "env": {
                key: os.environ.get(key, "")
                for key in (
                    "TRIM_APPDEST",
                    "TRIM_PKGVAR",
                    "NCM_CLOUD_SYNC_DATA_DIR",
                    "NCM_CLOUD_SYNC_SETTINGS",
                    "NCM_CLOUD_SYNC_LOG_FILE",
                )
            },
            "logs": read_log_tail(self.log_paths, 120),
        }

    def _settings_payload(self) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("music_dirs"):
            settings = bootstrap_settings(self.config)
        return {
            "settings": settings,
            "music_dirs": [str(path) for path in self.config.music_dirs],
            "interval": self.config.interval,
            "rate_limit": self.config.rate_limit,
            "exclude_dirs": [str(path) for path in self.config.exclude_dirs],
            "auto_sync": self.config.auto_sync,
            "notify_on": self.config.notify_on,
            "notifications": list(self.config.notify_channels),
            "dry_run": self.config.dry_run,
            "log_level": self.config.log_level,
            "settings_path": str(self.settings.path),
        }

    @staticmethod
    def _notify_types() -> list[dict[str, Any]]:
        return [
            {
                "type": item,
                "label": notify.TYPE_LABELS[item],
                "fields": list(notify.FIELDS[item]),
                "required": list(notify.REQUIRED[item]),
                "secret": [name for name in notify.FIELDS[item] if name in notify.SECRET_FIELDS],
            }
            for item in notify.ALLOWED_TYPES
        ]

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
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    ring = _RingBufferHandler()
    ring.setFormatter(formatter)
    handlers.append(ring)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
            )
        except OSError:
            # 数据目录不可写时 Web UI 仍要能启动，日志退化为 stderr + 内存。
            logging.getLogger(__name__).warning("无法写入日志文件: %s", path, exc_info=True)
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
        os.environ.get("NCM_CLOUD_SYNC_SETTINGS") or (config.data_dir / "settings.json")
    ).expanduser()
    store = SettingsStore(settings_path)
    settings = store.load()
    if not settings.get("music_dirs"):
        settings = bootstrap_settings(config)
    apply_settings(config, settings)
    config.ensure_data_dir()
    log_path = Path(os.environ.get("NCM_CLOUD_SYNC_LOG_FILE") or (config.data_dir / "app.log"))
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
    allow_local = _as_bool(os.environ.get("NCM_CLOUD_SYNC_DEV"), False)
    app, sync = build_app(config, allow_local=allow_local)
    sync.start_scheduler()
    socket_path = os.environ.get("NCM_CLOUD_SYNC_SOCKET", "").strip()

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
            print(f"ncm-cloud-sync web listening on unix:{path}", flush=True)
            serve(app, unix_socket=str(path), unix_socket_perms="660", threads=8, channel_timeout=120)
        else:
            host = os.environ.get("NCM_CLOUD_SYNC_HOST", "127.0.0.1")
            port = _as_int(os.environ.get("NCM_CLOUD_SYNC_PORT", "8765"), 8765, 1, 65535)
            print(f"ncm-cloud-sync web listening on http://{host}:{port}", flush=True)
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

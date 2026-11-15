"""检查 GitHub Release 更新，不依赖第三方库。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/jiu-zui-90817/ncm-cloud-sync"
RELEASES_URL = REPO_URL + "/releases"
LATEST_RELEASE_URL = "https://api.github.com/repos/jiu-zui-90817/ncm-cloud-sync/releases/latest"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version(value: Any) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)*)", str(value or ""))
    if not match:
        return ()
    return tuple(int(item) for item in match.group(1).split("."))


def is_newer(latest: Any, current: Any) -> bool:
    newest = parse_version(latest)
    running = parse_version(current)
    if not newest or not running:
        return False
    length = max(len(newest), len(running))
    return newest + (0,) * (length - len(newest)) > running + (0,) * (length - len(running))


def fetch_latest_release(timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ncm-cloud-sync-update-check",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("GitHub 返回格式不正确")
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("GitHub 没有返回版本号")
    latest = tag[1:] if tag.lower().startswith("v") else tag
    notes = str(data.get("body") or "").strip()
    if len(notes) > 6000:
        notes = notes[:6000] + "\n…"
    return {
        "latest": latest,
        "release_url": str(data.get("html_url") or RELEASES_URL),
        "published_at": str(data.get("published_at") or ""),
        "notes": notes,
    }


class UpdateChecker:
    def __init__(
        self,
        current_version: str,
        *,
        fetch: Callable[[], dict[str, Any]] | None = None,
        ttl: float = 600.0,
    ) -> None:
        self.current_version = current_version
        self._fetch = fetch or fetch_latest_release
        self._ttl = max(1.0, float(ttl))
        self._error_ttl = min(60.0, self._ttl)
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    def _cached_result(self, force: bool) -> dict[str, Any] | None:
        with self._lock:
            if force or self._cached is None:
                return None
            age = time.monotonic() - self._cached_at
            limit = self._error_ttl if self._cached.get("error") else self._ttl
            if age < limit:
                return dict(self._cached)
        return None

    def _store(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._cached = dict(result)
            self._cached_at = time.monotonic()
        return result

    def check(self, force: bool = False) -> dict[str, Any]:
        cached = self._cached_result(force)
        if cached is not None:
            return cached
        try:
            data = self._fetch()
            if not isinstance(data, dict):
                raise ValueError("更新接口返回格式不正确")
            latest = str(data.get("latest") or "").strip()
            if not latest:
                raise ValueError("更新接口没有返回版本号")
            result = {
                "current": self.current_version,
                "latest": latest,
                "update_available": is_newer(latest, self.current_version),
                "release_url": str(data.get("release_url") or RELEASES_URL),
                "published_at": str(data.get("published_at") or ""),
                "notes": str(data.get("notes") or ""),
                "checked_at": _now(),
                "error": "",
            }
        except Exception as exc:
            logger.warning("检查更新失败: %s", exc)
            result = {
                "current": self.current_version,
                "latest": "",
                "update_available": False,
                "release_url": RELEASES_URL,
                "published_at": "",
                "notes": "",
                "checked_at": _now(),
                "error": f"检查更新失败：{exc}",
            }
        return self._store(result)

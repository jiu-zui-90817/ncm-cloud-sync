"""常驻轮询模式。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import CloudClient
from .config import Config
from .signin import maybe_daily_signin
from .state import StateStore
from .syncer import SyncStats, Syncer

logger = logging.getLogger(__name__)


def time_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_last_run(path: Path, stats: SyncStats, error: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"at": time_now(), "stats": stats.as_dict()}
    if error:
        payload["error"] = error
    fd, tmp_name = tempfile.mkstemp(prefix=".last-run-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def run_daemon(
    config: Config,
    client: CloudClient,
    state: StateStore,
    stop_event: threading.Event,
    *,
    force: bool = False,
    run_immediately: bool = True,
) -> None:
    interval = max(1, int(config.interval))
    logger.info("守护进程启动，轮询间隔 %d 秒（Ctrl+C 退出）", interval)
    def check_signin() -> None:
        if not config.daily_signin:
            return
        try:
            maybe_daily_signin(config, client, state)
        except Exception:
            logger.warning("随机签到检查失败，已忽略", exc_info=True)

    first = True
    while not stop_event.is_set():
        if not first or run_immediately:
            syncer = Syncer(
                config,
                client,
                state,
                force=force,
                stop_event=stop_event,
            )
            try:
                stats = syncer.run_once()
                write_last_run(config.last_run_path, stats)
            except Exception as exc:
                logger.exception("本轮同步失败")
                write_last_run(config.last_run_path, SyncStats(), error=str(exc))
        first = False
        deadline = time.monotonic() + interval
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if stop_event.wait(min(60.0, remaining)):
                break
            check_signin()
    logger.info("守护进程已退出")

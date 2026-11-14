"""同步编排：扫描、去重、上传、状态落库。"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .client import CloudClient, UploadResult
from .config import Config
from .errors import AuthError, RateLimitError
from .hashing import file_md5
from .scanner import AudioFile, scan
from .state import StateStore

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class SyncStats:
    scanned: int = 0
    up_to_date: int = 0
    uploaded: int = 0
    skipped_local: int = 0
    skipped_remote: int = 0
    planned: int = 0
    failed: int = 0
    permission_denied: int = 0
    deferred: int = 0
    ignored: int = 0
    missing: int = 0
    bytes_uploaded: int = 0
    duration: float = 0.0
    aborted: bool = False
    error: str = ""

    @property
    def skipped(self) -> int:
        return self.up_to_date + self.skipped_local + self.skipped_remote + self.ignored

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["skipped"] = self.skipped
        return data

    def summary(self) -> str:
        return (
            f"扫描 {self.scanned}，上传 {self.uploaded}，"
            f"已存在/跳过 {self.skipped}（本地重复 {self.skipped_local}，"
            f"云端已有 {self.skipped_remote}，未变更 {self.up_to_date}），"
            f"失败 {self.failed}"
            + (f"（其中无权限 {self.permission_denied}）" if self.permission_denied else "")
            + f"，缺失 {self.missing}，"
            f"上传 {self.bytes_uploaded / 1024 / 1024:.1f} MB，"
            f"耗时 {self.duration:.1f}s"
            + (f"；已暂停，剩余 {self.deferred} 首待下次" if self.deferred else "")
        )


class Syncer:
    def __init__(
        self,
        config: Config,
        client: CloudClient,
        state: StateStore,
        *,
        dry_run: bool = False,
        force: bool = False,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        stop_event: Any | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.state = state
        self.dry_run = dry_run
        self.force = force
        self.limit = limit
        self.progress = progress
        self.stop_event = stop_event

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress is None:
            return
        try:
            self.progress(event, payload)
        except Exception:
            logger.debug("进度回调失败", exc_info=True)

    def _stopped(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def run_once(self) -> SyncStats:
        started = time.monotonic()
        stats = SyncStats()
        run_id = self.state.start_run()
        logger.info("开始扫描: %s", ", ".join(str(item) for item in self.config.music_dirs))
        files = list(
            scan(
                self.config.music_dirs,
                self.config.extensions,
                self.config.exclude,
            )
        )
        known = {str(item.path) for item in files}
        logger.info("发现 %d 个候选音频文件", len(files))
        processed = 0
        try:
            for audio in files:
                if self._stopped():
                    stats.aborted = True
                    break
                if self.limit is not None and processed >= self.limit:
                    logger.info("已达到 --limit %d，提前结束", self.limit)
                    break
                processed += 1
                stats.scanned += 1
                try:
                    self._process(audio, stats)
                except AuthError:
                    stats.aborted = True
                    raise
                except RateLimitError as exc:
                    stats.aborted = True
                    stats.deferred = max(0, len(files) - processed + 1)
                    stats.error = f"网易云限流：{exc}。本次同步已暂停，稍后会自动继续。"
                    logger.warning("触发网易云限流，本次同步暂停: %s", exc)
                    if not self.dry_run:
                        try:
                            self.state.mark_pending(audio.path, str(exc))
                        except Exception:
                            logger.exception("写入待处理状态时出错: %s", audio.path)
                    break
                except PermissionError as exc:
                    stats.failed += 1
                    stats.permission_denied += 1
                    logger.warning(
                        "没有读取权限，跳过: %s（请给应用用户 wy-music-cloud 授予该目录读取权限）",
                        audio.path,
                    )
                    if not self.dry_run:
                        try:
                            self.state.mark_failed(
                                audio.path,
                                "没有读取权限，请在应用中心授权该目录，或给应用用户 wy-music-cloud 读权限",
                            )
                        except Exception:
                            logger.exception("写入失败状态时出错: %s", audio.path)
                except Exception as exc:
                    stats.failed += 1
                    logger.exception("处理文件失败: %s", audio.path)
                    if not self.dry_run:
                        try:
                            self.state.mark_failed(audio.path, str(exc))
                        except Exception:
                            logger.exception("写入失败状态时出错: %s", audio.path)
            self._check_missing(known, stats)
        except AuthError as exc:
            stats.duration = time.monotonic() - started
            self.state.finish_run(
                run_id,
                scanned=stats.scanned,
                uploaded=stats.uploaded,
                skipped=stats.skipped,
                failed=stats.failed,
                error=str(exc),
            )
            raise
        stats.duration = time.monotonic() - started
        self.state.finish_run(
            run_id,
            scanned=stats.scanned,
            uploaded=stats.uploaded,
            skipped=stats.skipped,
            failed=stats.failed,
            error=stats.error or None,
        )
        self._notify("done", stats.as_dict())
        logger.info("同步完成: %s", stats.summary())
        return stats

    def _process(self, audio: AudioFile, stats: SyncStats) -> None:
        record = self.state.get(audio.path)
        if (
            not self.force
            and record is not None
            and record.size == audio.size
            and record.mtime_ns == audio.mtime_ns
            and record.status in {"uploaded", "skipped_local"}
        ):
            stats.up_to_date += 1
            return

        if not self.dry_run:
            self.state.mark_seen(audio.path, audio.size, audio.mtime_ns)
            record = self.state.get(audio.path)

        if audio.size < self.config.min_size:
            stats.ignored += 1
            logger.info("文件小于 min_size，跳过: %s", audio.path)
            return

        if audio.suffix not in self.config.upload_extensions:
            stats.ignored += 1
            logger.info("扩展名 %s 不在 upload_extensions，跳过: %s", audio.suffix, audio.path)
            return

        if self.config.max_upload_size and audio.size > self.config.max_upload_size:
            stats.failed += 1
            message = f"文件超过 max_upload_size ({self.config.max_upload_size} 字节)"
            logger.warning("%s: %s", message, audio.path)
            if not self.dry_run:
                self.state.mark_failed(audio.path, message)
            return

        cached = (
            record.md5
            if record
            and record.md5
            and record.size == audio.size
            and record.mtime_ns == audio.mtime_ns
            else None
        )
        md5 = cached or file_md5(audio.path)
        if not self.dry_run and md5 != cached:
            self.state.set_md5(audio.path, md5)

        canonical = self.state.find_by_md5(md5, exclude_path=str(audio.path))
        if canonical is not None:
            stats.skipped_local += 1
            logger.info("本地重复，跳过: %s -> %s", audio.path, canonical.path)
            if not self.dry_run:
                self.state.mark_skipped(
                    audio.path,
                    status="skipped_local",
                    duplicate_of=canonical.path,
                    song_id=canonical.song_id,
                )
            self._notify(
                "skip_local",
                {"path": str(audio.path), "duplicate_of": canonical.path},
            )
            return

        if not self.dry_run:
            self.state.mark_uploading(audio.path)

        result: UploadResult = self.client.upload(
            audio.path,
            md5=md5,
            dry_run=self.dry_run,
        )

        if result.reason == "remote_exists":
            stats.skipped_remote += 1
            logger.info("云端已存在，跳过: %s", audio.path)
            if not self.dry_run:
                self.state.mark_skipped(
                    audio.path,
                    status="skipped_remote",
                    song_id=result.song_id,
                )
            self._notify(
                "skip_remote",
                {"path": str(audio.path), "song_id": result.song_id},
            )
            return

        if result.reason == "dry_run":
            stats.planned += 1
            logger.info("dry-run：待上传 %s", audio.path)
            self._notify("planned", {"path": str(audio.path)})
            return

        stats.uploaded += 1
        stats.bytes_uploaded += audio.size
        logger.info("上传成功: %s -> songId=%s", audio.path, result.song_id)
        if not self.dry_run:
            self.state.mark_uploaded(
                audio.path,
                song_id=result.song_id,
                cloud_name=result.cloud_name,
            )
        self._notify(
            "uploaded",
            {"path": str(audio.path), "song_id": result.song_id, "size": audio.size},
        )

    def _check_missing(self, known: set[str], stats: SyncStats) -> None:
        for record in list(self.state.missing_candidates(known)):
            if Path(record.path).exists():
                continue
            stats.missing += 1
            logger.warning("文件已不存在，标记 missing: %s", record.path)
            if not self.dry_run:
                self.state.mark_missing(record.path)

"""同步编排：扫描、去重、上传、状态落库。"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import CloudClient, UploadResult
from .config import Config
from .errors import AuthError, NcmSyncError, NosUnavailableError, RateLimitError
from .hashing import file_md5
from .scanner import AudioFile, path_is_excluded, scan_with_report
from .state import StateStore

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]


def _human_size(size: int) -> str:
    value = max(int(size or 0), 0)
    if value >= 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024 / 1024:.2f} GB"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


@dataclass
class SyncStats:
    scanned: int = 0
    processed: int = 0
    cloud_songs: int = 0
    preflight_ok: bool = False
    cloud_only: int = 0
    matched: int = 0
    to_upload: int = 0
    duplicate_local: int = 0
    scan_unsupported: int = 0
    scan_excluded: int = 0
    scan_unreadable: int = 0
    scan_missing_roots: int = 0
    unsupported_extensions: dict[str, int] = field(default_factory=dict)
    up_to_date: int = 0
    uploaded: int = 0
    linked: int = 0
    skipped_local: int = 0
    skipped_remote: int = 0
    failed: int = 0
    permission_denied: int = 0
    deferred: int = 0
    ignored: int = 0
    missing: int = 0
    bytes_uploaded: int = 0
    capacity_total: int = 0
    capacity_used: int = 0
    capacity_free: int = 0
    capacity_insufficient: int = 0
    duration: float = 0.0
    aborted: bool = False
    paused: bool = False
    stopped: bool = False
    error: str = ""
    mode: str = "sync"

    @property
    def skipped(self) -> int:
        return self.up_to_date + self.skipped_local + self.skipped_remote

    @property
    def filtered(self) -> int:
        return (
            self.scan_unsupported
            + self.scan_excluded
            + self.scan_unreadable
        )

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["skipped"] = self.skipped
        data["filtered"] = self.filtered
        return data

    def summary(self) -> str:
        if self.mode == "scan":
            return (
                f"仅扫描：本地候选 {self.scanned}"
                + (
                    f"（实际检查 {self.processed}）"
                    if self.processed != self.scanned
                    else ""
                )
                + f"，云盘 {self.cloud_songs} 首，待上传 {self.to_upload}，"
                f"云端独有 {self.cloud_only}，已匹配 {self.matched}，"
                f"本地重复 {self.duplicate_local}，失败 {self.failed}"
                + (f"（其中无权限 {self.permission_denied}）" if self.permission_denied else "")
                + (f"，忽略 {self.ignored}" if self.ignored else "")
                + f"，耗时 {self.duration:.1f}s"
                + (
                    f"；已终止，剩余 {self.deferred} 首待下次"
                    if self.stopped
                    else f"；已暂停，剩余 {self.deferred} 首待下次"
                    if self.paused
                    else ""
                )
            )
        return (
            f"扫描 {self.scanned}"
            + (
                f"（实际处理 {self.processed}）"
                if self.processed != self.scanned
                else ""
            )
            + "，"
            + (f"云盘 {self.cloud_songs} 首，" if self.cloud_songs else "")
            + f"上传 {self.uploaded}，"
            + (f"补建 {self.linked}，" if self.linked else "")
            + f"已存在/跳过 {self.skipped}（本地重复 {self.skipped_local}，"
            f"云端已有 {self.skipped_remote}，未变更 {self.up_to_date}），"
            f"失败 {self.failed}"
            + (f"（其中无权限 {self.permission_denied}）" if self.permission_denied else "")
            + (f"，忽略 {self.ignored}" if self.ignored else "")
            + (f"，扫描过滤 {self.filtered}" if self.filtered else "")
            + f"，缺失 {self.missing}，"
            f"上传 {self.bytes_uploaded / 1024 / 1024:.1f} MB，"
            f"耗时 {self.duration:.1f}s"
            + (
                f"；已终止，剩余 {self.deferred} 首待下次"
                if self.stopped
                else f"；已暂停，剩余 {self.deferred} 首待下次"
                if self.paused
                else f"；已中止，剩余 {self.deferred} 首待下次"
                if self.deferred
                else ""
            )
        )


class Syncer:
    def __init__(
        self,
        config: Config,
        client: CloudClient,
        state: StateStore,
        *,
        force: bool = False,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        stop_event: Any | None = None,
        pause_event: Any | None = None,
        scan_only: bool = False,
    ) -> None:
        self.config = config
        self.client = client
        self.state = state
        self.scan_only = scan_only
        self.force = force
        self.limit = limit
        self.progress = progress
        self.stop_event = stop_event
        self.pause_event = pause_event
        self._cloud_md5: dict[str, int] = {}
        self._items: list[dict[str, Any]] = []
        self.mode = "scan" if scan_only else "sync"

    def _record(
        self,
        kind: str,
        *,
        path: str | Path | None = None,
        name: str | None = None,
        detail: str | None = None,
        size: int | None = None,
    ) -> None:
        if len(self._items) >= 5000:
            return
        self._items.append(
            {
                "kind": kind,
                "path": str(path) if path is not None else None,
                "name": name,
                "detail": detail,
                "size": size,
            }
        )

    @staticmethod
    def _keep_run(stats: SyncStats) -> bool:
        """只保留有实际同步结果或错误的运行记录。"""
        if stats.error:
            return True
        if stats.mode == "scan":
            return bool(
                stats.to_upload
                or stats.failed
                or stats.cloud_only
                or stats.duplicate_local
            )
        return bool(
            stats.uploaded
            or stats.linked
            or stats.failed
            or stats.missing
            or stats.deferred
        )

    def _finish_run(self, run_id: int, stats: SyncStats) -> None:
        self.state.finish_run(
            run_id,
            scanned=stats.scanned,
            uploaded=stats.uploaded,
            skipped=stats.skipped,
            failed=stats.failed,
            error=stats.error or None,
            stats=stats.as_dict(),
            items=self._items,
            keep=self._keep_run(stats),
        )

    def _notify(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress is None:
            return
        try:
            self.progress(event, payload)
        except Exception:
            logger.debug("进度回调失败", exc_info=True)

    def _stopped(self) -> bool:
        return bool(self.stop_event and self.stop_event.is_set())

    def _paused(self) -> bool:
        return bool(self.pause_event and self.pause_event.is_set())

    def _under_music_dirs(self, path: str | Path) -> bool:
        key = os.path.normcase(os.path.abspath(str(path)))
        for root in self.config.music_dirs:
            root_key = os.path.normcase(os.path.abspath(str(root)))
            if key == root_key or key.startswith(root_key + os.sep):
                return True
        return False

    def _mark_ignored_excluded(self, known: set[str]) -> int:
        count = 0
        for stored in self.state.all_paths():
            if stored in known or not self._under_music_dirs(stored):
                continue
            if not path_is_excluded(
                stored,
                self.config.exclude,
                self.config.exclude_dirs,
                self.config.music_dirs,
            ):
                continue
            record = self.state.get(stored)
            if record is None or record.status == "ignored":
                continue
            self.state.mark_ignored(stored, "命中排除规则，已隐藏")
            count += 1
        if count:
            logger.info("已隐藏 %d 个被排除规则命中的历史记录", count)
        return count

    def _load_cloud(self, stats: SyncStats) -> None:
        """读取云盘列表；失败是硬错误，不能当作空云盘继续上传。"""
        self._cloud_md5 = {}
        loader = getattr(self.client, "cloud_song_md5_map", None)
        if not callable(loader):
            raise NcmSyncError("客户端不支持读取云盘列表")
        try:
            self._cloud_md5 = dict(loader() or {})
        except Exception as exc:
            raise NcmSyncError(f"读取云盘列表失败：{exc}") from exc
        stats.cloud_songs = int(
            getattr(self.client, "last_cloud_song_count", len(self._cloud_md5)) or 0
        )
        logger.info(
            "云盘已有 %d 首（%d 个不同 MD5），已用于同步前对账",
            stats.cloud_songs,
            len(self._cloud_md5),
        )
        self._load_capacity(stats)

    def _load_capacity(self, stats: SyncStats) -> None:
        """记录云盘容量；接口未返回容量时保持未知，不能误判为空间不足。"""
        capacity = getattr(self.client, "last_cloud_capacity", None)
        if not isinstance(capacity, dict):
            return
        try:
            total = int(capacity.get("total_bytes") or 0)
            used = int(capacity.get("used_bytes") or 0)
        except (TypeError, ValueError):
            return
        if total <= 0:
            return
        stats.capacity_total = total
        stats.capacity_used = max(used, 0)
        stats.capacity_free = max(total - stats.capacity_used, 0)
        logger.info(
            "云盘容量：已用 %s / %s，剩余 %s",
            _human_size(stats.capacity_used),
            _human_size(stats.capacity_total),
            _human_size(stats.capacity_free),
        )

    def _free_capacity(self, stats: SyncStats) -> int | None:
        if stats.capacity_total <= 0:
            return None
        used = stats.capacity_used + stats.bytes_uploaded
        return max(stats.capacity_total - used, 0)

    def _preflight(self, stats: SyncStats) -> None:
        """上传前检查登录态与上传查重接口；失败抛 NcmSyncError。"""
        checker = getattr(self.client, "preflight", None)
        if not callable(checker):
            stats.preflight_ok = True
            return
        try:
            detail = checker(self._cloud_md5) or {}
        except AuthError:
            raise
        except Exception as exc:
            raise NcmSyncError(f"上传查重接口不可用：{exc}") from exc
        stats.preflight_ok = True
        logger.info(
            "上传前接口检查通过：userId=%s，云盘 %s 首，查重探针 %s",
            detail.get("user_id"),
            detail.get("cloud_songs"),
            detail.get("probe_md5"),
        )

    def _abort_run(self, run_id: int, stats: SyncStats, started: float, message: str) -> SyncStats:
        stats.aborted = True
        stats.error = message
        stats.duration = time.monotonic() - started
        try:
            self._finish_run(run_id, stats)
        except Exception:
            logger.exception("写入运行记录失败")
        self._notify("done", stats.as_dict())
        logger.error(message)
        return stats

    def run_once(self) -> SyncStats:
        started = time.monotonic()
        stats = SyncStats()
        stats.mode = self.mode
        if self.scan_only:
            return self._run_scan_only(started, stats)
        run_id = self.state.start_run(mode=self.mode)
        logger.info("开始扫描: %s", ", ".join(str(item) for item in self.config.music_dirs))
        if self.config.exclude_dirs:
            logger.info(
                "排除目录 %d 个: %s",
                len(self.config.exclude_dirs),
                ", ".join(str(item) for item in self.config.exclude_dirs),
            )
        files, report = scan_with_report(
            self.config.music_dirs,
            self.config.extensions,
            self.config.exclude,
            self.config.exclude_dirs,
        )
        stats.scanned = len(files)
        stats.scan_unsupported = report.unsupported
        stats.scan_excluded = report.excluded + report.excluded_dirs
        stats.scan_unreadable = report.unreadable
        stats.scan_missing_roots = report.missing_roots
        stats.unsupported_extensions = dict(report.unsupported_suffixes)
        known = {str(item.path) for item in files}
        logger.info(
            "发现 %d 个候选音频文件；扫描过滤 %d（扩展名 %d，名称/目录排除 %d，不可读 %d），缺失音乐目录 %d",
            len(files),
            report.filtered,
            report.unsupported,
            report.excluded + report.excluded_dirs,
            report.unreadable,
            report.missing_roots,
        )
        try:
            self._load_cloud(stats)
            self._preflight(stats)
        except Exception as exc:
            return self._abort_run(run_id, stats, started, f"上传前接口检查失败：{exc}")
        if self._stopped():
            stats.stopped = True
            stats.deferred = len(files)
            return self._abort_run(
                run_id, stats, started, f"已终止本次同步，剩余 {stats.deferred} 首待下次。"
            )
        if self._paused():
            stats.paused = True
            stats.deferred = len(files)
            return self._abort_run(
                run_id, stats, started, f"已暂停本次同步，剩余 {stats.deferred} 首待下次。"
            )
        processed = 0
        try:
            self._mark_ignored_excluded(known)
            for audio in files:
                if self._stopped():
                    stats.aborted = True
                    stats.stopped = True
                    stats.deferred = max(0, len(files) - processed)
                    stats.error = f"已终止本次同步，剩余 {stats.deferred} 首待下次。"
                    logger.warning("收到终止请求，本次同步已终止，剩余 %d 首", stats.deferred)
                    break
                if self._paused():
                    stats.aborted = True
                    stats.paused = True
                    stats.deferred = max(0, len(files) - processed)
                    stats.error = f"已暂停本次同步，剩余 {stats.deferred} 首待下次。"
                    logger.info("收到暂停请求，本次同步已暂停，剩余 %d 首", stats.deferred)
                    break
                if self.limit is not None and processed >= self.limit:
                    logger.info("已达到 --limit %d，提前结束", self.limit)
                    break
                processed += 1
                stats.processed = processed
                try:
                    self._process(audio, stats)
                except AuthError:
                    stats.aborted = True
                    raise
                except RateLimitError as exc:
                    stats.aborted = True
                    stats.paused = True
                    stats.deferred = max(0, len(files) - processed + 1)
                    stats.error = f"网易云限流：{exc}。本次同步已暂停，稍后会自动继续。"
                    self._record(
                        "deferred",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=f"网易云限流：{exc}",
                    )
                    logger.warning("触发网易云限流，本次同步暂停: %s", exc)
                    try:
                        self.state.mark_pending(audio.path, str(exc))
                    except Exception:
                        logger.exception("写入待处理状态时出错: %s", audio.path)
                    break
                except NosUnavailableError as exc:
                    stats.aborted = True
                    stats.paused = True
                    stats.deferred = max(0, len(files) - processed + 1)
                    stats.error = (
                        f"无法连接网易云上传节点：{exc}。"
                        "本次同步已暂停，请检查 NAS 网络后重试。"
                    )
                    self._record(
                        "deferred",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=f"上传节点不可用：{exc}",
                    )
                    logger.warning("无法连接网易云上传节点，本次同步暂停: %s", exc)
                    try:
                        self.state.mark_pending(audio.path, str(exc))
                    except Exception:
                        logger.exception("写入待处理状态时出错: %s", audio.path)
                    break
                except PermissionError as exc:
                    stats.failed += 1
                    stats.permission_denied += 1
                    self._record(
                        "permission",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail="没有读取权限，请在应用中心授权该目录",
                    )
                    logger.warning(
                        "没有读取权限，跳过: %s（请给应用用户 ncm-cloud-sync 授予该目录读取权限）",
                        audio.path,
                    )
                    try:
                        self.state.mark_failed(
                            audio.path,
                            "没有读取权限，请在应用中心授权该目录，或给应用用户 ncm-cloud-sync 读权限",
                        )
                    except Exception:
                        logger.exception("写入失败状态时出错: %s", audio.path)
                except Exception as exc:
                    stats.failed += 1
                    self._record(
                        "failed",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=str(exc) or exc.__class__.__name__,
                    )
                    logger.exception("处理文件失败: %s", audio.path)
                    try:
                        self.state.mark_failed(audio.path, str(exc))
                    except Exception:
                        logger.exception("写入失败状态时出错: %s", audio.path)
            self._check_missing(known, stats)
        except AuthError as exc:
            stats.duration = time.monotonic() - started
            stats.error = str(exc)
            self._finish_run(run_id, stats)
            raise
        stats.duration = time.monotonic() - started
        self._finish_run(run_id, stats)
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

        self.state.mark_seen(audio.path, audio.size, audio.mtime_ns)
        record = self.state.get(audio.path)

        if audio.size < self.config.min_size:
            stats.ignored += 1
            reason = f"文件小于 min_size ({self.config.min_size} 字节)，已忽略"
            self._record(
                "ignored", path=audio.path, name=audio.path.name, size=audio.size, detail=reason
            )
            logger.info("%s: %s", reason, audio.path)
            self.state.mark_ignored(audio.path, reason)
            return

        if audio.suffix not in self.config.upload_extensions:
            stats.ignored += 1
            reason = f"扩展名 {audio.suffix} 不在 upload_extensions，已忽略"
            self._record(
                "ignored", path=audio.path, name=audio.path.name, size=audio.size, detail=reason
            )
            logger.info("%s: %s", reason, audio.path)
            self.state.mark_ignored(audio.path, reason)
            return

        if self.config.max_upload_size and audio.size > self.config.max_upload_size:
            stats.failed += 1
            message = f"文件超过 max_upload_size ({self.config.max_upload_size} 字节)"
            self._record(
                "failed", path=audio.path, name=audio.path.name, size=audio.size, detail=message
            )
            logger.warning("%s: %s", message, audio.path)
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
        if md5 != cached:
            self.state.set_md5(audio.path, md5)

        canonical = self.state.find_by_md5(md5, exclude_path=str(audio.path))
        if canonical is not None:
            stats.skipped_local += 1
            self._record(
                "skipped_local",
                path=audio.path,
                name=audio.path.name,
                size=audio.size,
                detail=f"与本地文件重复：{canonical.path}",
            )
            logger.info("本地重复，跳过: %s -> %s", audio.path, canonical.path)
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

        if md5 in self._cloud_md5:
            cloud_song_id = self._cloud_md5.get(md5) or None
            stats.skipped_remote += 1
            self._record(
                "skipped_remote",
                path=audio.path,
                name=audio.path.name,
                size=audio.size,
                detail=f"云盘已有相同内容（songId={cloud_song_id or '未知'}）",
            )
            logger.info("云盘已有相同内容，跳过: %s -> songId=%s", audio.path, cloud_song_id)
            self.state.mark_skipped(
                audio.path,
                status="skipped_remote",
                song_id=cloud_song_id,
            )
            self._notify(
                "skip_remote",
                {"path": str(audio.path), "song_id": cloud_song_id},
            )
            return

        free = self._free_capacity(stats)
        if free is not None and audio.size > free:
            stats.failed += 1
            stats.capacity_insufficient += 1
            message = (
                f"云盘剩余空间不足：文件 {_human_size(audio.size)}，"
                f"剩余 {_human_size(free)}"
            )
            self._record(
                "failed",
                path=audio.path,
                name=audio.path.name,
                size=audio.size,
                detail=message,
            )
            logger.warning("%s: %s", message, audio.path)
            self.state.mark_failed(audio.path, message)
            return

        self.state.mark_uploading(audio.path)

        result: UploadResult = self.client.upload(audio.path, md5=md5)

        if result.reason == "remote_exists":
            stats.skipped_remote += 1
            self._record(
                "skipped_remote",
                path=audio.path,
                name=audio.path.name,
                size=audio.size,
                detail=f"云端已存在（songId={result.song_id or '未知'}）",
            )
            logger.info("云端已存在，跳过: %s", audio.path)
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

        if result.reason == "linked":
            stats.linked += 1
            self._record(
                "linked",
                path=audio.path,
                name=audio.path.name,
                size=audio.size,
                detail=f"补建云盘条目成功（songId={result.song_id or '未知'}）",
            )
            logger.info("补建云盘条目成功: %s -> songId=%s", audio.path, result.song_id)
            self.state.mark_uploaded(
                audio.path,
                song_id=result.song_id,
                cloud_name=result.cloud_name,
            )
            self._notify(
                "linked",
                {"path": str(audio.path), "song_id": result.song_id},
            )
            return

        stats.uploaded += 1
        stats.bytes_uploaded += audio.size
        self._record(
            "uploaded",
            path=audio.path,
            name=audio.path.name,
            size=audio.size,
            detail=f"上传成功（songId={result.song_id or '未知'}）",
        )
        logger.info("上传成功: %s -> songId=%s", audio.path, result.song_id)
        self.state.mark_uploaded(
            audio.path,
            song_id=result.song_id,
            cloud_name=result.cloud_name,
        )
        self._notify(
            "uploaded",
            {"path": str(audio.path), "song_id": result.song_id, "size": audio.size},
        )

    def _cloud_index(self) -> dict[str, dict[str, Any]]:
        loader = getattr(self.client, "cloud_song_index", None)
        if callable(loader):
            data = loader() or {}
            return {
                str(md5).lower(): dict(value)
                for md5, value in data.items()
                if isinstance(value, dict)
            }
        mapping = self.client.cloud_song_md5_map()
        return {
            str(md5).lower(): {
                "md5": str(md5).lower(),
                "song_id": song_id,
                "name": "",
                "artist": "",
                "album": "",
                "size": None,
            }
            for md5, song_id in mapping.items()
        }

    def _run_scan_only(self, started: float, stats: SyncStats) -> SyncStats:
        """只读扫描：本地候选与云盘列表对账，不写 files 表、不上传。"""
        run_id = self.state.start_run(mode="scan")
        logger.info("开始仅扫描: %s", ", ".join(str(item) for item in self.config.music_dirs))
        try:
            files, report = scan_with_report(
                self.config.music_dirs,
                self.config.extensions,
                self.config.exclude,
                self.config.exclude_dirs,
            )
            stats.scanned = len(files)
            stats.scan_unsupported = report.unsupported
            stats.scan_excluded = report.excluded + report.excluded_dirs
            stats.scan_unreadable = report.unreadable
            stats.scan_missing_roots = report.missing_roots
            stats.unsupported_extensions = dict(report.unsupported_suffixes)
            self._notify("scan_local", {"total": len(files)})
            logger.info(
                "仅扫描：本地候选 %d 个，扫描过滤 %d（扩展名 %d，排除 %d，不可读 %d）",
                len(files),
                report.filtered,
                report.unsupported,
                report.excluded + report.excluded_dirs,
                report.unreadable,
            )
            cloud = self._cloud_index()
            stats.cloud_songs = int(
                getattr(self.client, "last_cloud_song_count", len(cloud)) or len(cloud)
            )
            self._notify("cloud_loaded", {"count": stats.cloud_songs})
            self._load_capacity(stats)
            logger.info("仅扫描：云盘 %d 首（%d 个不同 MD5）", stats.cloud_songs, len(cloud))

            local_md5: dict[str, str] = {}
            processed = 0
            for audio in files:
                if self._stopped():
                    stats.aborted = True
                    stats.stopped = True
                    stats.deferred = max(0, len(files) - processed)
                    stats.error = f"已终止本次仅扫描，剩余 {stats.deferred} 首未检查。"
                    break
                if self._paused():
                    stats.aborted = True
                    stats.paused = True
                    stats.deferred = max(0, len(files) - processed)
                    stats.error = f"已暂停本次仅扫描，剩余 {stats.deferred} 首未检查。"
                    break
                if self.limit is not None and processed >= self.limit:
                    logger.info("已达到 --limit %d，提前结束仅扫描", self.limit)
                    break
                processed += 1
                stats.processed = processed
                self._notify(
                    "scan",
                    {"path": str(audio.path), "index": processed, "total": len(files)},
                )

                if audio.size < self.config.min_size:
                    stats.ignored += 1
                    self._record(
                        "ignored",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=f"文件小于 min_size ({self.config.min_size} 字节)",
                    )
                    continue
                if audio.suffix not in self.config.upload_extensions:
                    stats.ignored += 1
                    self._record(
                        "ignored",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=f"扩展名 {audio.suffix} 不在 upload_extensions",
                    )
                    continue
                if self.config.max_upload_size and audio.size > self.config.max_upload_size:
                    stats.failed += 1
                    self._record(
                        "failed",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=f"文件超过 max_upload_size ({self.config.max_upload_size} 字节)",
                    )
                    continue

                record = self.state.get(audio.path)
                cached = (
                    record.md5
                    if record
                    and record.md5
                    and record.size == audio.size
                    and record.mtime_ns == audio.mtime_ns
                    else None
                )
                try:
                    md5 = cached or file_md5(audio.path)
                except PermissionError:
                    stats.failed += 1
                    stats.permission_denied += 1
                    self._record(
                        "permission",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail="没有读取权限，请在应用中心授权该目录",
                    )
                    continue
                except Exception as exc:
                    stats.failed += 1
                    self._record(
                        "failed",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=str(exc) or exc.__class__.__name__,
                    )
                    continue

                if md5 in local_md5:
                    stats.duplicate_local += 1
                    self._record(
                        "duplicate_local",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail=f"与本地文件重复：{local_md5[md5]}",
                    )
                    continue
                local_md5[md5] = str(audio.path)
                if md5 in cloud:
                    stats.matched += 1
                    self._record(
                        "matched",
                        path=audio.path,
                        name=audio.path.name,
                        size=audio.size,
                        detail="本地与云端 MD5 一致",
                    )
                    continue
                stats.to_upload += 1
                detail = "本地有，云端没有"
                if record is not None and record.status == "failed":
                    detail += "；本地记录显示上次同步失败"
                self._record(
                    "to_upload",
                    path=audio.path,
                    name=audio.path.name,
                    size=audio.size,
                    detail=detail,
                )

            for md5, info in cloud.items():
                if md5 in local_md5:
                    continue
                stats.cloud_only += 1
                detail_parts = [
                    str(info.get("artist") or "").strip(),
                    str(info.get("album") or "").strip(),
                ]
                detail = " / ".join(item for item in detail_parts if item)
                song_id = info.get("song_id")
                if song_id:
                    detail = (detail + f"（songId={song_id}）").strip()
                self._record(
                    "cloud_only",
                    name=str(info.get("name") or "" )[:500] or None,
                    size=info.get("size"),
                    detail=detail or None,
                )

            stats.duration = time.monotonic() - started
            self._finish_run(run_id, stats)
            self._notify("done", stats.as_dict())
            logger.info("仅扫描完成: %s", stats.summary())
            return stats
        except Exception as exc:
            stats.duration = time.monotonic() - started
            stats.error = stats.error or str(exc) or exc.__class__.__name__
            self._finish_run(run_id, stats)
            raise

    def _check_missing(self, known: set[str], stats: SyncStats) -> None:
        known_md5: dict[str, str] = {}
        for known_path in known:
            known_record = self.state.get(known_path)
            if (
                known_record is not None
                and known_record.md5
                and known_record.status in {"uploaded", "skipped_local", "skipped_remote"}
            ):
                known_md5.setdefault(str(known_record.md5), known_path)
        for record in list(self.state.missing_candidates(known)):
            if Path(record.path).exists():
                continue
            moved_to = known_md5.get(str(record.md5 or ""))
            if moved_to and moved_to != record.path:
                self._record(
                    "moved",
                    path=record.path,
                    name=Path(record.path).name,
                    detail=f"已移动到 {moved_to}",
                )
                logger.info("文件已移动，更新旧记录: %s -> %s", record.path, moved_to)
                self.state.mark_moved(record.path, moved_to)
                continue
            stats.missing += 1
            self._record(
                "missing",
                path=record.path,
                name=Path(record.path).name,
                detail="本地文件已不存在",
            )
            logger.warning("文件已不存在，标记 missing: %s", record.path)
            self.state.mark_missing(record.path)

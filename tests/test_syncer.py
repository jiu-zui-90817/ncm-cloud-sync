from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

import pytest

from ncm_cloud_sync.client import UploadResult
from ncm_cloud_sync.config import Config
from ncm_cloud_sync.errors import NosUnavailableError, RateLimitError, UploadError
from ncm_cloud_sync.state import StateStore
from ncm_cloud_sync.syncer import Syncer


class FakeClient:
    def __init__(self, outcomes=None, cloud=None) -> None:
        self.outcomes = outcomes or {}
        self.cloud = cloud or {}
        self.calls: list[tuple[str, bool]] = []

    def cloud_song_md5_map(self) -> dict[str, int]:
        return dict(self.cloud)

    def upload(self, path, *, md5=None, dry_run=False):
        self.calls.append((Path(path).name, dry_run))
        if dry_run:
            return UploadResult(False, reason="dry_run")
        outcome = self.outcomes.get(Path(path).name)
        if outcome is None:
            return UploadResult(True, song_id=len(self.calls))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_config(tmp_path: Path, music: Path) -> Config:
    return Config(
        music_dirs=[music],
        data_dir=tmp_path / "data",
        extensions=("mp3",),
        upload_extensions=("mp3",),
        interval=1,
        rate_limit=0,
    )


def test_upload_then_up_to_date(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        first = Syncer(config, client, state).run_once()
        assert first.uploaded == 1
        assert state.get(music / "a.mp3").status == "uploaded"

        second = Syncer(config, client, state).run_once()
        assert second.uploaded == 0
        assert second.up_to_date == 1
        assert len(client.calls) == 1


def test_local_duplicate_skipped(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"same" * 100)
    (music / "b.mp3").write_bytes(b"same" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.uploaded == 1
        assert stats.skipped_local == 1
        assert len(client.calls) == 1
        assert state.get(music / "b.mp3").status == "skipped_local"


def test_remote_exists_skipped(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    client = FakeClient({"a.mp3": UploadResult(False, song_id=7, reason="remote_exists")})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.skipped_remote == 1
        record = state.get(music / "a.mp3")
        assert record.status == "skipped_remote"
        assert record.song_id == 7


def test_skipped_remote_record_is_rechecked(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    first = FakeClient({"a.mp3": UploadResult(False, song_id=7, reason="remote_exists")})
    with StateStore(config.db_path) as state:
        assert Syncer(config, first, state).run_once().skipped_remote == 1
        assert state.get(music / "a.mp3").status == "skipped_remote"

        second = FakeClient()
        stats = Syncer(config, second, state).run_once()
        assert stats.uploaded == 1
        assert len(second.calls) == 1


def test_cloud_reconciliation_skips_before_upload(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    md5 = hashlib.md5(b"a").hexdigest()
    config = make_config(tmp_path, music)
    client = FakeClient(cloud={md5: 123})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.skipped_remote == 1
        assert client.calls == []
        record = state.get(music / "a.mp3")
        assert record.status == "skipped_remote"
        assert record.song_id == 123


def test_linked_metadata_is_not_counted_as_upload(tmp_path: Path, caplog) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    client = FakeClient({"a.mp3": UploadResult(True, song_id=9, reason="linked")})
    with StateStore(config.db_path) as state, caplog.at_level(logging.INFO):
        stats = Syncer(config, client, state).run_once()
        assert stats.linked == 1
        assert stats.uploaded == 0
        assert stats.bytes_uploaded == 0
        assert "补建云盘条目成功" in caplog.text
        assert state.get(music / "a.mp3").status == "uploaded"


def test_failure_is_isolated(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    (music / "b.mp3").write_bytes(b"b")
    config = make_config(tmp_path, music)
    client = FakeClient({"a.mp3": UploadError("boom")})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.failed == 1
        assert stats.uploaded == 1
        assert state.get(music / "a.mp3").status == "failed"
        assert state.get(music / "b.mp3").status == "uploaded"


def test_dry_run_does_not_persist_uploaded(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state, dry_run=True).run_once()
        assert stats.planned == 1
        assert stats.uploaded == 0
        assert state.get(music / "a.mp3") is None


def test_missing_file_marked(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a")
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        Syncer(config, client, state).run_once()
        target.unlink()
        stats = Syncer(config, client, state).run_once()
        assert stats.missing == 1
        assert state.get(target).status == "missing"


def test_limit(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    (music / "b.mp3").write_bytes(b"b")
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state, limit=1).run_once()
        assert stats.scanned == 2
        assert stats.processed == 1
        assert len(client.calls) == 1


def test_rate_limit_aborts_run(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (music / name).write_bytes(name.encode())
    config = make_config(tmp_path, music)
    client = FakeClient({"a.mp3": RateLimitError("操作频繁，请稍候再试", code=405)})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.aborted is True
        assert stats.failed == 0
        assert stats.scanned == 3
        assert stats.processed == 1
        assert stats.deferred == 3
        assert "405" in stats.error
        assert state.get(music / "a.mp3").status == "pending"
        assert len(client.calls) == 1


def test_pause_event_pauses_run(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (music / name).write_bytes(name.encode())
    config = make_config(tmp_path, music)
    client = FakeClient()
    pause = threading.Event()
    pause.set()
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state, pause_event=pause).run_once()
        assert stats.paused is True
        assert stats.stopped is False
        assert stats.aborted is True
        assert stats.scanned == 3
        assert stats.processed == 0
        assert stats.deferred == 3
        assert client.calls == []
        assert "已暂停" in stats.summary()


def test_stop_event_stops_run(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    for name in ("a.mp3", "b.mp3"):
        (music / name).write_bytes(name.encode())
    config = make_config(tmp_path, music)
    client = FakeClient()
    stop = threading.Event()
    stop.set()
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state, stop_event=stop).run_once()
        assert stats.stopped is True
        assert stats.paused is False
        assert stats.aborted is True
        assert stats.scanned == 2
        assert stats.processed == 0
        assert stats.deferred == 2
        assert "已终止" in stats.error
        assert "已终止" in stats.summary()
        assert client.calls == []


def test_nos_unavailable_aborts_run(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (music / name).write_bytes(name.encode())
    config = make_config(tmp_path, music)
    client = FakeClient({"a.mp3": NosUnavailableError("无法获取网易云上传节点；连接被拒绝")})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.aborted is True
        assert stats.failed == 0
        assert stats.deferred == 3
        assert "无法连接网易云上传节点" in stats.error
        assert state.get(music / "a.mp3").status == "pending"
        assert len(client.calls) == 1


def test_permission_error_is_counted(tmp_path: Path, monkeypatch) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a")
    config = make_config(tmp_path, music)

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("ncm_cloud_sync.syncer.file_md5", denied)
    with StateStore(config.db_path) as state:
        stats = Syncer(config, FakeClient(), state).run_once()
        record = state.get(target)
        assert stats.failed == 1
        assert stats.permission_denied == 1
        assert record is not None and record.status == "failed"
        assert "没有读取权限" in (record.last_error or "")


def test_small_and_unsupported_files_are_marked_ignored(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    small = music / "small.mp3"
    small.write_bytes(b"a")
    unsupported = music / "other.mp3"
    unsupported.write_bytes(b"b" * 100)
    config = make_config(tmp_path, music)
    config.min_size = 10
    config.upload_extensions = ("flac",)
    with StateStore(config.db_path) as state:
        stats = Syncer(config, FakeClient(), state).run_once()
        assert stats.ignored == 2
        assert stats.skipped == 0
        assert state.get(small).status == "ignored"
        assert state.get(unsupported).status == "ignored"


def test_excluded_history_is_hidden_not_missing(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    keep = music / "keep.mp3"
    keep.write_bytes(b"a" * 100)
    excluded = music / "有声书"
    excluded.mkdir()
    old = excluded / "old.mp3"
    old.write_bytes(b"b" * 100)
    config = make_config(tmp_path, music)
    config.exclude_dirs = (excluded,)
    with StateStore(config.db_path) as state:
        state.mark_seen(old, 100, 1)
        state.mark_uploaded(old, song_id=1, cloud_name="old")
        stats = Syncer(config, FakeClient(), state).run_once()
        assert state.get(old).status == "ignored"
        assert stats.missing == 0
        assert state.get(keep).status == "uploaded"

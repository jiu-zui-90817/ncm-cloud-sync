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
    def __init__(
        self,
        outcomes=None,
        cloud=None,
        cloud_index=None,
        preflight_error=None,
        cloud_error=None,
        capacity=None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.cloud = cloud or {}
        self.cloud_index = cloud_index or {}
        self.preflight_error = preflight_error
        self.cloud_error = cloud_error
        self.last_cloud_capacity = capacity or {}
        self.calls: list[str] = []

    def preflight(self, index):
        if self.preflight_error is not None:
            raise self.preflight_error
        return {"user_id": 1, "cloud_songs": len(self.cloud), "need_upload": True}

    def cloud_song_md5_map(self) -> dict[str, int]:
        if self.cloud_error is not None:
            raise self.cloud_error
        return dict(self.cloud)

    def cloud_song_index(self) -> dict[str, dict]:
        return dict(self.cloud_index)

    def upload(self, path, *, md5=None):
        self.calls.append(Path(path).name)
        outcome = self.outcomes.get(Path(path).name)
        if isinstance(outcome, Exception):
            raise outcome
        if md5:
            # 模拟云盘在成功上传/补建/查重命中后出现的 MD5，供下一轮 up_to_date 判断。
            self.cloud[str(md5).lower()] = len(self.calls)
        if outcome is None:
            return UploadResult(True, song_id=len(self.calls))
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
        # 第二次没有任何同步动作，不保留空的运行记录
        assert len(state.recent_runs(10)) == 1


def test_up_to_date_rechecks_cloud_md5(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    md5 = hashlib.md5(b"a" * 100).hexdigest()
    with StateStore(config.db_path) as state:
        first = Syncer(config, client, state).run_once()
        assert first.uploaded == 1
        assert md5 in client.cloud

        # 云盘里的条目被删掉/换掉后，本地记录仍然是 uploaded；
        # 不能仅凭 size+mtime 走 up_to_date，否则永远不会重新上传。
        client.cloud.clear()
        second = Syncer(config, client, state).run_once()
        assert second.uploaded == 1
        assert second.up_to_date == 0
        assert client.calls == ["a.mp3", "a.mp3"]


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


def test_capacity_insufficient_blocks_upload(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient(
        capacity={
            "supported": True,
            "total_bytes": 50,
            "used_bytes": 10,
            "free_bytes": 40,
        }
    )
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.failed == 1
        assert stats.capacity_insufficient == 1
        assert stats.capacity_total == 50
        assert stats.capacity_free == 40
        assert client.calls == []
        assert state.get(music / "a.mp3").status == "failed"


def test_capacity_enough_allows_upload(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient(
        capacity={
            "supported": True,
            "total_bytes": 1000,
            "used_bytes": 100,
            "free_bytes": 900,
        }
    )
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.uploaded == 1
        assert stats.capacity_insufficient == 0
        assert client.calls == ["a.mp3"]


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


def test_preflight_failure_aborts_before_upload(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    client = FakeClient(preflight_error=UploadError("上传查重接口 400"))
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.aborted is True
        assert stats.preflight_ok is False
        assert "上传前接口检查失败" in stats.error
        assert "上传查重接口 400" in stats.error
        assert stats.uploaded == 0
        assert client.calls == []
        assert state.get(music / "a.mp3") is None
        run = state.recent_runs(1)[0]
        assert run["error"] and "上传前接口检查失败" in run["error"]


def test_cloud_list_failure_aborts_before_upload(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    client = FakeClient(cloud_error=UploadError("cloud/get 500"))
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.aborted is True
        assert stats.preflight_ok is False
        assert "上传前接口检查失败" in stats.error
        assert "读取云盘列表失败" in stats.error
        assert "cloud/get 500" in stats.error
        assert stats.uploaded == 0
        assert client.calls == []
        assert state.get(music / "a.mp3") is None
        run = state.recent_runs(1)[0]
        assert run["error"] and "读取云盘列表失败" in run["error"]


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


def test_moved_file_not_marked_missing(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    old = music / "a.mp3"
    new = music / "b.mp3"
    old.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        assert Syncer(config, client, state).run_once().uploaded == 1
        old.rename(new)
        stats = Syncer(config, client, state).run_once()
        assert stats.missing == 0
        assert stats.skipped_local == 1
        assert state.get(old).status == "moved"
        assert state.get(new).status == "skipped_local"
        assert client.calls == ["a.mp3"]


def test_existing_missing_record_is_reconciled_after_move(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    old = music / "a.mp3"
    new = music / "b.mp3"
    payload = b"a" * 100
    old.write_bytes(payload)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        assert Syncer(config, client, state).run_once().uploaded == 1
        old.unlink()
        assert Syncer(config, client, state).run_once().missing == 1
        assert state.get(old).status == "missing"

        client.cloud = {hashlib.md5(payload).hexdigest(): 1}
        new.write_bytes(payload)
        stats = Syncer(config, client, state).run_once()
        assert stats.missing == 0
        assert stats.moved == 1
        assert state.get(old).status == "moved"
        assert state.get(new).status == "skipped_remote"
        assert client.calls == ["a.mp3"]


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


def test_scan_only_reports_local_cloud_diff(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    (music / "b.mp3").write_bytes(b"b")
    (music / "c.mp3").write_bytes(b"a")
    config = make_config(tmp_path, music)
    md5_b = hashlib.md5(b"b").hexdigest()
    md5_cloud = hashlib.md5(b"cloud").hexdigest()
    client = FakeClient(
        cloud_index={
            md5_b: {"md5": md5_b, "song_id": 2, "name": "b", "artist": "x", "album": "y", "size": 1},
            md5_cloud: {"md5": md5_cloud, "song_id": 3, "name": "cloud", "artist": "", "album": "", "size": 1},
        }
    )
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state, scan_only=True).run_once()
        assert stats.mode == "scan"
        assert stats.scanned == 3
        assert stats.processed == 3
        assert stats.to_upload == 1
        assert stats.matched == 1
        assert stats.duplicate_local == 1
        assert stats.cloud_only == 1
        assert client.calls == []
        assert state.get(music / "a.mp3") is None

        runs = state.recent_runs(1)
        assert runs[0]["mode"] == "scan"
        detail = state.run_detail(runs[0]["id"])
        assert detail is not None
        assert detail["counts"] == {"to_upload": 1, "matched": 1, "duplicate_local": 1, "cloud_only": 1}


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

def test_post_sync_delete_removes_local_after_cloud_confirmed(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    config.post_sync_action = "delete"
    with StateStore(config.db_path) as state:
        stats = Syncer(config, FakeClient(), state).run_once()
        assert stats.uploaded == 1
        assert stats.deleted == 1
        assert not target.exists()
        record = state.get(target)
        assert record is not None and record.status == "deleted"


def test_post_sync_move_archives_local_file(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    archive = tmp_path / "archive"
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    config.post_sync_action = "move"
    config.move_target = archive
    with StateStore(config.db_path) as state:
        stats = Syncer(config, FakeClient(), state).run_once()
        assert stats.uploaded == 1
        assert stats.moved_after_sync == 1
        assert not target.exists()
        assert archive.is_dir()
        assert (archive / "a.mp3").exists()
        record = state.get(target)
        assert record is not None and record.status == "archived"
        assert str(archive / "a.mp3") in (record.last_error or "")


def test_post_sync_delete_keeps_already_uploaded_file(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        assert Syncer(config, client, state).run_once().uploaded == 1
        config.post_sync_action = "delete"
        stats = Syncer(config, client, state).run_once()
        assert stats.up_to_date == 1
        assert stats.deleted == 0
        assert target.exists()
        record = state.get(target)
        assert record is not None and record.status == "uploaded"


def test_post_sync_move_keeps_already_uploaded_file(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    archive = tmp_path / "archive"
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        assert Syncer(config, client, state).run_once().uploaded == 1
        config.post_sync_action = "move"
        config.move_target = archive
        stats = Syncer(config, client, state).run_once()
        assert stats.up_to_date == 1
        assert stats.moved_after_sync == 0
        assert target.exists()
        assert not archive.exists()


def test_post_sync_delete_does_not_touch_remote_duplicate(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    md5 = hashlib.md5(b"a" * 100).hexdigest()
    config = make_config(tmp_path, music)
    config.post_sync_action = "delete"
    client = FakeClient(cloud={md5: 7})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.skipped_remote == 1
        assert stats.deleted == 0
        assert target.exists()


def test_post_sync_delete_does_not_touch_remote_exists_file(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    config.post_sync_action = "delete"
    client = FakeClient({"a.mp3": UploadResult(False, song_id=7, reason="remote_exists")})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.skipped_remote == 1
        assert stats.deleted == 0
        assert target.exists()


def test_post_sync_delete_removes_linked_file(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    config.post_sync_action = "delete"
    client = FakeClient({"a.mp3": UploadResult(False, song_id=7, reason="linked")})
    with StateStore(config.db_path) as state:
        stats = Syncer(config, client, state).run_once()
        assert stats.linked == 1
        assert stats.deleted == 1
        assert not target.exists()
        record = state.get(target)
        assert record is not None and record.status == "deleted"


def test_archive_inside_music_dir_is_not_scanned_again(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    archive = music / "archive"
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    config.post_sync_action = "move"
    config.move_target = archive
    client = FakeClient()
    with StateStore(config.db_path) as state:
        first = Syncer(config, client, state).run_once()
        assert first.moved_after_sync == 1
        assert (archive / "a.mp3").exists()

        second = Syncer(config, client, state).run_once()
        assert second.scanned == 0
        assert len(client.calls) == 1
        assert [item.name for item in archive.iterdir()] == ["a.mp3"]


def test_missing_is_archived_when_local_and_cloud_are_gone(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    client = FakeClient()
    with StateStore(config.db_path) as state:
        assert Syncer(config, client, state).run_once().uploaded == 1
        target.unlink()
        client.cloud.clear()
        stats = Syncer(config, client, state).run_once()
        assert stats.missing == 0
        record = state.get(target)
        assert record is not None and record.status == "deleted"
        assert "本地和云盘都已不存在" in (record.last_error or "")


def test_missing_check_can_be_disabled(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    target = music / "a.mp3"
    target.write_bytes(b"a" * 100)
    config = make_config(tmp_path, music)
    config.check_missing = False
    client = FakeClient()
    with StateStore(config.db_path) as state:
        assert Syncer(config, client, state).run_once().uploaded == 1
        target.unlink()
        stats = Syncer(config, client, state).run_once()
        assert stats.missing == 0
        record = state.get(target)
        assert record is not None and record.status == "uploaded"

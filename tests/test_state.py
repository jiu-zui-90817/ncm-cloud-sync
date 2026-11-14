from __future__ import annotations

from pathlib import Path

from wy_music_cloud.state import StateStore


def test_seen_uploaded_and_mtime_change(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("a.mp3", 100, 1)
        assert state.needs_processing("a.mp3", 100, 1) is True
        state.set_md5("a.mp3", "abc")
        state.mark_uploaded("a.mp3", song_id=42, cloud_name="a")
        assert state.is_done("a.mp3", 100, 1) is True
        assert state.needs_processing("a.mp3", 100, 1) is False
        assert state.needs_processing("a.mp3", 100, 2) is True
        assert state.find_by_md5("abc").song_id == 42


def test_mark_seen_invalidates_cached_md5_on_change(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("a.mp3", 100, 1)
        state.set_md5("a.mp3", "abc")
        state.mark_uploaded("a.mp3", song_id=42, cloud_name="a")
        # 未变化时保留缓存，避免重复计算 MD5
        state.mark_seen("a.mp3", 100, 1)
        assert state.get("a.mp3").md5 == "abc"
        # 文件内容/时间变化后必须让旧 MD5 失效
        state.mark_seen("a.mp3", 100, 2)
        record = state.get("a.mp3")
        assert record.md5 is None
        assert record.status == "pending"


def test_failed_can_retry_and_reset(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("a.mp3", 100, 1)
        state.mark_failed("a.mp3", "boom")
        assert state.needs_processing("a.mp3", 100, 1) is True
        assert state.reset_failed() == 1
        assert state.get("a.mp3").status == "pending"


def test_missing_detection(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("gone.mp3", 1, 1)
        state.mark_uploaded("gone.mp3", song_id=1, cloud_name="x")
        assert [r.path for r in state.missing_candidates(set())] == ["gone.mp3"]


def test_runs(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        run_id = state.start_run()
        state.finish_run(run_id, scanned=2, uploaded=1, skipped=1, failed=0)
        runs = state.recent_runs()
        assert runs[0]["scanned"] == 2

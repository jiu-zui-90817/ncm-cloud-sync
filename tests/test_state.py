from __future__ import annotations

from pathlib import Path

from ncm_cloud_sync.state import StateStore


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


def test_runs_keep_only_recent_five(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        for index in range(6):
            run_id = state.start_run()
            state.finish_run(run_id, scanned=index, uploaded=index)
        runs = state.recent_runs(10)
        assert len(runs) == 5
        assert [run["uploaded"] for run in runs] == [5, 4, 3, 2, 1]


def test_file_list_and_counts(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("uploaded.mp3", 100, 1)
        state.mark_uploaded("uploaded.mp3", song_id=1, cloud_name="uploaded")
        state.mark_seen("pending.mp3", 100, 1)
        state.mark_seen("missing.mp3", 100, 1)
        state.mark_missing("missing.mp3")
        state.mark_seen("local.mp3", 100, 1)
        state.mark_skipped("local.mp3", status="skipped_local")
        state.mark_seen("remote.mp3", 100, 1)
        state.mark_skipped("remote.mp3", status="skipped_remote")
        state.mark_seen("denied.mp3", 100, 1)
        state.mark_failed("denied.mp3", "读取文件失败：没有读取权限")

        counts = state.file_counts()
        assert counts["uploaded"] == 1
        assert counts["pending"] == 1
        assert counts["missing"] == 1
        assert counts["skipped"] == 2
        assert counts["permission"] == 1

        uploaded = state.file_list("uploaded")
        assert uploaded["total"] == 1
        assert uploaded["items"][0]["name"] == "uploaded.mp3"
        skipped = state.file_list("skipped")
        assert skipped["total"] == 2
        permission = state.file_list("failed")
        assert permission["items"][0]["kind"] == "permission"


def test_finish_run_can_discard_noop_run(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        run_id = state.start_run()
        state.finish_run(run_id, scanned=3, skipped=3, keep=False)
        assert state.recent_runs(10) == []


def test_problem_counts_files_and_reset(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("ok.mp3", 100, 1)
        state.mark_failed("ok.mp3", "网络错误")
        state.mark_seen("denied.mp3", 100, 1)
        state.mark_failed("denied.mp3", "读取文件失败：没有读取权限")

        assert state.problem_counts() == {"failed": 1, "permission": 1}
        failed = state.problem_files(kind="failed")
        assert [item["path"] for item in failed] == ["ok.mp3"]
        denied = state.problem_files(kind="permission")
        assert [item["path"] for item in denied] == ["denied.mp3"]
        assert "没有读取权限" in denied[0]["last_error"]
        assert denied[0]["kind"] == "permission"
        assert failed[0]["kind"] == "failed"
        assert {item["path"] for item in state.problem_files(kind="all")} == {"ok.mp3", "denied.mp3"}

        assert state.reset_problems(kind="permission") == 1
        assert state.problem_counts() == {"failed": 1, "permission": 0}
        assert state.get("denied.mp3").status == "pending"


def test_mark_ignored_and_all_paths(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        state.mark_seen("hidden.mp3", 10, 1)
        state.mark_ignored("hidden.mp3", "命中排除规则，已隐藏")
        record = state.get("hidden.mp3")
        assert record.status == "ignored"
        assert "排除规则" in record.last_error
        assert state.stats()["ignored"] == 1
        assert "hidden.mp3" in state.all_paths()


def test_run_detail_and_scan_mode(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        run_id = state.start_run("scan")
        state.finish_run(
            run_id,
            scanned=3,
            stats={"mode": "scan", "to_upload": 2, "cloud_only": 1},
            items=[
                {"kind": "to_upload", "path": "a.mp3", "name": "a.mp3", "detail": "待上传", "size": 10},
                {"kind": "to_upload", "path": "b.mp3", "name": "b.mp3", "detail": "待上传", "size": 20},
                {"kind": "cloud_only", "name": "c", "detail": "仅云端"},
            ],
        )
        runs = state.recent_runs(1)
        assert runs[0]["mode"] == "scan"
        assert runs[0]["stats"]["to_upload"] == 2

        detail = state.run_detail(run_id)
        assert detail is not None
        assert detail["kind"] == "all"
        assert detail["total"] == 3
        assert detail["counts"] == {"to_upload": 2, "cloud_only": 1}
        assert [item["name"] for item in detail["items"]] == ["a.mp3", "b.mp3", "c"]

        only_cloud = state.run_detail(run_id, kind="cloud_only")
        assert only_cloud is not None
        assert only_cloud["total"] == 1
        assert [item["kind"] for item in only_cloud["items"]] == ["cloud_only"]

        page = state.run_detail(run_id, limit=1, offset=1)
        assert page is not None
        assert page["total"] == 3
        assert [item["name"] for item in page["items"]] == ["b.mp3"]
        assert state.run_detail(99999) is None


def test_old_run_items_are_pruned_with_runs(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as state:
        for index in range(6):
            run_id = state.start_run()
            state.finish_run(
                run_id,
                items=[{"kind": "uploaded", "name": f"{index}.mp3"}],
            )
        runs = state.recent_runs(10)
        assert len(runs) == 5
        assert [run["id"] for run in runs] == [6, 5, 4, 3, 2]
        rows = state.conn.execute("SELECT COUNT(*) FROM run_items").fetchone()[0]
        assert rows == 5

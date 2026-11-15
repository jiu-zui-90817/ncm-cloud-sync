from __future__ import annotations

from pathlib import Path

import ncm_cloud_sync.permissions as permissions
from ncm_cloud_sync.permissions import audit_permissions


def _music(tmp_path: Path) -> Path:
    root = tmp_path / "music"
    (root / "album").mkdir(parents=True)
    (root / "album" / "a.mp3").write_bytes(b"a")
    (root / "album" / "b.flac").write_bytes(b"b")
    (root / "album" / "cover.jpg").write_bytes(b"c")
    return root


def test_audit_reports_readable_library(tmp_path: Path, monkeypatch) -> None:
    root = _music(tmp_path)
    monkeypatch.setattr(permissions, "_can_read_dir", lambda path: True)
    monkeypatch.setattr(permissions, "_can_read_file", lambda path: True)

    report = audit_permissions([root], ["mp3", "flac"])

    assert report["ok"] is True
    assert report["files"] == 2
    assert report["dirs"] == 1
    assert report["unreadable_files"] == 0
    assert report["unreadable_dirs"] == 0
    assert report["commands"] == []
    assert report["directories"][0]["path"] == str(root)
    assert report["directories"][0]["readable"] is True


def test_audit_flags_unreadable_file_and_builds_commands(
    tmp_path: Path, monkeypatch
) -> None:
    root = _music(tmp_path)
    blocked = root / "album" / "a.mp3"
    monkeypatch.setattr(permissions, "_can_read_dir", lambda path: True)
    monkeypatch.setattr(
        permissions, "_can_read_file", lambda path: Path(path) != blocked
    )

    report = audit_permissions([root], ["mp3", "flac"])

    assert report["ok"] is False
    assert report["files"] == 2
    assert report["unreadable_files"] == 1
    assert report["samples"][0]["kind"] == "file"
    assert report["samples"][0]["path"].endswith("a.mp3")
    assert len(report["commands"]) == 2
    assert report["commands"][0].startswith("setfacl -R -m u:")
    assert "-type d -exec setfacl -d -m u:" in report["commands"][1]


def test_audit_skips_excluded_dirs(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "music"
    (root / "keep").mkdir(parents=True)
    (root / "skip").mkdir()
    (root / "keep" / "ok.mp3").write_bytes(b"k")
    (root / "skip" / "hidden.mp3").write_bytes(b"s")
    monkeypatch.setattr(permissions, "_can_read_dir", lambda path: True)
    monkeypatch.setattr(permissions, "_can_read_file", lambda path: True)

    report = audit_permissions(
        [root], ["mp3"], exclude_dirs=[root / "skip"]
    )

    assert report["ok"] is True
    assert report["files"] == 1
    assert report["dirs"] == 1


def test_audit_reports_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    report = audit_permissions([missing], ["mp3"])

    assert report["ok"] is False
    assert report["unreadable_dirs"] == 1
    assert report["directories"][0]["exists"] is False
    assert report["commands"] == []
    assert "不存在" in report["samples"][0]["error"]


def test_audit_requires_music_dirs(tmp_path: Path) -> None:
    report = audit_permissions([], ["mp3"])

    assert report["ok"] is False
    assert report["error"] == "还没有配置音乐目录。"
    assert report["directories"] == []


def test_audit_truncates_at_file_limit(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "music"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.mp3").write_bytes(b"x")
    monkeypatch.setattr(permissions, "_can_read_dir", lambda path: True)
    monkeypatch.setattr(permissions, "_can_read_file", lambda path: True)

    report = audit_permissions([root], ["mp3"], max_files=2)

    assert report["truncated"] is True
    assert report["files"] == 2
    assert report["limit"] == 2
    assert report["ok"] is True

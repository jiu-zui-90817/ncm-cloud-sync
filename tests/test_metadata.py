from __future__ import annotations

from pathlib import Path

from ncm_cloud_sync.metadata import read_tags, sanitize_filename, song_name


def test_sanitize_filename() -> None:
    assert sanitize_filename('a/b:c*?"<>|') == "a_b_c______"
    assert sanitize_filename("  周杰伦 - 晴天  ") == "周杰伦 - 晴天"
    assert sanitize_filename("") == "未知歌曲"


def test_song_name_prefers_tag(tmp_path: Path) -> None:
    path = tmp_path / "track01.mp3"
    path.write_bytes(b"x")
    assert song_name(path, {"title": "晴天"}) == "晴天"
    assert song_name(path, {}) == "track01"


def test_read_tags_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_tags(tmp_path / "nope.mp3") == {}

from __future__ import annotations

from pathlib import Path

from ncm_cloud_sync.metadata import (
    find_lyric_sidecar,
    lyric_target,
    read_tags,
    sanitize_filename,
    song_name,
)


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


def test_find_lyric_sidecar_is_case_insensitive_and_prefers_exact_stem(tmp_path: Path) -> None:
    audio = tmp_path / "Song.mp3"
    audio.write_bytes(b"x")
    lower = tmp_path / "song.lrc"
    lower.write_text("lower", encoding="utf-8")
    exact = tmp_path / "Song.LRC"
    exact.write_text("exact", encoding="utf-8")
    assert find_lyric_sidecar(audio) == exact
    assert lyric_target(tmp_path / "Song (1).mp3", exact) == tmp_path / "Song (1).LRC"


def test_find_lyric_sidecar_missing_or_non_lrc_returns_none(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    (tmp_path / "a.txt").write_text("not lrc", encoding="utf-8")
    assert find_lyric_sidecar(audio) is None
    assert find_lyric_sidecar(tmp_path / "nope.mp3") is None

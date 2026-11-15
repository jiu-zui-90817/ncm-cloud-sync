from __future__ import annotations

from pathlib import Path

from ncm_cloud_sync.scanner import path_is_excluded, scan, scan_with_report


def test_scan_filters_extension_and_excludes(tmp_path: Path) -> None:
    music = tmp_path / "music"
    (music / "sub").mkdir(parents=True)
    (music / ".hidden").mkdir()
    (music / "@eaDir").mkdir()
    (music / "a.mp3").write_bytes(b"a")
    (music / "sub" / "b.flac").write_bytes(b"b")
    (music / "c.txt").write_text("c", encoding="utf-8")
    (music / ".hidden" / "d.mp3").write_bytes(b"d")
    (music / "@eaDir" / "e.mp3").write_bytes(b"e")

    found = list(scan([music], ["mp3", "flac"], [".*", "@eaDir"]))
    assert {item.path.name for item in found} == {"a.mp3", "b.flac"}
    assert all(item.size > 0 for item in found)
    assert {item.suffix for item in found} == {"mp3", "flac"}


def test_scan_report_explains_filtered_files(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "keep.mp3").write_bytes(b"a")
    (music / "notes.txt").write_text("notes", encoding="utf-8")
    (music / ".hidden").mkdir()
    (music / ".hidden" / "hidden.mp3").write_bytes(b"b")
    (music / "@eaDir").mkdir()
    (music / "@eaDir" / "cache.mp3").write_bytes(b"c")
    excluded = music / "有声书"
    excluded.mkdir()
    (excluded / "skip.mp3").write_bytes(b"d")

    files, report = scan_with_report(
        [music, tmp_path / "missing"],
        ["mp3"],
        [".*", "@eaDir"],
        [excluded],
    )

    assert [item.path.name for item in files] == ["keep.mp3"]
    assert report.candidates == 1
    assert report.unsupported == 1
    assert report.unsupported_suffixes == {"txt": 1}
    assert report.excluded == 2
    assert report.excluded_dirs == 1
    assert report.missing_roots == 1
    assert report.filtered == 4


def test_scan_skips_excluded_directory_tree(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "keep.mp3").write_bytes(b"a")
    excluded = music / "有声书"
    excluded.mkdir()
    (excluded / "skip.mp3").write_bytes(b"b")
    nested = excluded / "chapter"
    nested.mkdir()
    (nested / "deep.mp3").write_bytes(b"c")

    found = list(scan([music], ["mp3"], exclude_dirs=[excluded]))
    assert {item.path.name for item in found} == {"keep.mp3"}


def test_scan_exclude_outside_music_root_has_no_effect(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    (music / "keep.mp3").write_bytes(b"a")
    other = tmp_path / "other"
    other.mkdir()

    found = list(scan([music], ["mp3"], exclude_dirs=[other]))
    assert {item.path.name for item in found} == {"keep.mp3"}


def test_path_is_excluded_matches_components_and_exclude_dirs(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    hidden = music / ".hidden" / "a.mp3"
    nested = music / "有声书" / "chapter" / "b.mp3"
    assert path_is_excluded(hidden, [".*"], music_dirs=[music]) is True
    assert path_is_excluded(nested, [], [music / "有声书"], music_dirs=[music]) is True
    assert path_is_excluded(music / "normal.mp3", [".*"], music_dirs=[music]) is False
    hidden_root = tmp_path / ".music"
    hidden_root.mkdir()
    assert path_is_excluded(hidden_root / "a.mp3", [".*"], music_dirs=[hidden_root]) is False

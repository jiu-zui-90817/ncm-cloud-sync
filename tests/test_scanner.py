from __future__ import annotations

from pathlib import Path

from wy_music_cloud.scanner import scan


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

from __future__ import annotations

from pathlib import Path

from ncm_cloud_sync.metadata import read_tags
from ncm_cloud_sync.metadata_fill import _clean_name, fill_metadata


def make_mp3(path: Path) -> Path:
    # 8 个有效 MPEG 帧，足以让 mutagen 识别并写入 ID3 标签。
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * 8)
    return path


class FakeClient:
    def __init__(self, candidates: list[dict]) -> None:
        self.candidates = candidates
        self.queries: list[str] = []

    def search_songs(self, keyword: str, limit: int = 10) -> list[dict]:
        self.queries.append(keyword)
        return list(self.candidates)


def test_fill_metadata_writes_only_missing_fields(tmp_path: Path) -> None:
    path = make_mp3(tmp_path / "01. 周杰伦 - 晴天 [320k].mp3")
    client = FakeClient(
        [{"id": 1, "title": "晴天", "artist": "周杰伦", "album": "叶惠美"}]
    )
    result = fill_metadata(path, client)

    assert result.status == "filled"
    assert result.fields == {"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}
    assert client.queries == ["晴天 周杰伦"]
    assert read_tags(path) == {
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
    }

    again = fill_metadata(path, FakeClient([]))
    assert again.status == "skipped"


def test_fill_metadata_keeps_existing_and_rejects_artist_mismatch(tmp_path: Path) -> None:
    path = make_mp3(tmp_path / "周杰伦 - 晴天.mp3")
    client = FakeClient(
        [{"id": 1, "title": "晴天", "artist": "其他歌手", "album": "别的专辑"}]
    )
    result = fill_metadata(path, client)
    assert result.status == "not_found"
    assert read_tags(path) == {"title": "", "artist": "", "album": ""}


def test_fill_metadata_handles_reversed_title_artist_filename(tmp_path: Path) -> None:
    path = make_mp3(tmp_path / "晴天 - 周杰伦.mp3")
    client = FakeClient(
        [{"id": 186016, "title": "晴天", "artist": "周杰伦", "album": "叶惠美"}]
    )
    result = fill_metadata(path, client)

    assert result.status == "filled"
    assert result.fields == {"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}
    assert client.queries == ["周杰伦 晴天", "晴天 周杰伦"]


def test_clean_name_removes_track_and_quality_tags() -> None:
    assert _clean_name("01. 周杰伦 - 晴天 [320k]") == ("周杰伦", "晴天")
    assert _clean_name("Artist_Name_-_Title") == ("Artist Name", "Title")

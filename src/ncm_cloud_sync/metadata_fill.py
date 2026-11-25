"""用网易云搜索结果补全本地音频缺失的标题、艺术家和专辑标签。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile

from .client import CloudClient
from .metadata import read_tags

_FIELDS = ("title", "artist", "album")
_LEADING_TRACK_RE = re.compile(r"^\s*\d{1,3}[\s._-]+")
_TAG_RE = re.compile(r"[\[\(（【][^\]\)）】]{1,24}[\]\)）】]")
_CLEAN_RE = re.compile(r"[\s._-]+")
_SEARCH_LIMIT = 20


@dataclass
class FillResult:
    status: str
    fields: dict[str, str] = field(default_factory=dict)
    message: str = ""
    match: dict[str, Any] | None = None

    @property
    def filled(self) -> bool:
        return self.status == "filled"


def _normalize(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").casefold())


def _name_candidates(stem: str) -> list[tuple[str, str]]:
    """从文件名猜测 (artist, title)，同时兼容“歌手 - 歌名”和“歌名 - 歌手”。"""
    text = _TAG_RE.sub(" ", stem)
    text = _LEADING_TRACK_RE.sub("", text)
    text = text.replace("_", " ").strip()
    parts = re.split(r"\s+-\s+", text, maxsplit=1)
    if len(parts) == 2:
        left = _CLEAN_RE.sub(" ", parts[0]).strip()
        right = _CLEAN_RE.sub(" ", parts[1]).strip()
        if left and right:
            candidates = [(left, right)]
            swapped = (right, left)
            if swapped != candidates[0]:
                candidates.append(swapped)
            return candidates
    cleaned = _CLEAN_RE.sub(" ", text).strip()
    return [("", cleaned)] if cleaned else []


def _clean_name(stem: str) -> tuple[str, str]:
    """向后兼容：返回第一种文件名解释。"""
    candidates = _name_candidates(stem)
    return candidates[0] if candidates else ("", "")


def _candidate_score(
    candidate: dict[str, Any], title: str, artist: str
) -> int:
    wanted_title = _normalize(title)
    wanted_artist = _normalize(artist)
    found_title = _normalize(str(candidate.get("title") or ""))
    found_artist = _normalize(str(candidate.get("artist") or ""))
    if not wanted_title or not found_title:
        return -1
    if found_title == wanted_title:
        score = 100
    elif wanted_title in found_title or found_title in wanted_title:
        score = 70
    else:
        return -1
    if wanted_artist:
        if found_artist == wanted_artist:
            score += 30
        elif wanted_artist in found_artist or found_artist in wanted_artist:
            score += 15
        else:
            score -= 40
    return score


def _pick(
    candidates: list[dict[str, Any]], title: str, artist: str
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 69
    for candidate in candidates:
        score = _candidate_score(candidate, title, artist)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def fill_metadata(
    path: str | Path, client: CloudClient, *, force: bool = False
) -> FillResult:
    """只补缺失字段，不覆盖已有标签；找不到可靠匹配时保持原样。"""
    file_path = Path(path)
    tags = read_tags(file_path)
    title = str(tags.get("title") or "").strip()
    artist = str(tags.get("artist") or "").strip()
    album = str(tags.get("album") or "").strip()
    missing = [
        name
        for name, value in (("title", title), ("artist", artist), ("album", album))
        if force or not value
    ]
    if not missing:
        return FillResult("skipped", message="标签已完整")

    candidates = _name_candidates(file_path.stem)
    if not candidates:
        candidates = [("", "")]
    match: dict[str, Any] | None = None
    seen_queries: set[str] = set()
    last_query = ""
    for guessed_artist, guessed_title in candidates:
        query_title = title or guessed_title
        query_artist = artist or guessed_artist
        if not query_title:
            continue
        query = f"{query_title} {query_artist}".strip()
        if query in seen_queries:
            continue
        seen_queries.add(query)
        last_query = query
        songs = client.search_songs(query, limit=_SEARCH_LIMIT)
        match = _pick(songs, query_title, query_artist)
        if match is not None:
            break
    if match is None:
        if not seen_queries:
            return FillResult("not_found", message="无法从文件名解析歌曲名")
        return FillResult("not_found", message=f"没有找到可靠匹配：{last_query}")

    updates: dict[str, str] = {}
    for name in missing:
        value = str(match.get(name) or "").strip()
        if value:
            updates[name] = value
    if not updates:
        return FillResult("not_found", message="搜索结果缺少可补全字段", match=match)

    try:
        audio = MutagenFile(str(file_path), easy=True)
    except Exception as exc:
        return FillResult("unsupported", message=f"无法读取音频标签：{exc}", match=match)
    if audio is None:
        return FillResult("unsupported", message="当前格式不支持写入标签", match=match)
    try:
        if audio.tags is None:
            audio.add_tags()
        for name, value in updates.items():
            audio.tags[name] = [value]
        audio.save()
    except Exception as exc:
        return FillResult("failed", message=f"写入标签失败：{exc}", match=match)
    return FillResult("filled", fields=updates, match=match)

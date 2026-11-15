"""音频标签读取与文件名处理。"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

try:  # mutagen 是运行依赖，测试环境可能尚未安装。
    from mutagen import File as MutagenFile
except ImportError:  # pragma: no cover
    MutagenFile = None  # type: ignore[assignment]

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def first_value(value: Any) -> str:
    """mutagen 的标签值可能是字符串、列表或 None。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return ""
    return str(value).strip()


def read_tags(path: str | Path) -> dict[str, str]:
    """尽力读取 title/artist/album，失败时返回空字典。"""
    if MutagenFile is None:
        return {}
    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        return {}
    if audio is None:
        return {}
    tags = audio.tags or {}
    result: dict[str, str] = {}
    try:
        title = first_value(tags.get("title"))
        artist = first_value(tags.get("artist"))
        album = first_value(tags.get("album"))
        if not title and hasattr(audio, "get"):
            title = first_value(audio.get("title"))
        result["title"] = title
        result["artist"] = artist
        result["album"] = album
    except Exception:
        return result
    return result


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """网易云对文件名比较敏感，去掉非法字符并压缩空白。"""
    text = unicodedata.normalize("NFC", name or "")
    text = _INVALID_CHARS.sub("_", text)
    text = _SPACES.sub(" ", text).strip().strip(".")
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    return text or "未知歌曲"


def song_name(path: str | Path, tags: dict[str, str] | None = None) -> str:
    """优先用标签标题，其次用文件名（不含扩展名）。"""
    tags = tags if tags is not None else read_tags(path)
    title = sanitize_filename(tags.get("title") or Path(path).stem)
    return title


def display_artist(tags: dict[str, str] | None = None) -> str:
    tags = tags or {}
    return sanitize_filename(tags.get("artist") or "未知艺术家")


def display_album(tags: dict[str, str] | None = None) -> str:
    tags = tags or {}
    return sanitize_filename(tags.get("album") or "未知专辑")

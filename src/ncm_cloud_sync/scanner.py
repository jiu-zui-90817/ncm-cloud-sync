"""遍历音乐目录。"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFile:
    path: Path
    size: int
    mtime_ns: int

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower().lstrip(".")


def is_excluded(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _path_is_excluded(path: str | Path, excluded: tuple[str, ...]) -> bool:
    key = _path_key(path)
    for root in excluded:
        if key == root or key.startswith(root + os.sep):
            return True
    return False


def path_is_excluded(
    path: str | Path,
    exclude: Iterable[str] = (),
    exclude_dirs: Iterable[str | Path] = (),
    music_dirs: Iterable[str | Path] = (),
) -> bool:
    """判断某个已落库路径是否命中文件名或排除目录规则。

    如果提供 music_dirs，只检查音乐目录内的相对路径，避免音乐根目录本身
    的名字（例如以 . 开头）误伤整棵目录。
    """
    patterns = tuple(exclude)
    roots = tuple(_path_key(item) for item in exclude_dirs if str(item).strip())
    if _path_is_excluded(path, roots):
        return True
    target = Path(path)
    parts = target.parts
    for music_root in music_dirs:
        try:
            parts = target.relative_to(Path(music_root)).parts
            break
        except ValueError:
            continue
    return any(is_excluded(part, patterns) for part in parts)


def _walk(
    directory: Path,
    extensions: set[str],
    exclude: tuple[str, ...],
    exclude_dirs: tuple[str, ...],
) -> Iterator[AudioFile]:
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        logger.warning("无法读取目录 %s: %s", directory, exc)
        return
    for entry in entries:
        name = entry.name
        if is_excluded(name, exclude):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                if _path_is_excluded(entry.path, exclude_dirs):
                    continue
                yield from _walk(Path(entry.path), extensions, exclude, exclude_dirs)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            ext = Path(name).suffix.lower().lstrip(".")
            if ext not in extensions:
                continue
            stat = entry.stat(follow_symlinks=False)
            yield AudioFile(Path(entry.path), stat.st_size, stat.st_mtime_ns)
        except OSError as exc:
            logger.warning("跳过无法访问的条目 %s: %s", entry.path, exc)


def scan(
    music_dirs: Iterable[str | Path],
    extensions: Iterable[str],
    exclude: Iterable[str] = (),
    exclude_dirs: Iterable[str | Path] = (),
) -> Iterator[AudioFile]:
    """递归扫描目录，按扩展名过滤并跳过隐藏/排除项。"""
    ext_set = {item.lower().lstrip(".") for item in extensions}
    exclude_tuple = tuple(exclude)
    exclude_paths = tuple(
        _path_key(item) for item in exclude_dirs if str(item).strip()
    )
    seen: set[str] = set()
    for root in music_dirs:
        root_path = Path(root)
        if not root_path.is_dir():
            logger.warning("音乐目录不存在，已跳过: %s", root_path)
            continue
        if _path_is_excluded(root_path, exclude_paths):
            logger.info("音乐目录位于排除目录内，已跳过: %s", root_path)
            continue
        for audio in _walk(root_path, ext_set, exclude_tuple, exclude_paths):
            key = str(audio.path)
            if key in seen:
                continue
            seen.add(key)
            yield audio

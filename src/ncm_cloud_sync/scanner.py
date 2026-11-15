"""遍历音乐目录。"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
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


@dataclass
class ScanReport:
    """一次扫描的过滤明细，用于解释“本地为什么少于云盘”。"""

    candidates: int = 0
    unsupported: int = 0
    excluded: int = 0
    excluded_dirs: int = 0
    unreadable: int = 0
    missing_roots: int = 0
    unsupported_suffixes: dict[str, int] = field(default_factory=dict)

    @property
    def filtered(self) -> int:
        return self.unsupported + self.excluded + self.excluded_dirs + self.unreadable

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": self.candidates,
            "filtered": self.filtered,
            "unsupported": self.unsupported,
            "excluded": self.excluded,
            "excluded_dirs": self.excluded_dirs,
            "unreadable": self.unreadable,
            "missing_roots": self.missing_roots,
            "unsupported_suffixes": dict(self.unsupported_suffixes),
        }


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
    report: ScanReport,
) -> Iterator[AudioFile]:
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        report.unreadable += 1
        logger.warning("无法读取目录 %s: %s", directory, exc)
        return
    for entry in entries:
        name = entry.name
        if is_excluded(name, exclude):
            report.excluded += 1
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                if _path_is_excluded(entry.path, exclude_dirs):
                    report.excluded_dirs += 1
                    continue
                yield from _walk(
                    Path(entry.path), extensions, exclude, exclude_dirs, report
                )
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            ext = Path(name).suffix.lower().lstrip(".")
            if ext not in extensions:
                report.unsupported += 1
                report.unsupported_suffixes[ext] = (
                    report.unsupported_suffixes.get(ext, 0) + 1
                )
                continue
            stat = entry.stat(follow_symlinks=False)
            yield AudioFile(Path(entry.path), stat.st_size, stat.st_mtime_ns)
        except OSError as exc:
            report.unreadable += 1
            logger.warning("跳过无法访问的条目 %s: %s", entry.path, exc)


def scan_with_report(
    music_dirs: Iterable[str | Path],
    extensions: Iterable[str],
    exclude: Iterable[str] = (),
    exclude_dirs: Iterable[str | Path] = (),
) -> tuple[list[AudioFile], ScanReport]:
    """扫描并返回候选文件与过滤统计。"""
    report = ScanReport()
    ext_set = {item.lower().lstrip(".") for item in extensions}
    exclude_tuple = tuple(exclude)
    exclude_paths = tuple(
        _path_key(item) for item in exclude_dirs if str(item).strip()
    )
    seen: set[str] = set()
    files: list[AudioFile] = []
    for root in music_dirs:
        root_path = Path(root)
        if not root_path.is_dir():
            report.missing_roots += 1
            logger.warning("音乐目录不存在，已跳过: %s", root_path)
            continue
        if _path_is_excluded(root_path, exclude_paths):
            report.excluded_dirs += 1
            logger.info("音乐目录位于排除目录内，已跳过: %s", root_path)
            continue
        for audio in _walk(root_path, ext_set, exclude_tuple, exclude_paths, report):
            key = str(audio.path)
            if key in seen:
                continue
            seen.add(key)
            files.append(audio)
    report.candidates = len(files)
    return files, report


def scan(
    music_dirs: Iterable[str | Path],
    extensions: Iterable[str],
    exclude: Iterable[str] = (),
    exclude_dirs: Iterable[str | Path] = (),
) -> Iterator[AudioFile]:
    """递归扫描目录，按扩展名过滤并跳过隐藏/排除项。"""
    files, _report = scan_with_report(
        music_dirs, extensions, exclude, exclude_dirs
    )
    yield from files

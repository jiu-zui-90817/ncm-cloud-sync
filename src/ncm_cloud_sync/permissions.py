"""音乐目录权限体检。

应用以 ncm-cloud-sync 包用户运行，通常拿不到下载器新建文件的 600 权限。
这里只做只读检查并给出飞牛文件管理里的授权建议，不会修改 ACL，也不会提权。

飞牛自己的 ACL 不继承 Linux ACL，setfacl/default ACL 对下载器 rename 进来的
文件无效，因此不再生成 setfacl 命令，统一引导用户在文件管理界面授权。
"""

from __future__ import annotations

import getpass
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .scanner import path_is_excluded

# 单次体检最多检查的音频文件数，避免超大音乐库把 Web 请求拖死。
MAX_AUDIO_FILES = 200_000
# 单次体检的时间预算，超时后返回不完整结果。
MAX_SECONDS = 30.0
MAX_SAMPLES = 20


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - 极端环境没有用户名
        return str(getattr(os, "getuid", lambda: "unknown")())


def _can_read_dir(path: Path) -> bool:
    return os.access(path, os.R_OK | os.X_OK)


def _can_read_file(path: Path) -> bool:
    return os.access(path, os.R_OK)


def _sample(report: dict[str, Any], path: str, kind: str, error: str) -> None:
    if len(report["samples"]) >= MAX_SAMPLES:
        return
    report["samples"].append({"path": path, "kind": kind, "error": error})


def audit_permissions(
    music_dirs: Iterable[str | Path],
    extensions: Iterable[str],
    *,
    exclude: Iterable[str] = (),
    exclude_dirs: Iterable[str | Path] = (),
    max_files: int = MAX_AUDIO_FILES,
    max_seconds: float = MAX_SECONDS,
) -> dict[str, Any]:
    """检查应用账号能否读取音乐目录中的音频文件。

    返回值只包含检查结果和修复建议，不会修改文件权限。
    """
    roots = [Path(item) for item in music_dirs]
    ext_set = {str(item).lower().lstrip(".") for item in extensions if str(item).strip()}
    exclude_tuple = tuple(exclude)
    exclude_dir_tuple = tuple(str(item) for item in exclude_dirs if str(item).strip())
    user = _current_user()
    deadline = time.monotonic() + max(1.0, float(max_seconds))

    report: dict[str, Any] = {
        "ok": False,
        "checked_at": _utc_now(),
        "user": user,
        "files": 0,
        "dirs": 0,
        "unreadable_files": 0,
        "unreadable_dirs": 0,
        "samples": [],
        "directories": [],
        "truncated": False,
        "limit": int(max_files),
        "guidance": [],
        "notes": [
            "这里只检查配置的音乐目录中的音频文件，不会修改任何权限。",
            "请在飞牛文件管理里给应用账号授权；Linux setfacl 的默认 ACL "
            "不会被子级自动继承，不能作为可靠修复方式。",
            "如果下载器以 600/umask 077 写入，新增文件仍可能不可读；"
            "建议把下载器 umask 改为 022，或在下载完成钩子里单独授权。",
        ],
    }
    if not roots:
        report["error"] = "还没有配置音乐目录。"
        return report

    truncated = False
    scanned_files = 0
    stop_walk = False

    for root in roots:
        info: dict[str, Any] = {
            "path": str(root),
            "exists": root.is_dir(),
            "readable": False,
            "files": 0,
            "dirs": 0,
            "unreadable_files": 0,
            "unreadable_dirs": 0,
            "truncated": False,
        }
        report["directories"].append(info)

        if not info["exists"]:
            info["error"] = "目录不存在"
            report["unreadable_dirs"] += 1
            _sample(report, str(root), "dir", "目录不存在")
            continue
        if not _can_read_dir(root):
            info["error"] = "应用账号无法进入该目录"
            report["unreadable_dirs"] += 1
            _sample(report, str(root), "dir", "应用账号无法进入该目录")
            continue
        info["readable"] = True

        stack: list[Path] = [root]
        while stack:
            if time.monotonic() > deadline:
                truncated = True
                info["truncated"] = True
                stop_walk = True
                break
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                info["unreadable_dirs"] += 1
                report["unreadable_dirs"] += 1
                _sample(report, str(current), "dir", f"无法读取目录：{exc}")
                continue

            for entry in entries:
                if time.monotonic() > deadline:
                    truncated = True
                    info["truncated"] = True
                    stop_walk = True
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        if path_is_excluded(
                            child, exclude_tuple, exclude_dir_tuple, [root]
                        ):
                            continue
                        info["dirs"] += 1
                        report["dirs"] += 1
                        if not _can_read_dir(child):
                            info["unreadable_dirs"] += 1
                            report["unreadable_dirs"] += 1
                            _sample(
                                report,
                                str(child),
                                "dir",
                                "应用账号无法进入该目录",
                            )
                            continue
                        stack.append(child)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    suffix = Path(entry.name).suffix.lower().lstrip(".")
                    if suffix not in ext_set:
                        continue
                    if scanned_files >= max_files:
                        truncated = True
                        info["truncated"] = True
                        stop_walk = True
                        break
                    scanned_files += 1
                    info["files"] += 1
                    report["files"] += 1
                    if not _can_read_file(Path(entry.path)):
                        info["unreadable_files"] += 1
                        report["unreadable_files"] += 1
                        _sample(
                            report,
                            entry.path,
                            "file",
                            "应用账号无法读取该文件（可能是 600 权限）",
                        )
                except OSError as exc:
                    # 目录/文件在扫描过程中消失，或底层文件系统拒绝访问。
                    report["unreadable_dirs"] += 1
                    info["unreadable_dirs"] += 1
                    _sample(report, entry.path, "dir", f"无法访问：{exc}")
            if stop_walk:
                break
        if stop_walk:
            break

    report["truncated"] = truncated
    report["ok"] = (
        report["unreadable_files"] == 0
        and report["unreadable_dirs"] == 0
        and all(item.get("exists") for item in report["directories"])
    )

    affected = [
        str(info["path"])
        for info in report["directories"]
        if info.get("exists") and (info["unreadable_files"] or info["unreadable_dirs"])
    ]
    if affected or not report["ok"]:
        guidance = [
            "打开飞牛文件管理，进入下面这些目录（或选中对应文件）。",
            "在“权限设置 / 编辑权限”里添加应用账号 "
            f"{user}，至少授予读取权限：遍历文件夹/执行文件、"
            "列出文件夹/读取数据、读取属性。",
            "保存后回到这里重新点“开始体检”。",
            "如果新下载的文件仍然无权限，请把下载器的 umask 改为 022；"
            "飞牛 ACL 不继承 Linux 默认 ACL，setfacl 不能可靠修复。",
        ]
        if affected:
            guidance.insert(0, "需要授权的目录：" + "、".join(affected))
        report["guidance"] = guidance
    return report

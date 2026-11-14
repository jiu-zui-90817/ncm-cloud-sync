#!/usr/bin/env python3
"""Normalize executable bits in an fnOS .fpk outer archive.

fnpack on Windows records 0666 for every file. fnOS appcenter may execute
cmd/main and lifecycle scripts directly, so make them 0755. The nested
app.tgz is left byte-for-byte intact because manifest checksum is its MD5.
"""

from __future__ import annotations

import os
import sys
import tarfile
import tempfile


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: fix-fpk-modes.py <package.fpk>", file=sys.stderr)
        return 2

    path = os.path.abspath(sys.argv[1])
    if not os.path.isfile(path):
        print(f"not a file: {path}", file=sys.stderr)
        return 2

    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".fpk-modes-", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        with tarfile.open(path, "r:gz") as src, tarfile.open(
            tmp_path, "w:gz", format=tarfile.USTAR_FORMAT
        ) as dst:
            for member in src:
                if member.isdir():
                    member.mode = 0o755
                elif member.name.startswith("cmd/") and member.isfile():
                    member.mode = 0o755
                elif member.isfile():
                    member.mode = 0o644
                data = src.extractfile(member) if member.isfile() else None
                dst.addfile(member, data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    print(f"normalized modes: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

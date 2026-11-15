"""Merge fnOS wizard values into existing settings without dropping extra fields."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1])
    music = sys.argv[2]
    interval = int(sys.argv[3])
    log_level = sys.argv[4]

    data: dict[str, object] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            data = raw

    data["version"] = 1
    data["music_dirs"] = [music]
    data["interval"] = interval
    data["log_level"] = log_level
    # Newer settings survive an app-center config change; defaults only fill gaps.
    for key, default in (
        ("exclude_dirs", []),
        ("auto_sync", True),
        ("notify_on", "always"),
        ("notifications", []),
    ):
        data.setdefault(key, default)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

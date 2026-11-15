"""SQLite 状态库：本地去重、失败重试、运行记录。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import StateError

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    song_id INTEGER,
    cloud_name TEXT,
    duplicate_of TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    uploaded_at TEXT,
    seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_md5 ON files(md5);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    scanned INTEGER NOT NULL DEFAULT 0,
    uploaded INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FileRecord:
    path: str
    size: int
    mtime_ns: int
    md5: str | None
    status: str
    song_id: int | None
    cloud_name: str | None
    duplicate_of: str | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    uploaded_at: str | None
    seen_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileRecord":
        return cls(**{key: row[key] for key in row.keys()})


class StateStore:
    RUN_HISTORY_LIMIT = 5

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(str(self.path), timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        except sqlite3.Error as exc:
            raise StateError(f"无法打开状态数据库 {self.path}: {exc}") from exc

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- 查询 -------------------------------------------------------------
    def get(self, path: str | Path) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        return FileRecord.from_row(row) if row else None

    def needs_processing(self, path: str | Path, size: int, mtime_ns: int) -> bool:
        record = self.get(path)
        if record is None:
            return True
        if record.size != size or record.mtime_ns != mtime_ns:
            return True
        return record.status in {"pending", "uploading", "failed", "missing"}

    def is_done(self, path: str | Path, size: int, mtime_ns: int) -> bool:
        record = self.get(path)
        if record is None or record.size != size or record.mtime_ns != mtime_ns:
            return False
        return record.status in {"uploaded", "skipped_local", "skipped_remote"}

    def find_by_md5(self, md5: str, exclude_path: str | None = None) -> FileRecord | None:
        """找已经确认存在于云盘的同一 md5 文件。"""
        sql = (
            "SELECT * FROM files WHERE md5 = ? AND status IN "
            "('uploaded', 'skipped_remote')"
        )
        params: list[Any] = [md5]
        if exclude_path:
            sql += " AND path != ?"
            params.append(exclude_path)
        sql += " ORDER BY CASE status WHEN 'uploaded' THEN 0 ELSE 1 END, updated_at LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return FileRecord.from_row(row) if row else None

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def problem_counts(self) -> dict[str, int]:
        permission = self.conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE status = 'failed' "
            "AND last_error LIKE '%没有读取权限%'"
        ).fetchone()["n"]
        failed = self.conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE status = 'failed' "
            "AND (last_error IS NULL OR last_error NOT LIKE '%没有读取权限%')"
        ).fetchone()["n"]
        return {"failed": int(failed or 0), "permission": int(permission or 0)}

    def problem_files(self, kind: str = "failed", limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if kind == "permission":
            where = "status = 'failed' AND last_error LIKE '%没有读取权限%'"
        elif kind == "failed":
            where = (
                "status = 'failed' AND (last_error IS NULL OR last_error NOT LIKE '%没有读取权限%')"
            )
        elif kind == "all":
            where = "status = 'failed'"
        else:
            raise StateError(f"未知的问题类型: {kind}")
        rows = self.conn.execute(
            "SELECT path, size, attempts, last_error, updated_at FROM files "
            f"WHERE {where} ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def reset_problems(self, kind: str = "failed") -> int:
        if kind == "permission":
            where = "status = 'failed' AND last_error LIKE '%没有读取权限%'"
        elif kind == "failed":
            where = (
                "status = 'failed' AND (last_error IS NULL OR last_error NOT LIKE '%没有读取权限%')"
            )
        elif kind == "all":
            where = "status = 'failed'"
        else:
            raise StateError(f"未知的问题类型: {kind}")
        cursor = self.conn.execute(
            f"UPDATE files SET status = 'pending', last_error = NULL, updated_at = ? WHERE {where}",
            (utc_now(),),
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 20) -> list[FileRecord]:
        rows = self.conn.execute(
            "SELECT * FROM files ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [FileRecord.from_row(row) for row in rows]

    def all_paths(self) -> list[str]:
        rows = self.conn.execute("SELECT path FROM files").fetchall()
        return [str(row["path"]) for row in rows]

    # -- 写入 -------------------------------------------------------------
    def mark_seen(self, path: str | Path, size: int, mtime_ns: int) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO files (path, size, mtime_ns, status, created_at, updated_at, seen_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                status = CASE
                    WHEN files.size != excluded.size OR files.mtime_ns != excluded.mtime_ns
                    THEN 'pending' ELSE files.status END,
                md5 = CASE
                    WHEN files.size != excluded.size OR files.mtime_ns != excluded.mtime_ns
                    THEN NULL ELSE files.md5 END,
                seen_at = excluded.seen_at,
                updated_at = excluded.updated_at
            """,
            (str(path), size, mtime_ns, now, now, now),
        )
        self.conn.commit()

    def set_md5(self, path: str | Path, md5: str) -> None:
        self.conn.execute(
            "UPDATE files SET md5 = ?, updated_at = ? WHERE path = ?",
            (md5, utc_now(), str(path)),
        )
        self.conn.commit()

    def mark_uploading(self, path: str | Path) -> None:
        self.conn.execute(
            "UPDATE files SET status = 'uploading', attempts = attempts + 1, updated_at = ? "
            "WHERE path = ?",
            (utc_now(), str(path)),
        )
        self.conn.commit()

    def mark_uploaded(
        self,
        path: str | Path,
        *,
        song_id: int | None,
        cloud_name: str | None,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE files SET status = 'uploaded', song_id = ?, cloud_name = ?,
                last_error = NULL, uploaded_at = ?, updated_at = ?
            WHERE path = ?
            """,
            (song_id, cloud_name, now, now, str(path)),
        )
        self.conn.commit()

    def mark_skipped(
        self,
        path: str | Path,
        *,
        status: str,
        duplicate_of: str | None = None,
        song_id: int | None = None,
    ) -> None:
        if status not in {"skipped_local", "skipped_remote"}:
            raise StateError(f"非法跳过状态: {status}")
        self.conn.execute(
            """
            UPDATE files SET status = ?, duplicate_of = COALESCE(?, duplicate_of),
                song_id = COALESCE(?, song_id), last_error = NULL, updated_at = ?
            WHERE path = ?
            """,
            (status, duplicate_of, song_id, utc_now(), str(path)),
        )
        self.conn.commit()

    def mark_pending(self, path: str | Path, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE files SET status = 'pending', last_error = ?, updated_at = ? WHERE path = ?",
            (reason[:2000], utc_now(), str(path)),
        )
        self.conn.commit()

    def mark_failed(self, path: str | Path, error: str) -> None:
        self.conn.execute(
            "UPDATE files SET status = 'failed', last_error = ?, updated_at = ? WHERE path = ?",
            (error[:2000], utc_now(), str(path)),
        )
        self.conn.commit()

    def mark_missing(self, path: str | Path) -> None:
        self.conn.execute(
            "UPDATE files SET status = 'missing', updated_at = ? WHERE path = ?",
            (utc_now(), str(path)),
        )
        self.conn.commit()

    def mark_ignored(self, path: str | Path, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE files SET status = 'ignored', last_error = ?, updated_at = ? WHERE path = ?",
            (reason[:2000], utc_now(), str(path)),
        )
        self.conn.commit()

    def reset_failed(self) -> int:
        cursor = self.conn.execute(
            "UPDATE files SET status = 'pending', updated_at = ? WHERE status = 'failed'",
            (utc_now(),),
        )
        self.conn.commit()
        return cursor.rowcount

    # -- 运行记录 ---------------------------------------------------------
    def start_run(self) -> int:
        cursor = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (utc_now(),)
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        scanned: int = 0,
        uploaded: int = 0,
        skipped: int = 0,
        failed: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs SET finished_at = ?, scanned = ?, uploaded = ?, skipped = ?,
                failed = ?, error = ? WHERE id = ?
            """,
            (utc_now(), scanned, uploaded, skipped, failed, error, run_id),
        )
        # 运行记录只保留最近若干次，避免 SQLite 随长期运行无限增长。
        self.conn.execute(
            "DELETE FROM runs WHERE id NOT IN ("
            "SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (self.RUN_HISTORY_LIMIT,),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def missing_candidates(self, known_paths: set[str]) -> Iterable[FileRecord]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE status IN ('uploaded', 'skipped_local', 'skipped_remote')"
        ).fetchall()
        for row in rows:
            record = FileRecord.from_row(row)
            if record.path not in known_paths:
                yield record

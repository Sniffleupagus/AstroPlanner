"""SQLite cache for CaptureRecord scan results.

Cache key: (file_path, mtime, size). On hit, returns the stored record without
touching the file. On miss, the caller parses the file and calls store().

Two modes:
  read-write — used by update_cache.py to build/refresh the DB
  read-only  — used by scan.py so the RAID never needs to be writable
"""

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from planner.scanner import CaptureRecord

_LOCAL_DB = Path(__file__).parent.parent / "cache" / "captures.db"
_RAID_DB_NAME = "astroplanner_cache.db"

_CREATE = """
CREATE TABLE IF NOT EXISTS captures (
    file_path  TEXT PRIMARY KEY,
    file_mtime REAL NOT NULL,
    file_size  INTEGER NOT NULL,
    record_json TEXT NOT NULL
)
"""


def find_db(archive_base: str) -> Path | None:
    """Return path to an existing DB: RAID copy first, then local fallback."""
    raid = Path(archive_base) / _RAID_DB_NAME
    if raid.exists():
        return raid
    if _LOCAL_DB.exists():
        return _LOCAL_DB
    return None


def raid_db_path(archive_base: str) -> Path:
    return Path(archive_base) / _RAID_DB_NAME


def local_db_path() -> Path:
    return _LOCAL_DB


class CaptureCache:
    def __init__(self, db_path: Path | str, read_only: bool = False):
        self._path = Path(db_path)
        self._read_only = read_only
        if not read_only:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{self._path}{'?mode=ro' if read_only else ''}";
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        if not read_only:
            self._conn.execute(_CREATE)
            self._conn.commit()

    def lookup(self, file_path: str, mtime: float, size: int) -> CaptureRecord | None:
        row = self._conn.execute(
            "SELECT record_json FROM captures WHERE file_path=? AND file_mtime=? AND file_size=?",
            (file_path, mtime, size),
        ).fetchone()
        if row is None:
            return None
        d = json.loads(row["record_json"])
        return CaptureRecord(**d)

    def store(self, file_path: str, mtime: float, size: int, record: CaptureRecord) -> None:
        if self._read_only:
            raise RuntimeError("cache is read-only")
        self._conn.execute(
            """INSERT OR REPLACE INTO captures (file_path, file_mtime, file_size, record_json)
               VALUES (?, ?, ?, ?)""",
            (file_path, mtime, size, json.dumps(asdict(record))),
        )

    def all_paths(self) -> set[str]:
        rows = self._conn.execute("SELECT file_path FROM captures").fetchall()
        return {r["file_path"] for r in rows}

    def delete_paths(self, paths: set[str]) -> None:
        if self._read_only:
            raise RuntimeError("cache is read-only")
        self._conn.executemany(
            "DELETE FROM captures WHERE file_path=?", [(p,) for p in paths]
        )

    def commit(self) -> None:
        if not self._read_only:
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.commit()
        self.close()

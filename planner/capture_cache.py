"""SQLite cache for CaptureRecord scan results.

Cache key: (file_path, mtime, size). On hit, returns the stored record without
touching the file. On miss, the caller parses the file and calls store().

Two modes:
  read-write — used by update_cache.py to build/refresh the DB
  read-only  — used by scan.py so the RAID never needs to be writable
"""

import json
import math
import os
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path

from planner.scanner import CaptureRecord

_NETWORK_FS = {"smb", "smb2", "cifs", "nfs", "nfs4", "fuse.sshfs"}


def _is_local_fs(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() not in _NETWORK_FS
    except Exception:
        return True


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

_CREATE_SUBS = """
CREATE TABLE IF NOT EXISTS subs (
    file_path    TEXT PRIMARY KEY,
    file_mtime   REAL NOT NULL,
    file_size    INTEGER NOT NULL,
    ra_deg       REAL NOT NULL,
    dec_deg      REAL NOT NULL,
    target       TEXT NOT NULL,
    scope        TEXT NOT NULL,
    filter_name  TEXT NOT NULL,
    exposure_sec REAL NOT NULL,
    gain         INTEGER NOT NULL,
    date_obs     TEXT NOT NULL,
    sub_dir      TEXT NOT NULL
)
"""

_CREATE_SUBS_IDX = """
CREATE INDEX IF NOT EXISTS idx_subs_radec ON subs (ra_deg, dec_deg)
"""


def find_db(archive_base: str) -> Path | None:
    """Return path to an existing DB: local first, then RAID fallback."""
    if _LOCAL_DB.exists():
        return _LOCAL_DB
    raid = Path(archive_base) / _RAID_DB_NAME
    if raid.exists():
        return raid
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
        uri = f"file:{self._path}{'?mode=ro' if read_only else ''}"
        local = _is_local_fs(self._path)
        self._conn = sqlite3.connect(uri, uri=True, timeout=30 if local else 5)
        self._conn.row_factory = sqlite3.Row
        if not read_only:
            if local:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_CREATE)
            self._conn.execute(_CREATE_SUBS)
            self._conn.execute(_CREATE_SUBS_IDX)
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

    def load_all(self) -> list[CaptureRecord]:
        rows = self._conn.execute("SELECT record_json FROM captures").fetchall()
        return [CaptureRecord(**json.loads(r["record_json"])) for r in rows]

    def all_paths(self) -> set[str]:
        rows = self._conn.execute("SELECT file_path FROM captures").fetchall()
        return {r["file_path"] for r in rows}

    def delete_paths(self, paths: set[str]) -> None:
        if self._read_only:
            raise RuntimeError("cache is read-only")
        self._conn.executemany(
            "DELETE FROM captures WHERE file_path=?", [(p,) for p in paths]
        )

    def lookup_sub(self, file_path: str, mtime: float, size: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM subs WHERE file_path=? AND file_mtime=? AND file_size=?",
            (file_path, mtime, size),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def store_sub(self, file_path: str, mtime: float, size: int,
                  ra_deg: float, dec_deg: float, target: str, scope: str,
                  filter_name: str, exposure_sec: float, gain: int,
                  date_obs: str, sub_dir: str) -> None:
        if self._read_only:
            raise RuntimeError("cache is read-only")
        self._conn.execute(
            """INSERT OR REPLACE INTO subs
               (file_path, file_mtime, file_size, ra_deg, dec_deg, target,
                scope, filter_name, exposure_sec, gain, date_obs, sub_dir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_path, mtime, size, ra_deg, dec_deg, target, scope,
             filter_name, exposure_sec, gain, date_obs, sub_dir),
        )

    def query_subs(self, ra_center: float, dec_center: float, radius_deg: float,
                   scope: str | None = None, filter_name: str | None = None,
                   exposure_sec: float | None = None) -> list[dict]:
        cos_dec = max(0.01, abs(math.cos(math.radians(dec_center))))
        ra_lo = ra_center - radius_deg / cos_dec
        ra_hi = ra_center + radius_deg / cos_dec
        dec_lo = dec_center - radius_deg
        dec_hi = dec_center + radius_deg

        sql = "SELECT * FROM subs WHERE dec_deg BETWEEN ? AND ? AND ra_deg BETWEEN ? AND ?"
        params: list = [dec_lo, dec_hi, ra_lo, ra_hi]

        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if filter_name:
            sql += " AND filter_name = ?"
            params.append(filter_name)
        if exposure_sec is not None:
            sql += " AND exposure_sec = ?"
            params.append(exposure_sec)

        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def sub_paths(self) -> set[str]:
        rows = self._conn.execute("SELECT file_path FROM subs").fetchall()
        return {r["file_path"] for r in rows}

    def delete_sub_paths(self, paths: set[str]) -> None:
        if self._read_only:
            raise RuntimeError("cache is read-only")
        self._conn.executemany(
            "DELETE FROM subs WHERE file_path=?", [(p,) for p in paths]
        )

    def sub_count(self) -> int:
        try:
            row = self._conn.execute("SELECT COUNT(*) as c FROM subs").fetchone()
            return row["c"]
        except sqlite3.OperationalError:
            return 0

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

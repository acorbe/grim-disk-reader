"""Persistent state for a salvage run: selection overrides and per-path copy status.

Stored as SQLite next to the destination tree so a run can be killed (or the
source disk can wedge) and resumed later without re-copying successes or
losing track of failures.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selection (
    path TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('include', 'exclude'))
);

CREATE TABLE IF NOT EXISTS copy_state (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
    size INTEGER,
    mtime REAL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'in_progress', 'success', 'failed', 'skipped')
    ),
    bytes_copied INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_ts REAL,
    last_success_ts REAL
);

CREATE INDEX IF NOT EXISTS idx_copy_state_status ON copy_state(status);
"""


def to_rel(path: str) -> str:
    """Normalize a relative path to posix-style with no leading/trailing slash.

    The empty string denotes the root of the tree.
    """
    if path in ("", "."):
        return ""
    return PurePosixPath(path).as_posix().strip("/")


@dataclass
class CopyRecord:
    path: str
    kind: str
    size: Optional[int]
    mtime: Optional[float]
    status: str
    bytes_copied: int
    error: Optional[str]
    attempts: int
    last_attempt_ts: Optional[float]
    last_success_ts: Optional[float]


class RootMismatchError(RuntimeError):
    """Raised when an existing state DB was created for different source/dest roots."""


class StateDB:
    def __init__(self, db_path: Path, source_root: Path, dest_root: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._check_or_set_roots(str(source_root.resolve()), str(dest_root.resolve()))

    def _check_or_set_roots(self, source_root: str, dest_root: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'source_root'"
            ).fetchone()
            if row is None:
                self._conn.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    [
                        ("source_root", source_root),
                        ("dest_root", dest_root),
                        ("created_at", str(time.time())),
                    ],
                )
                self._conn.commit()
                return
            existing_source = row["value"]
            existing_dest = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'dest_root'"
            ).fetchone()["value"]
        if existing_source != source_root or existing_dest != dest_root:
            raise RootMismatchError(
                f"State DB {self.db_path} was created for source={existing_source!r} "
                f"dest={existing_dest!r}, but got source={source_root!r} dest={dest_root!r}. "
                "Use a different --state-db, or the correct source/dest pair."
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- selection overrides -------------------------------------------------

    def get_selection_override(self, path: str) -> Optional[str]:
        path = to_rel(path)
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM selection WHERE path = ?", (path,)
            ).fetchone()
        return row["state"] if row else None

    def all_selection_overrides(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT path, state FROM selection").fetchall()
        return {r["path"]: r["state"] for r in rows}

    def set_selection_override(self, path: str, state: str) -> None:
        path = to_rel(path)
        with self._lock:
            self._conn.execute(
                "INSERT INTO selection(path, state) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET state = excluded.state",
                (path, state),
            )
            self._conn.commit()

    def clear_selection_override(self, path: str) -> None:
        path = to_rel(path)
        with self._lock:
            self._conn.execute("DELETE FROM selection WHERE path = ?", (path,))
            self._conn.commit()

    def has_override_under(self, path: str, state: str) -> bool:
        """Is there an explicit `state` ('include'/'exclude') override at or below `path`?

        Used both to decide whether an excluded directory can be skipped
        entirely during the walk (something re-included below it?) and to
        render tri-state checkboxes (something excluded below an included
        directory, or vice versa?).
        """
        path = to_rel(path)
        prefix = path + "/" if path else ""
        with self._lock:
            if prefix:
                row = self._conn.execute(
                    "SELECT 1 FROM selection WHERE state = ? AND "
                    "(path = ? OR path LIKE ? ESCAPE '\\') LIMIT 1",
                    (state, path, prefix.replace("_", r"\_").replace("%", r"\%") + "%"),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM selection WHERE state = ? LIMIT 1", (state,)
                ).fetchone()
        return row is not None

    def has_include_override_under(self, path: str) -> bool:
        return self.has_override_under(path, "include")

    def clear_selection_overrides_under(self, path: str) -> None:
        """Remove explicit overrides strictly below `path` (its subtree)."""
        path = to_rel(path)
        prefix = path + "/" if path else ""
        with self._lock:
            self._conn.execute(
                "DELETE FROM selection WHERE path LIKE ? ESCAPE '\\' AND path != ?",
                (prefix.replace("_", r"\_").replace("%", r"\%") + "%", path),
            )
            self._conn.commit()

    # ---- copy state -----------------------------------------------------------

    def get_copy_record(self, path: str) -> Optional[CopyRecord]:
        path = to_rel(path)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM copy_state WHERE path = ?", (path,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def all_copy_records(self) -> dict[str, CopyRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM copy_state").fetchall()
        return {r["path"]: _row_to_record(r) for r in rows}

    def records_under(self, path: str) -> list[CopyRecord]:
        path = to_rel(path)
        prefix = path + "/" if path else ""
        with self._lock:
            if prefix:
                rows = self._conn.execute(
                    "SELECT * FROM copy_state WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                    (path, prefix.replace("_", r"\_").replace("%", r"\%") + "%"),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM copy_state").fetchall()
        return [_row_to_record(r) for r in rows]

    def upsert_pending(self, path: str, kind: str, size: Optional[int], mtime: Optional[float]) -> None:
        path = to_rel(path)
        with self._lock:
            self._conn.execute(
                "INSERT INTO copy_state(path, kind, size, mtime, status) "
                "VALUES (?, ?, ?, ?, 'pending') "
                "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime "
                "WHERE copy_state.status != 'success'",
                (path, kind, size, mtime),
            )
            self._conn.commit()

    def mark_in_progress(self, path: str) -> None:
        path = to_rel(path)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE copy_state SET status = 'in_progress', attempts = attempts + 1, "
                "last_attempt_ts = ? WHERE path = ?",
                (now, path),
            )
            self._conn.commit()

    def mark_progress_bytes(self, path: str, bytes_copied: int) -> None:
        path = to_rel(path)
        with self._lock:
            self._conn.execute(
                "UPDATE copy_state SET bytes_copied = ? WHERE path = ?",
                (bytes_copied, path),
            )
            self._conn.commit()

    def mark_success(self, path: str, bytes_copied: int) -> None:
        path = to_rel(path)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE copy_state SET status = 'success', bytes_copied = ?, "
                "error = NULL, last_success_ts = ? WHERE path = ?",
                (bytes_copied, now, path),
            )
            self._conn.commit()

    def mark_failed(self, path: str, error: str, bytes_copied: int = 0) -> None:
        path = to_rel(path)
        with self._lock:
            self._conn.execute(
                "UPDATE copy_state SET status = 'failed', error = ?, bytes_copied = ? "
                "WHERE path = ?",
                (error, bytes_copied, path),
            )
            self._conn.commit()

    def mark_skipped(self, path: str, reason: str) -> None:
        path = to_rel(path)
        with self._lock:
            self._conn.execute(
                "UPDATE copy_state SET status = 'skipped', error = ? WHERE path = ?",
                (reason, path),
            )
            self._conn.commit()

    def reset_failed_to_pending(self) -> int:
        """Requeue every failed file for another attempt. Returns count reset."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE copy_state SET status = 'pending', error = NULL "
                "WHERE status = 'failed'"
            )
            self._conn.commit()
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(bytes_copied), 0) AS b "
                "FROM copy_state WHERE kind = 'file' GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def bytes_done(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes_copied), 0) AS b FROM copy_state "
                "WHERE kind = 'file' AND status = 'success'"
            ).fetchone()
        return row["b"]


def _row_to_record(row: sqlite3.Row) -> CopyRecord:
    return CopyRecord(
        path=row["path"],
        kind=row["kind"],
        size=row["size"],
        mtime=row["mtime"],
        status=row["status"],
        bytes_copied=row["bytes_copied"],
        error=row["error"],
        attempts=row["attempts"],
        last_attempt_ts=row["last_attempt_ts"],
        last_success_ts=row["last_success_ts"],
    )

"""The SQLite connection, WAL, schema and store version (S4, Task 2).

Layout of the store root: `<root>/metadata.sqlite3` (this database). Events are the only
authority; every other table is bookkeeping. Nothing here updates or deletes an event.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

STORE_VERSION = 1
DB_NAME = "metadata.sqlite3"

# The two uniqueness rules on events are named indexes, so a test helper can lift them
# to plant a clash that the real write path never allows.
_SCHEMA_V1 = """
CREATE TABLE explorations (
    exploration_id   TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    workspace_id     TEXT NOT NULL,
    active_branch_id TEXT
);
CREATE TABLE branches (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    exploration_id   TEXT NOT NULL REFERENCES explorations(exploration_id),
    branch_id        TEXT NOT NULL,
    parent_branch_id TEXT,
    parent_tick      INTEGER,
    name             TEXT,
    UNIQUE (exploration_id, branch_id)
);
CREATE TABLE events (
    id                INTEGER PRIMARY KEY,
    exploration_id    TEXT NOT NULL REFERENCES explorations(exploration_id),
    tick              INTEGER NOT NULL,
    event_id          TEXT NOT NULL,
    branch_id         TEXT NOT NULL,
    wall_time_ms      INTEGER NOT NULL,
    type              TEXT NOT NULL,
    author_hint       TEXT,
    author_confidence TEXT,
    user_id           TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    body              TEXT NOT NULL
);
CREATE UNIQUE INDEX events_tick_uq ON events(exploration_id, tick);
CREATE UNIQUE INDEX events_event_id_uq ON events(event_id);
CREATE TABLE blobs (
    exploration_id TEXT NOT NULL,
    hash           TEXT NOT NULL,
    stored_bytes   INTEGER NOT NULL,
    original_bytes INTEGER NOT NULL,
    truncated      INTEGER NOT NULL,
    PRIMARY KEY (exploration_id, hash)
);
CREATE TABLE operations (
    operation_id   TEXT PRIMARY KEY,
    exploration_id TEXT,
    kind           TEXT NOT NULL,
    state          TEXT NOT NULL,
    started_ms     INTEGER NOT NULL,
    detail         TEXT
);
CREATE TABLE settings_applied (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_ms INTEGER NOT NULL,
    settings   TEXT NOT NULL
);
"""


class StoreVersionError(RuntimeError):
    """The database was written by a newer Blaym; it is left alone."""


SYNCHRONOUS_MODES = ("FULL", "NORMAL")


def open_database(root: Path, synchronous: str = "FULL", busy_timeout: float = 5.0) -> sqlite3.Connection:
    """`synchronous` FULL (default): an acknowledged event survives an OS crash or power
    cut. NORMAL is faster but can silently lose the last commits in such a crash."""
    if synchronous not in SYNCHRONOUS_MODES:
        raise ValueError(f"synchronous must be one of {SYNCHRONOUS_MODES}, not {synchronous!r}")
    root.mkdir(parents=True, exist_ok=True)
    # autocommit mode: transactions are explicit (BEGIN IMMEDIATE ... COMMIT) in the store
    conn = sqlite3.connect(root / DB_NAME, isolation_level=None, check_same_thread=False,
                           timeout=busy_timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={synchronous}")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM schema_info WHERE key='store_version'").fetchone()
    if row is None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA_V1.split(";\n"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_info(key, value) VALUES ('store_version', ?)", (str(STORE_VERSION),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return
    if int(row[0]) > STORE_VERSION:
        raise StoreVersionError(
            f"this history was written by a newer version of Blaym (store version {row[0]}, "
            f"this one understands up to {STORE_VERSION})"
        )


def translate_busy(fn):
    """A locked database (after the busy timeout) becomes `StoreBusy`, which is safe to retry."""
    import functools

    from blaym_jupyter.ports.events import StoreBusy

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise StoreBusy(f"the history database is busy: {exc}") from exc
            raise

    return wrapper

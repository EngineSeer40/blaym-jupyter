"""SQLite-specific behaviour of the event store. The shared contract suite in `tests/ports/`
covers what every store must do."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from blaym_jupyter.ports.events import BranchRecord, DuplicateEventId
from blaym_jupyter.store.db import STORE_VERSION, StoreVersionError, open_database
from blaym_jupyter.store.events import SqliteEventStore
from blaym_jupyter.tests.ports.harness import EXP, dump, make_event

_ROW = ("INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type, user_id,"
        " workspace_id, body) VALUES (?,?,?,?,0,'t','u','w','{}')")


def test_database_layout_durability_constraints_version_and_reopen(tmp_path):
    s = SqliteEventStore(tmp_path / "a")
    try:
        conn = s._conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL by default
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"explorations", "branches", "events", "blobs", "operations", "schema_info"} <= tables
        s.append(EXP, [make_event(1), make_event(2)], branches=[BranchRecord("main")], active_branch_id="main")
        with pytest.raises(sqlite3.IntegrityError):  # the database itself enforces unique tick and event id
            conn.execute(_ROW, (EXP, 1, "another-id", "main"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_ROW, (EXP, 3, make_event(1)["eventId"], "main"))
        with pytest.raises(DuplicateEventId):  # a refused batch leaves no rows behind
            s.append("fresh", [make_event(1, exp="fresh"), make_event(1, exp="fresh")])
        assert s.exploration("fresh") is None and not conn.in_transaction
        before = dump(s.events_for(EXP))
    finally:
        s.close()
    with pytest.raises(ValueError):
        SqliteEventStore(tmp_path / "b", synchronous="OFF")
    s = SqliteEventStore(tmp_path / "a")  # a restart keeps everything
    try:
        assert dump(s.events_for(EXP)) == before and s.exploration(EXP).active_branch_id == "main"
        assert s.append(EXP, [make_event(3)])[0].tick == 3
    finally:
        s.close()
    open_database(tmp_path / "c").close()
    conn = sqlite3.connect(tmp_path / "c" / "metadata.sqlite3")
    conn.execute("UPDATE schema_info SET value=? WHERE key='store_version'", (str(STORE_VERSION + 1),))
    conn.commit()
    conn.close()
    with pytest.raises(StoreVersionError):  # a newer store version is left alone
        SqliteEventStore(tmp_path / "c")


def test_two_stores_on_one_root_share_dense_ticks_and_a_planted_clash_comes_back_in_arrival_order(tmp_path):
    a, b = SqliteEventStore(tmp_path), SqliteEventStore(tmp_path)
    try:
        for i in range(10):
            got = (a if i % 2 == 0 else b).append(EXP, [make_event(i), make_event(i + 1000)])
            assert [e.tick for e in got] == [2 * i + 1, 2 * i + 2]
        with pytest.raises(DuplicateEventId):  # a failed batch does not burn a tick for the other
            a.append(EXP, [make_event(5000), make_event(1)])
        assert b.append(EXP, [make_event(5001)])[0].tick == 21

        def work(store, idx):
            for i in range(20):
                store.append(EXP, [make_event(idx * 10_000 + i)])

        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def run(store, idx):
            try:
                barrier.wait()
                work(store, idx)
            except BaseException as exc:  # collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(s, i + 1)) for i, s in enumerate((a, b))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        for exc in errors:  # the only acceptable failure is a clean "database is locked"
            assert isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc), repr(exc)
        ticks = [e.tick for e in a.events_for(EXP)]
        assert ticks == list(range(1, len(ticks) + 1)) and len(ticks) == 21 + 40 - len(errors)
        # a clash (two events at one tick) is returned as two events, in arrival order
        a._conn.execute("DROP INDEX events_tick_uq")
        ids = ["f" * 8 + "-0000-4000-8000-000000000001", "a" * 8 + "-0000-4000-8000-000000000001"]
        for name in ids:
            a._conn.execute(
                "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type, user_id,"
                " workspace_id, body) SELECT exploration_id, tick, ?, branch_id, wall_time_ms, type, user_id,"
                " workspace_id, replace(body, ?, ?) FROM events WHERE tick=1 AND event_id != ? LIMIT 1",
                (name, make_event(0)["eventId"], name, name))
        assert [str(e.eventId) for e in a.events_for(EXP) if e.tick == 1][1:] == ids
    finally:
        a.close()
        b.close()

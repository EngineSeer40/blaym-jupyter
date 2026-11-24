"""SQLite-specific behaviour of the event store (S4, Task 2). The shared contract suite
in `tests/ports/` covers what every store must do."""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from blaym_jupyter.ports.events import DuplicateEventId
from blaym_jupyter.store.db import STORE_VERSION, StoreVersionError, open_database
from blaym_jupyter.store.events import SqliteEventStore
from blaym_jupyter.tests.ports.test_event_store_contract import EXP, make_event


def test_interpreter_sqlite_has_fts5_trigram_wal_and_json():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        conn.execute("INSERT INTO t VALUES ('residuals plot')")
        assert conn.execute("SELECT count(*) FROM t WHERE t MATCH 'idua'").fetchone()[0] == 1
        assert json.loads(conn.execute("SELECT json_extract('{\"a\": [1, 2]}', '$.a')").fetchone()[0]) == [1, 2]
    except sqlite3.OperationalError as exc:  # fail loudly: S4 cannot run without these
        pytest.fail(f"this Python's SQLite lacks FTS5, trigram or JSON support: {exc}")
    finally:
        conn.close()


def test_database_is_in_wal_mode_and_has_the_planned_tables(sqlite_harness):
    conn = sqlite_harness.store._conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"explorations", "branches", "events", "blobs", "operations", "settings_applied", "schema_info"} <= tables
    assert conn.execute("SELECT value FROM schema_info WHERE key='store_version'").fetchone()[0] == str(STORE_VERSION)
    assert (sqlite_harness._dir / "metadata.sqlite3").exists()


def test_the_database_itself_enforces_unique_tick_and_unique_event_id(sqlite_harness):
    s = sqlite_harness.store
    s.append(EXP, [make_event(1)])
    row = ("INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type, user_id,"
           " workspace_id, body) VALUES (?,?,?,?,0,'t','u','w','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        s._conn.execute(row, (EXP, 1, "another-id", "main"))
    with pytest.raises(sqlite3.IntegrityError):
        s._conn.execute(row, (EXP, 2, make_event(1)["eventId"], "main"))


def test_a_rolled_back_write_leaves_no_tick_branch_or_exploration_row(sqlite_harness):
    s = sqlite_harness.store
    with pytest.raises(DuplicateEventId):
        s.append("fresh", [make_event(1, exp="fresh"), make_event(1, exp="fresh")])
    assert s.exploration("fresh") is None
    assert s._conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert not s._conn.in_transaction


def test_stored_body_is_plain_json_without_nulls_and_without_the_placeholder_tick(sqlite_harness):
    s = sqlite_harness.store
    s.append(EXP, [make_event(1, nulls=True), make_event(2)])
    rows = s._conn.execute("SELECT tick, body FROM events ORDER BY tick").fetchall()
    for tick, body in rows:
        data = json.loads(body)
        assert data["tick"] == tick
        assert None not in data.values()
        assert "authorHint" not in data


def test_a_newer_store_version_is_left_alone(tmp_path):
    open_database(tmp_path).close()
    conn = sqlite3.connect(tmp_path / "metadata.sqlite3")
    conn.execute("UPDATE schema_info SET value=? WHERE key='store_version'", (str(STORE_VERSION + 1),))
    conn.commit()
    conn.close()
    with pytest.raises(StoreVersionError):
        SqliteEventStore(tmp_path)


def test_a_second_connection_sees_committed_events_only(tmp_path):
    a = SqliteEventStore(tmp_path)
    b = SqliteEventStore(tmp_path)
    try:
        a.append(EXP, [make_event(1)])
        assert [e.tick for e in b.events_for(EXP)] == [1]
        assert b.append(EXP, [make_event(2)])[0].tick == 2
        assert a.tip_tick(EXP) == 2
    finally:
        a.close()
        b.close()


def test_a_clash_can_be_planted_by_lifting_the_unique_index(sqlite_harness):
    """What Task 3 needs: the real write path never makes a clash, a test helper can."""
    s = sqlite_harness.store
    s.append(EXP, [make_event(1)])
    s._conn.execute("DROP INDEX events_tick_uq")
    s._conn.execute(
        "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type, user_id,"
        " workspace_id, body) SELECT exploration_id, tick, 'second', branch_id, wall_time_ms, type, user_id,"
        " workspace_id, body FROM events"
    )
    assert s._conn.execute("SELECT count(*) FROM events WHERE tick=1").fetchone()[0] == 2


def test_two_stores_on_one_root_appending_alternately_get_a_dense_tick_sequence(tmp_path):
    a = SqliteEventStore(tmp_path)
    b = SqliteEventStore(tmp_path)
    try:
        n = 0
        for i in range(30):
            n += 1
            got = (a if i % 2 == 0 else b).append(EXP, [make_event(n), make_event(n + 1000)])
            assert [e.tick for e in got] == [2 * i + 1, 2 * i + 2]
        # a failed batch on one connection does not burn a tick for the other
        with pytest.raises(DuplicateEventId):
            a.append(EXP, [make_event(5000), make_event(1)])
        assert b.append(EXP, [make_event(5001)])[0].tick == 61
        assert [e.tick for e in a.events_for(EXP)] == list(range(1, 62))
    finally:
        a.close()
        b.close()


def test_two_stores_appending_from_two_threads_never_take_the_same_tick(tmp_path):
    stores = [SqliteEventStore(tmp_path), SqliteEventStore(tmp_path)]
    per_thread = 40
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(stores))

    def work(idx: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                stores[idx].append(EXP, [make_event(idx * 10_000 + i)])
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(len(stores))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        # the only acceptable failure is a clean "database is locked" (a retry message)
        for exc in errors:
            assert isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc), repr(exc)
        ticks = [e.tick for e in stores[0].events_for(EXP)]
        assert ticks == list(range(1, len(ticks) + 1))  # dense, no repeats, no holes
        assert len(ticks) == 2 * per_thread - len(errors)
    finally:
        for s in stores:
            s.close()


def test_reopening_keeps_branches_active_branch_and_exploration_after_failed_writes(tmp_path):
    from blaym_jupyter.ports.events import BranchRecord, InvalidEvent

    s = SqliteEventStore(tmp_path)
    s.append(EXP, [make_event(1)], branches=[BranchRecord("main")], active_branch_id="main")
    s.append(EXP, [make_event(2, branch="b1")], branches=[BranchRecord("b1", "main", 1, "try")], active_branch_id="b1")
    with pytest.raises(InvalidEvent):
        s.append(EXP, [make_event(3), make_event(4, payload={"cellId": "c"})], branches=[BranchRecord("b2")], active_branch_id="b2")
    s.close()
    s = SqliteEventStore(tmp_path)
    try:
        assert [b.branch_id for b in s.branches(EXP)] == ["main", "b1"]
        assert s.exploration(EXP).active_branch_id == "b1"
        assert s.tip_tick(EXP) == 2
    finally:
        s.close()


def test_an_invalid_first_event_leaves_no_half_created_exploration(sqlite_harness):
    from blaym_jupyter.ports.events import BranchRecord, InvalidEvent

    s = sqlite_harness.store
    for bad in ({"payload": {"cellId": "c"}}, {"explorationId": "other"}):
        with pytest.raises(InvalidEvent):
            s.append("fresh", [make_event(1, exp="fresh", **bad)], branches=[BranchRecord("main")], active_branch_id="main")
    with pytest.raises(DuplicateEventId):
        s.append("fresh", [make_event(2, exp="fresh"), make_event(2, exp="fresh")], active_branch_id="main")
    with pytest.raises(InvalidEvent):  # valid event but an active branch that has no record
        s.append("fresh", [make_event(3, exp="fresh")], active_branch_id="main")
    for table in ("explorations", "events", "branches"):
        assert s._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert s.explorations() == []


def test_a_planted_clash_is_returned_as_two_events_not_deduplicated(sqlite_harness):
    s = sqlite_harness.store
    s.append(EXP, [make_event(1)])
    s._conn.execute("DROP INDEX events_tick_uq")
    s._conn.execute(
        "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type, user_id,"
        " workspace_id, body) SELECT exploration_id, tick, 'second', branch_id, wall_time_ms, type, user_id,"
        " workspace_id, body FROM events"
    )
    got = s.events_for(EXP)
    assert [e.tick for e in got] == [1, 1]


def test_durability_is_full_by_default_normal_is_selectable_and_bad_values_are_refused(tmp_path):
    default = SqliteEventStore(tmp_path / "a")
    normal = SqliteEventStore(tmp_path / "b", synchronous="NORMAL")
    try:
        assert default._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert normal._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        default.close()
        normal.close()
    for bad in ("OFF", "full", "FULL; DROP TABLE events", ""):
        with pytest.raises(ValueError):
            SqliteEventStore(tmp_path / "c", synchronous=bad)


def test_a_planted_clash_comes_back_in_arrival_order(sqlite_harness):
    """Ties at one tick are ordered by insertion (row id), not by whatever index the planner
    picks. A non-unique index on (exploration, tick, event id) would hand back event-id order
    if the query did not break the tie itself."""
    s = sqlite_harness.store
    s.append(EXP, [make_event(1)])
    conn = s._conn
    conn.execute("DROP INDEX events_tick_uq")
    conn.execute("CREATE INDEX events_probe ON events(exploration_id, tick, event_id)")
    ids = ["f" * 8 + "-0000-4000-8000-000000000001", "a" * 8 + "-0000-4000-8000-000000000001", "c" * 8 + "-0000-4000-8000-000000000001"]
    for name in ids:  # arrival order is not event-id order
        conn.execute(
            "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type, user_id,"
            " workspace_id, body) SELECT exploration_id, tick, ?, branch_id, wall_time_ms, type, user_id,"
            " workspace_id, replace(body, ?, ?) FROM events WHERE tick=1 AND event_id != ? LIMIT 1",
            (name, make_event(1)["eventId"], name, name),
        )
    got = [str(e.eventId) for e in s.events_for(EXP)]
    assert got[1:] == ids

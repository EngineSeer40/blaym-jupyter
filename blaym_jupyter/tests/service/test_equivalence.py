"""Rebuild equivalence through the port (S4, Task 3).

A `Rebuilder` hydrated from a store (in-memory and SQLite, and SQLite reopened) must
answer exactly as a `Rebuilder` fed the same events directly, at every sampled position,
and its outline must equal `history.build_outline` of the same events. Hydration never
changes a stored event.

What a store can and cannot hold (the store keeps one row per event id, keyed by tick):

* exact duplicates: a store holds the first arrival once; the repeat is not reachable.
* same event id, different content (a conflict): NOT reachable through a store, in either
  implementation (unique event id). It is reproduced by what the rebuilder's own EventLog
  sees: after hydrating the held events, the whole delivery (conflicting copy included) is
  passed to `HydratedRebuilder.add`, the incremental path, and compared with a rebuilder fed
  the same sequence directly.
* a tick clash (two event ids at one tick): reachable in SQLite only (the test helper lifts
  `events_tick_uq`); the in-memory store is keyed by tick and cannot hold one, so there the
  clashing second arrival is left out of the store and reaches the rebuilder via `add` only.
* a hole (dropped ticks), reordering and an unknown branch: reachable in both. Reordering is
  invisible to a store (it returns tick order), so the "direct" rebuilder is fed the held
  events in tick order, ties in arrival order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blaym_jupyter.history import Rebuilder, build_outline
from blaym_jupyter.service.hydrate import HydratedRebuilder, hydrate
from blaym_jupyter.tests.history.conftest import DELIVERY_SETTINGS, MAX_EVENTS
from blaym_jupyter.tests.history.generate import deliveries, histories, invalid_histories
from blaym_jupyter.tests.ports.harness import KINDS, Harness
from blaym_jupyter_types import BlaymEvent

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rebuild"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
INTERVALS = (7, None)
EXP = "exploration-1"
EXP_S = "exploration-1"


def dump(result):
    return None if result is None else result.model_dump(mode="json", exclude_none=True)


def held_events(events, can_hold_clash: bool):
    """What a store can hold of a delivery: the first arrival of each event id; and, if the
    store cannot hold a clash, only the first event at each tick. Returned in store order
    (tick order, ties in arrival order)."""
    ids, ticks, held = set(), {}, []
    for raw in events:
        ev = raw.root if isinstance(raw, BlaymEvent) else raw
        if str(ev.eventId) in ids:
            continue
        if not can_hold_clash and ev.tick in ticks:
            continue
        ids.add(str(ev.eventId))
        ticks[ev.tick] = str(ev.eventId)
        held.append(ev)
    return sorted(held, key=lambda e: e.tick)  # sorted is stable


def positions(branches, events):
    ticks = sorted({e.tick for e in events})
    last = ticks[-1] if ticks else 0
    if last > 30:
        keep = set(ticks[:: max(1, last // 20)]) | {1, last}
        keep |= {e.tick + d for e in events if e.type == "branch_point_created" for d in (-1, 0, 1)}
        ticks = sorted(t for t in keep if t >= 1)
    sampled = {0, *ticks, last + 1, last + 5}
    return [(b, t) for b in branches for t in sorted(sampled)]


def branches_of(events, *extra):
    return sorted({e.branchId for e in events} | set(extra))


def assert_same(hydrated: HydratedRebuilder, direct: Rebuilder, direct_events, branches, where=""):
    for branch, tick in positions(branches, direct_events):
        assert dump(hydrated.state_at(branch, tick)) == dump(direct.state_at(branch, tick)), (where, branch, tick)
    expected = build_outline(direct_events)
    got = hydrated.outline_result()
    assert dump(got.outline) == dump(expected.outline), where
    assert dump(got.problem) == dump(expected.problem), where


def raw_rows(h: Harness, exploration_id):
    """The stored bytes of every event, for the 'not altered' check."""
    if h.kind == "memory":
        return json.dumps({str(k): v for k, v in sorted(h.store._events.get(exploration_id, {}).items())})
    rows = h.store._conn.execute("SELECT id, tick, event_id, body FROM events ORDER BY id").fetchall()
    return json.dumps(rows)


def check_through_store(kind, exploration_id, delivered, branches, interval, *, whole_delivery_after=False):
    """Load `delivered` into a store of `kind`, hydrate, compare; reopen (SQLite) and hydrate
    again, compare; optionally then add the whole delivery incrementally (conflicts)."""
    h = Harness(kind)
    try:
        held = held_events(delivered, h.can_hold_clash)
        h.load_exact(exploration_id, held)
        before = raw_rows(h, exploration_id)
        direct = Rebuilder(interval)
        direct.add(held)
        hydrated = hydrate(h.store, exploration_id, interval)
        assert_same(hydrated, direct, held, branches, f"{kind} hydrate")
        assert raw_rows(h, exploration_id) == before  # hydration changed nothing
        if h.can_reopen:
            h.reopen()
            again = hydrate(h.store, exploration_id, interval)
            assert_same(again, direct, held, branches, f"{kind} reopened")
            assert raw_rows(h, exploration_id) == before
        if whole_delivery_after:
            everything = list(held) + list(delivered)
            hydrated.add(delivered)
            direct.add(delivered)
            assert_same(hydrated, direct, everything, branches, f"{kind} after add")
            assert raw_rows(h, exploration_id) == before  # add never writes to the store
    finally:
        h.close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("interval", INTERVALS)
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_every_rebuild_fixture_through_the_store(path, interval, kind):
    data = json.loads(path.read_text())
    events = [BlaymEvent.model_validate(e).root for e in data["events"]]
    branches = branches_of(events, *{ex["branchId"] for ex in data["expectations"]})
    check_through_store(kind, events[0].explorationId, events, branches, interval)
    # the recorded expectations hold for the hydrated rebuilder as they do for a direct one
    h = Harness(kind)
    try:
        h.load_exact(events[0].explorationId, held_events(events, h.can_hold_clash))
        hydrated = hydrate(h.store, events[0].explorationId, interval)
        for ex in data["expectations"]:
            assert dump(hydrated.state_at(ex["branchId"], ex["tick"])) == ex["result"]
    finally:
        h.close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("interval", INTERVALS)
@settings(max_examples=100)
@given(histories(max_events=MAX_EVENTS))
def test_generated_valid_histories_through_the_store(kind, interval, history):
    check_through_store(kind, history.events[0].explorationId, history.events,
                        branches_of(history.events, "ghost-branch"), interval)


@pytest.mark.parametrize("kind", KINDS)
@DELIVERY_SETTINGS
@given(deliveries())
def test_generated_deliveries_through_the_store(kind, delivery):
    """Duplicates, reordering, drops, tick clashes and unknown branches go through the store
    (clashes in SQLite only); conflicts reach the rebuilder through `add` (see module doc)."""
    if not delivery.events:
        return
    check_through_store(kind, delivery.events[0].explorationId, delivery.events,
                        branches_of(delivery.events, delivery.unknown_branch or "ghost-branch"),
                        7, whole_delivery_after=True)


@pytest.mark.parametrize("kind", KINDS)
@settings(max_examples=100)
@given(invalid_histories())
def test_generated_invalid_histories_through_the_store(kind, invalid):
    branches = branches_of(invalid.events, invalid.branch)
    check_through_store(kind, invalid.events[0].explorationId, invalid.events, branches, 7,
                        whole_delivery_after=True)
    # the typed answer the generator promised is the one the hydrated rebuilder gives
    h = Harness(kind)
    try:
        h.load_exact(invalid.events[0].explorationId, held_events(invalid.events, h.can_hold_clash))
        if invalid.kind == "tick_clash" and not h.can_hold_clash:
            return  # not reachable through this store
        got = hydrate(h.store, invalid.events[0].explorationId, 7).state_at(invalid.branch, invalid.tick)
        assert got.status == invalid.expected_status
        if invalid.expected_reason is not None:
            assert got.reason == invalid.expected_reason
    finally:
        h.close()


@pytest.mark.parametrize("kind", KINDS)
@settings(max_examples=100)
@given(histories(max_events=MAX_EVENTS), st.data())
def test_appending_after_hydrate_equals_a_fresh_hydrate(kind, history, data):
    """Real writes: events committed through the store after hydration, added incrementally,
    give the same answers and outline as hydrating the whole history afresh."""
    events = history.events
    exp = events[0].explorationId
    cut = data.draw(st.integers(1, len(events)))
    h = Harness(kind)
    try:
        h.store.append(exp, [e.model_dump(mode="json", exclude_none=True) for e in events[:cut]])
        live = hydrate(h.store, exp, 7)
        rest = h.store.append(exp, [e.model_dump(mode="json", exclude_none=True) for e in events[cut:]]) \
            if cut < len(events) else []
        live.add(rest)
        fresh = hydrate(h.store, exp, 7)
        stored = h.store.events_for(exp)
        assert [str(e.eventId) for e in stored] == [str(e.eventId) for e in events]
        direct = Rebuilder(7)
        direct.add(stored)
        assert_same(live, fresh, stored, branches_of(events, "ghost-branch"), f"{kind} live vs fresh")
        assert_same(live, direct, stored, branches_of(events), f"{kind} live vs direct")
    finally:
        h.close()


@pytest.mark.parametrize("kind", KINDS)
def test_a_planted_hole_gives_missing_events_and_is_never_repaired(kind):
    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    s.edit("c1", "x = 3")
    s.edit("c1", "x = 4")
    h = Harness(kind)
    try:
        h.load_exact(EXP, [e for e in s.events if e.tick != 3])  # tick 3 never arrived
        before = raw_rows(h, EXP)
        rb = hydrate(h.store, EXP, 2)
        for tick in (3, 4, 5, 9):
            got = rb.state_at("main", tick)
            assert (got.status, got.firstMissingTick) == ("missing_events", 3), tick
        assert rb.state_at("main", 2).status == "ok"
        out = rb.outline()
        assert out is not None and out.highestTick == 2
        assert h.store.tip_tick(EXP) == 4 and len(h.store.events_for(EXP)) == 3
        assert raw_rows(h, EXP) == before
        if h.can_reopen:
            h.reopen()
            assert hydrate(h.store, EXP, 2).state_at("main", 4).status == "missing_events"
        # the missing event arriving later fills the hole incrementally
        rb.add([e for e in s.events if e.tick == 3])
        assert rb.state_at("main", 4).status == "ok"
        assert rb.outline().highestTick == 4
    finally:
        h.close()


@pytest.mark.parametrize("kind", KINDS)
def test_an_empty_or_unknown_exploration_hydrates_to_missing_events(kind):
    h = Harness(kind)
    try:
        rb = hydrate(h.store, "nothing-here", 200)
        assert rb.state_at("main", 1).status == "missing_events"
        assert rb.outline() is None
    finally:
        h.close()


def test_outline_is_cached_until_something_new_is_added():
    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    rb = HydratedRebuilder(7)
    rb.add(s.events[:1])
    first = rb.outline_result()
    assert rb.outline_result() is first
    rb.add(s.events[1:])
    assert rb.outline().highestTick == 2
    rb.add([])
    assert rb.outline().highestTick == 2


def test_outline_with_a_conflict_on_an_event_that_lost_a_tick_clash():
    """Was a `KeyError` in `history/outline.py`; the hydrated outline now equals the direct one."""
    import uuid

    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    first = s.events[0]
    twin = first.model_copy(update={"eventId": uuid.uuid4()})
    changed = first.model_copy(update={"wallTimeMs": first.wallTimeMs + 1})
    delivery = [twin, first, changed]
    for kind in KINDS:
        h = Harness(kind)
        try:
            held = held_events(delivery, h.can_hold_clash)
            h.load_exact(EXP_S, held)
            hydrated = hydrate(h.store, EXP_S, 7)
            hydrated.add(delivery)
            direct = Rebuilder(7)
            direct.add(held)
            direct.add(delivery)
            assert_same(hydrated, direct, held + delivery, ["main"], kind)
        finally:
            h.close()


def test_a_planted_clash_is_hydrated_in_arrival_order_even_when_an_index_would_sort_by_event_id():
    """Two different events at tick 2; the first to arrive has the LARGER event id. Which one
    wins the tick changes every later answer, so hydration must hand them over in arrival
    order. A non-unique index on (exploration, tick, event id) is the planner's temptation to
    return event-id order; only the `id` tie-break in `events_for` prevents it (reviewer,
    mutation `ORDER BY tick`). SQLite only: the in-memory store cannot hold a clash."""
    import uuid

    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    s.edit("c1", "x = 3")
    e1, e2, e3 = s.events
    late_id = e2.model_copy(update={"eventId": uuid.UUID("00000000-0000-4000-8000-000000000001")})
    early_id = e2.model_copy(update={"eventId": uuid.UUID("ffffffff-0000-4000-8000-000000000001")})
    assert str(late_id.eventId) < str(early_id.eventId)
    arrival = [e1, early_id, late_id, e3]  # larger id first
    h = Harness("sqlite")
    try:
        h.load_exact(EXP, arrival)
        h.store._conn.execute("CREATE INDEX events_probe ON events(exploration_id, tick, event_id)")
        direct = Rebuilder(7)
        direct.add(arrival)
        for interval in (7, None):
            hydrated = hydrate(h.store, EXP, interval)
            assert [str(e.eventId) for e in h.store.events_for(EXP)] == [str(e.eventId) for e in arrival]
            assert_same(hydrated, direct, arrival, ["main"], f"interval {interval}")
        assert dump(hydrated.state_at("main", 3)) == dump(direct.state_at("main", 3))
        assert hydrated.outline_result().problem.eventId == str(late_id.eventId)  # the later arrival lost
    finally:
        h.close()

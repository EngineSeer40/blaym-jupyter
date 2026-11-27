"""Rebuild equivalence through the port.

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

from blaym_jupyter.history import Rebuilder, build_outline
from blaym_jupyter.service.hydrate import HydratedRebuilder, hydrate
from blaym_jupyter.tests.history.conftest import MAX_EVENTS
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.ports.harness import KINDS, Harness
from blaym_jupyter_types import BlaymEvent

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rebuild"


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
@settings(max_examples=50)
@given(histories(max_events=MAX_EVENTS))
def test_generated_valid_histories_through_the_store(kind, history):
    check_through_store(kind, history.events[0].explorationId, history.events,
                        branches_of(history.events, "ghost-branch"), 7)



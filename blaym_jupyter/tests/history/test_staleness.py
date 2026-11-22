"""Staleness checks for the real rebuilder (S3a, Task 5 gate).

The original Blaym served stale state after branch switches and seeks. Here the cache is
warmed on every branch, then events arrive late, twice, in conflict, in clash, as new
branch points and in many tiny batches, with queries in random order in between. After
every step the rebuilder must equal `reference_rebuild` over the events delivered so far.
"""

from __future__ import annotations

import random
import uuid

import pytest
from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import Rebuilder, reference_rebuild, reference_replay_all
from blaym_jupyter.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.history.scenario import Scenario


def dump(r):
    return r.model_dump(mode="json")


def check_all(rb, delivered, branches, last, extra_branch="ghost"):
    positions = [(b, t) for b in list(branches) + [extra_branch] for t in range(0, last + 2)]
    wants = reference_replay_all(delivered, positions)  # one replay per line when the history is clean
    for b, t in positions:
        assert dump(rb.state_at(b, t)) == dump(wants[(b, t)]), (b, t)


def _twins(events, rnd):
    """Extra deliveries: duplicates, one conflict twin, one clash twin."""
    e = rnd.choice(events)
    conflict = e.model_copy(update={"wallTimeMs": e.wallTimeMs + 1})
    e2 = rnd.choice(events)
    clash = e2.model_copy(update={"eventId": uuid.uuid5(uuid.NAMESPACE_URL, f"clash:{e2.eventId}")})
    return conflict, clash


@settings(max_examples=100 if not DEEP else 1000)
@given(h=histories(max_events=MAX_EVENTS), seed=st.integers(0, 2**32), interval=st.sampled_from([1, 2, 3, 7, 200]),
       use_extras=st.booleans())
def test_tiny_batches_late_events_and_extras_never_serve_stale_answers(h, seed, interval, use_extras):
    rnd = random.Random(seed)
    events = list(h.events)
    rb = Rebuilder(interval=interval)
    last = h.last_tick

    # warm with a random subset that is a tick-prefix on some branches only (holes elsewhere)
    withheld = {e.tick for e in events if rnd.random() < 0.25}
    pool = [e for e in events if e.tick in withheld]
    delivered = [e for e in events if e.tick not in withheld]
    rb.add(delivered)
    check_all(rb, delivered, h.branches, last)  # warms snapshots on every branch

    extras = list(_twins(events, rnd)) if use_extras else []
    pool += extras + [rnd.choice(events) for _ in range(3)]  # plus duplicates
    rnd.shuffle(pool)
    while pool:
        batch = [pool.pop() for _ in range(rnd.randint(1, 2)) if pool]
        if not batch:
            break
        rb.add(batch)
        delivered += batch
        for _ in range(4):  # queries in random order
            b = rnd.choice(list(h.branches) + ["ghost"])
            t = rnd.randint(0, last + 1)
            assert dump(rb.state_at(b, t)) == dump(reference_rebuild(delivered, b, t)), (b, t)
    check_all(rb, delivered, h.branches, last)


def test_late_branch_point_for_a_new_branch_below_cached_snapshots():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 30):
        s.edit("c1", f"x = {i}")
    s.branch_from(tick=5, name="b")  # parentTick far below main's cached snapshots
    for i in range(10):
        s.edit("c1", f"b = {i}")
    held = s.events[30]  # the branch point
    assert held.type == "branch_point_created"
    rest = [e for e in s.events if e is not held]
    rb = Rebuilder(interval=3)
    rb.add(rest)
    # tick 30 missing: only ticks below it answer ok, and they warm main's snapshots
    for t in range(1, 30):
        assert dump(rb.state_at("main", t)) == dump(reference_rebuild(rest, "main", t))
    assert len(rb.snapshots) > 0
    rb.add([held])
    check_all(rb, s.events, ["main", "b"], s.tick)


def test_snapshot_on_an_ancestor_below_the_cut_is_dropped_when_an_earlier_event_lands():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 20):
        s.edit("c1", f"x = {i}")
    s.branch_from(tick=15, name="b")
    for i in range(10):
        s.edit("c1", f"b = {i}")
    held = s.events[7]
    rest = [e for e in s.events if e is not held]
    # now a rebuilder that had everything except `held`, warmed, then gets it
    rb2 = Rebuilder(interval=2)
    rb2.add(s.events[:7])
    check_all(rb2, s.events[:7], ["main"], 7)
    rb2.add(s.events[8:])
    rb2.add([held])
    check_all(rb2, s.events, ["main", "b"], s.tick)


def test_conflict_arriving_late_invalidates_every_warm_snapshot():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 15):
        s.edit("c1", f"x = {i}")
    s.branch_from(tick=10, name="b")
    for i in range(6):
        s.edit("c1", f"b = {i}")
    rb = Rebuilder(interval=2)
    rb.add(s.events)
    check_all(rb, s.events, ["main", "b"], s.tick)
    e = s.events[3]
    twin = e.model_copy(update={"wallTimeMs": e.wallTimeMs + 1})
    rb.add([twin])
    check_all(rb, s.events + [twin], ["main", "b"], s.tick)
    # a duplicate afterwards changes nothing and keeps answering the conflict
    rb.add([e])
    check_all(rb, s.events + [twin, e], ["main", "b"], s.tick)


def test_clash_arriving_late_invalidates_every_warm_snapshot():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 15):
        s.edit("c1", f"x = {i}")
    s.branch_from(tick=10, name="b")
    s.edit("c1", "b")
    rb = Rebuilder(interval=2)
    rb.add(s.events)
    check_all(rb, s.events, ["main", "b"], s.tick)
    e = s.events[12]
    twin = e.model_copy(update={"eventId": uuid.uuid5(uuid.NAMESPACE_URL, "clash")})
    rb.add([twin])
    check_all(rb, s.events + [twin], ["main", "b"], s.tick)


def test_returned_states_are_independent_copies_even_on_a_snapshot_hit():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 9):
        s.edit("c1", f"x = {i}")
    s.branch_from(tick=6, name="b")
    s.edit("c1", "b = 1")
    rb = Rebuilder(interval=2)
    rb.add(s.events)
    for branch, tick in (("main", 6), ("main", 8), ("b", 10), ("b", 6), ("b", 3)):
        first = rb.state_at(branch, tick)
        want = dump(reference_rebuild(s.events, branch, tick))
        assert dump(first) == want
        st_ = first.state
        st_.notebook.cells[0].code = "MUTATED"
        st_.notebook.cells[0].outputs.append("x")
        st_.position.tick = 999
        again = rb.state_at(branch, tick)
        assert dump(again) == want


def test_snapshots_are_keyed_per_branch_and_ancestor_hits_are_not_confused_with_descendants():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 9):
        s.edit("c1", f"m{i}")
    s.branch_from(tick=5, name="b1")
    for i in range(6):
        s.edit("c1", f"b1-{i}")
    s.branch_from(tick=5, name="b2", parent="main")  # sibling, same parentTick
    for i in range(6):
        s.edit("c1", f"b2-{i}")
    rb = Rebuilder(interval=2)
    rb.add(s.events)
    order = [(b, t) for b in ("b1", "b2", "main") for t in range(1, s.tick + 1)]
    random.Random(1).shuffle(order)
    for b, t in order:
        assert dump(rb.state_at(b, t)) == dump(reference_rebuild(s.events, b, t)), (b, t)
    keys = rb.snapshots.keys()
    assert len(keys) == len(set(keys))


def test_state_at_really_jumps_from_a_snapshot_and_never_calls_the_reference(monkeypatch):
    import blaym_jupyter.history.rebuild as mod
    import blaym_jupyter.history.reference as ref

    assert not hasattr(mod, "reference_rebuild")
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 40):
        s.edit("c1", f"x = {i}")
    rb = Rebuilder(interval=5)
    rb.add(s.events)
    rb.state_at("main", 40)
    applied = []
    orig = mod.step
    monkeypatch.setattr(mod, "step", lambda st_, ev: (applied.append(ev.tick), orig(st_, ev))[1])
    monkeypatch.setattr(ref, "reference_rebuild", lambda *a, **k: pytest.fail("reference called"))
    rb.state_at("main", 33)
    assert rb.last_base == ("main", 30)
    assert applied == [31, 32, 33]


def test_mutating_an_event_after_add_does_not_change_answers():
    s = Scenario()
    s.start(cells=[("c1", "x = 0"), ("c2", "keep")])
    s.edit("c2", "c2 v2")  # tick 2, never overwritten afterwards
    for i in range(1, 12):
        s.edit("c1", f"x = {i}")
    rb = Rebuilder(interval=4)
    rb.add(s.events)
    rb.state_at("main", 13)  # snapshots at ticks 4, 8, 12
    assert len(rb.snapshots) == 3
    s.events[1].payload.code = "TAMPERED"  # the caller mutates its own object afterwards
    early = rb.state_at("main", 3).state.notebook.cells[1].code  # starts from scratch: sees the tampered edit
    late = rb.state_at("main", 13).state.notebook.cells[1].code  # starts from snapshot 12
    assert early == late

"""The real rebuilder against the reference (S3a, Task 5).

The core equivalence: for every input, `Rebuilder.state_at` gives exactly the answer of
`reference_rebuild` (`model_dump(mode="json")` equal), whatever the snapshot interval,
the batches the events arrive in, or what was asked before. Task 6 adds the full
property set.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import Rebuilder, reference_rebuild, reference_replay_all
from blaym_jupyter.tests.history.conftest import DEEP, DELIVERY_SETTINGS, MAX_EVENTS
from blaym_jupyter.tests.history.generate import deliveries, histories
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.history.test_generator import _positions

INTERVALS = (1, 7, 50, 200, None)


def dump(result):
    return result.model_dump(mode="json")


def reference_answers(events, positions):
    """The reference's answer once per position, from one straight replay per branch line
    (`reference_replay_all`, proved equal to `reference_rebuild` in test_reference)."""
    return {p: dump(r) for p, r in reference_replay_all(events, positions).items()}


def assert_same(rb, events, positions, wants=None):
    wants = wants if wants is not None else reference_answers(events, positions)
    for branch, tick in positions:
        assert dump(rb.state_at(branch, tick)) == wants[(branch, tick)], (branch, tick)


def _long_scenario(n=12):
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, n):
        s.edit("c1", f"x = {i}")
    return s


# --- hand cases: jump paths -------------------------------------------------------


def test_jump_uses_a_snapshot_on_the_own_branch():
    s = _long_scenario()
    rb = Rebuilder(interval=4)
    rb.add(s.events)
    rb.state_at("main", 6)  # puts snapshots at own events 4
    assert ("main", 4) in rb.snapshots.keys()
    got = rb.state_at("main", 7)
    assert rb.last_base == ("main", 4)
    assert dump(got) == dump(reference_rebuild(s.events, "main", 7))


def test_jump_uses_a_snapshot_on_an_ancestor_up_to_the_cut():
    s = _long_scenario()
    s.branch_from(tick=10, name="b")
    s.edit("c1", "y = 1")
    s.edit("c1", "y = 2")
    rb = Rebuilder(interval=4)
    rb.add(s.events)
    rb.state_at("main", 12)  # main snapshots at 4 and 8 (own events); 12 not reached
    got = rb.state_at("b", s.tick)
    assert rb.last_base is not None and rb.last_base[0] == "main"
    assert rb.last_base[1] <= 10
    assert dump(got) == dump(reference_rebuild(s.events, "b", s.tick))
    # beyond the cut, ancestor events must not be used
    assert dump(rb.state_at("b", 11)) == dump(reference_rebuild(s.events, "b", 11))


def test_jump_with_no_snapshot_starts_from_the_beginning():
    s = _long_scenario()
    for interval in (None, 100):
        rb = Rebuilder(interval=interval)
        rb.add(s.events)
        got = rb.state_at("main", s.tick)
        assert rb.last_base is None
        assert len(rb.snapshots) == 0
        assert dump(got) == dump(reference_rebuild(s.events, "main", s.tick))


def test_interval_one_snapshots_every_event_and_stays_correct():
    s = _long_scenario()
    rb = Rebuilder(interval=1)
    rb.add(s.events)
    rb.state_at("main", s.tick)
    assert len(rb.snapshots) == s.tick
    assert_same(rb, s.events, [("main", t) for t in range(1, s.tick + 1)])


def test_bad_interval_is_refused():
    with pytest.raises(ValueError):
        Rebuilder(interval=0)


# --- late arrival, conflicts, clashes never serve a stale snapshot -----------------


def test_late_arriving_earlier_tick_drops_snapshots_and_answers_stay_right():
    s = _long_scenario()
    held = s.events[3]  # tick 4
    rest = [e for e in s.events if e is not held]
    rb = Rebuilder(interval=2)
    rb.add(rest)
    assert rb.state_at("main", 6).status == "missing_events"
    rb.add(rest[:2])  # duplicates change nothing
    rb.add([held])
    assert_same(rb, s.events, [("main", t) for t in range(1, s.tick + 1)])
    # warm the cache, then add the next event and check again
    rb2 = Rebuilder(interval=2)
    rb2.add(s.events[:8])
    assert_same(rb2, s.events[:8], [("main", t) for t in range(1, 9)])
    rb2.add(s.events[8:])
    assert_same(rb2, s.events, [("main", t) for t in range(1, s.tick + 1)])


def test_conflict_and_clash_after_warm_cache():
    s = _long_scenario()
    rb = Rebuilder(interval=2)
    rb.add(s.events)
    assert rb.state_at("main", 8).status == "ok"
    altered = s.events[2].model_copy(update={"wallTimeMs": s.events[2].wallTimeMs + 1})
    rb.add([altered])
    assert dump(rb.state_at("main", 8)) == dump(reference_rebuild(s.events + [altered], "main", 8))
    assert rb.state_at("main", 8).status == "conflict"

    rb = Rebuilder(interval=2)
    rb.add(s.events)
    rb.state_at("main", 8)
    twin = s.events[5].model_copy(update={"eventId": "00000000-0000-4000-8000-000000000042"})
    rb.add([twin])
    assert rb.state_at("main", 8).status == "invalid_event"
    assert dump(rb.state_at("main", 3)) == dump(reference_rebuild(s.events + [twin], "main", 3))


def test_returned_states_are_independent_copies():
    s = _long_scenario()
    rb = Rebuilder(interval=3)
    rb.add(s.events)
    a = rb.state_at("main", 7)
    a.state.notebook.cells[0].code = "tampered"
    a.state.position.tick = 99
    b = rb.state_at("main", 7)
    assert b.state.notebook.cells[0].code != "tampered"
    assert dump(b) == dump(reference_rebuild(s.events, "main", 7))


def test_real_rebuilder_reports_missing_events_for_a_branch_point_at_a_missing_tick():
    s = _long_scenario(4)
    s.branch_from(tick=3, name="b")
    s.edit("c1", "y")
    rb = Rebuilder()
    rb.add([e for e in s.events if e.tick != 3])
    r = rb.state_at("b", 1)
    assert r.status == "missing_events" and r.firstMissingTick == 3


# --- the core equivalence ----------------------------------------------------------


@settings(max_examples=1000 if DEEP else 100)
@given(histories(max_events=MAX_EVENTS), st.sampled_from(INTERVALS))
def test_valid_histories_match_the_reference_at_sampled_positions(h, interval):
    rb = Rebuilder(interval=interval)
    rb.add(h.events)
    pos = _positions(h)
    wants = reference_answers(h.events, pos)
    assert_same(rb, h.events, pos, wants)
    assert_same(rb, h.events, list(reversed(pos)), wants)  # warm cache, other order


def _extra_positions(d):
    h = d.history
    pos = list(_positions(h))
    branches = list(h.branches) + [d.unknown_branch or "ghost-branch"]
    pos += [(b, t) for b in branches for t in (1, h.last_tick, h.last_tick + 1)]
    for t in d.dropped_ticks:
        pos += [(b, t + k) for b in h.branches for k in (-1, 0, 1) if t + k >= 1]
    for t in d.clash_ticks:
        pos += [(h.branches[0], t)]
    return pos


def small_deliveries():
    return histories(max_events=MAX_EVENTS).flatmap(lambda h: deliveries(history=h))


@DELIVERY_SETTINGS
@given(small_deliveries(), st.sampled_from(INTERVALS))
def test_deliveries_match_the_reference_in_one_batch(d, interval):
    rb = Rebuilder(interval=interval)
    rb.add(d.events)
    assert_same(rb, d.events, _extra_positions(d))


@DELIVERY_SETTINGS
@given(small_deliveries(), st.sampled_from(INTERVALS), st.data())
def test_deliveries_match_the_reference_across_batches_with_a_warm_cache(d, interval, data):
    events = list(d.events)
    cuts = sorted(
        data.draw(st.lists(st.integers(0, len(events)), max_size=4)) + [len(events)]
    )
    rb = Rebuilder(interval=interval)
    pos = _extra_positions(d)
    start = 0
    for cut in cuts:
        rb.add(events[start:cut])
        start = cut
        sample = data.draw(st.lists(st.sampled_from(pos), min_size=1, max_size=8))
        seen = events[:cut]
        if seen:  # nothing to compare before the first event: the reference needs a tick 1
            assert_same(rb, seen, sample)
    assert_same(rb, events, pos)


def test_asks_below_the_first_event_and_on_nothing_are_typed_answers():
    s = _long_scenario(3)
    for tick in (0, -1):
        for events in ([], s.events):
            rb = Rebuilder()
            rb.add(events)
            r = rb.state_at("main", tick)
            assert r.status == "missing_events"
            assert dump(r) == dump(reference_rebuild(events, "main", tick))

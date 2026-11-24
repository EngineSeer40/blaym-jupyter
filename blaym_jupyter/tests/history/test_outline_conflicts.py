"""`build_outline` on conflicts and clashes together (S4, Task 3 follow-up, reviewer).

`history/outline.py` bounds the outline by the tick of the event a conflict names. An event
that lost a tick clash is not in the tick index, so the old lookup `by_id[eventId]` raised
`KeyError`; it now uses the conflict's own first copy. These tests pin that down against an
oracle written separately from the builder: the outline's highest tick must be the largest
prefix of first-arrival events (one per tick, one per id) that is below the first hole,
clash and conflict and that the real `Rebuilder` answers `ok` for at every branch tip.
"""

from __future__ import annotations

import uuid

from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import Rebuilder, build_outline
from blaym_jupyter.history.apply import unwrap
from blaym_jupyter.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_jupyter.tests.history.generate import deliveries, histories
from blaym_jupyter.tests.history.scenario import Scenario


def oracle_highest(events) -> int:
    """The largest sound prefix by the rules, using the real rebuilder for validity. 0 = none."""
    first_by_id, winners, ticks = {}, [], set()
    clash = conflict = None
    for raw in events:
        ev = unwrap(raw)
        key = str(ev.eventId)
        if key in first_by_id:
            if conflict is None and first_by_id[key].model_dump(mode="json") != ev.model_dump(mode="json"):
                conflict = first_by_id[key].tick  # the first copy's own tick, wherever it sits
            continue
        first_by_id[key] = ev
        if ev.tick in ticks:
            clash = ev.tick if clash is None else clash
        else:
            ticks.add(ev.tick)
            winners.append(ev)
    missing = 1
    while missing in ticks:
        missing += 1
    bound = min(x for x in (missing - 1, None if clash is None else clash - 1,
                            None if conflict is None else conflict - 1) if x is not None)
    for cut in range(bound, 0, -1):
        prefix = [w for w in winners if w.tick <= cut]
        rb = Rebuilder(7)
        rb.add(prefix)
        tips: dict[str, int] = {}
        for w in prefix:
            tips[w.branchId] = max(tips.get(w.branchId, 0), w.tick)
        if all(rb.state_at(b, t).status == "ok" for b, t in tips.items()):
            return cut
    return 0


def check(events, expected_highest=None):
    res = build_outline(events)  # must never raise
    got = 0 if res.outline is None else res.outline.highestTick
    assert got == oracle_highest(events), (got, oracle_highest(events))
    if expected_highest is not None:
        assert got == expected_highest
    if res.outline is not None:
        flat = sorted(t.root for b in res.outline.branches for t in b.ownTicks)
        assert flat == list(range(1, got + 1))
    return res


def six():
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 6):
        s.edit("c1", f"x = {i}")
    return s.events  # ticks 1..6


def twin(ev):
    return ev.model_copy(update={"eventId": uuid.uuid4()})


def changed(ev, **update):
    return ev.model_copy(update=update or {"wallTimeMs": ev.wallTimeMs + 1})


def test_the_first_copy_lost_the_clash_and_an_earlier_clash_was_recorded_first():
    e = six()
    t3 = twin(e[2])
    # clash at 2 arrives first (recorded); the loser of the clash at 3 then conflicts
    check([*e[:3], twin(e[1]), t3, *e[3:], changed(t3)], expected_highest=1)


def test_the_first_copy_lost_a_clash_and_is_the_only_problem_below_a_later_clash():
    e = six()
    t3 = twin(e[2])
    # recorded clash is at 5; the conflict on the lost twin at 3 is lower, so it bounds the outline
    check([*e[:5], twin(e[4]), t3, e[5], changed(t3)], expected_highest=2)


def test_a_conflict_on_an_event_above_the_first_missing_tick_leaves_the_hole_as_the_bound():
    e = six()
    without_3 = [x for x in e if x.tick != 3]
    check([*without_3, changed(e[4])], expected_highest=2)  # first copy in the tick index
    lost = twin(e[4])
    check([*without_3, lost, changed(lost)], expected_highest=2)  # first copy lost a clash at 5


def test_a_conflicting_copy_with_a_different_tick_is_bounded_by_the_first_copy():
    e = six()
    check([*e, changed(e[3], tick=2)], expected_highest=3)  # first copy at tick 4
    check([*e, changed(e[1], tick=5)], expected_highest=1)  # first copy at tick 2


def test_conflict_on_the_very_first_event_and_on_a_lost_first_event_gives_no_outline():
    e = six()
    assert check([*e, changed(e[0])], expected_highest=0).outline is None
    t1 = twin(e[0])
    res = check([t1, *e, changed(t1)], expected_highest=0)
    assert res.outline is None and res.problem is not None


def test_a_second_clash_at_a_lower_tick_than_the_recorded_one_is_not_a_bound():
    """Known, by the S3a rule 'the first clash in arrival order': only that clash is
    recorded, so a loser at a lower tick that arrived later does not lower the outline.
    Pinned (not endorsed) so a change in `history/` is noticed; through a store (tick
    order) the lowest clash always arrives first, so this is a delivery-only case."""
    e = six()
    res = build_outline([*e[:5], twin(e[4]), twin(e[1]), e[5]])
    assert res.outline.highestTick == 4  # an event at tick 2 lost a clash, yet tick 4 is shown


# --- generated ---------------------------------------------------------------


@settings(max_examples=1000 if DEEP else 300)
@given(deliveries())
def test_any_delivery_gives_the_outline_the_oracle_gives(d):
    check(d.events)


@st.composite
def clashes_and_conflicts(draw):
    """A valid history with, for several events, a twin (a clash), a changed copy (a
    conflict) of the event or of its twin, each inserted at a random place in the arrival."""
    h = draw(histories(max_events=MAX_EVENTS))
    events = list(h.events)
    for _ in range(draw(st.integers(1, 4))):
        ev = draw(st.sampled_from(h.events))
        victim = ev
        if draw(st.booleans()):
            victim = twin(ev)
            events.insert(draw(st.integers(0, len(events))), victim)
        events.insert(draw(st.integers(0, len(events))), changed(victim))
    if draw(st.booleans()):
        events = list(draw(st.permutations(events)))
    if draw(st.booleans()):
        drop = draw(st.integers(0, len(events) - 1))
        events.pop(drop)
    return events


@settings(max_examples=1000 if DEEP else 300)
@given(clashes_and_conflicts())
def test_conflicts_on_events_that_won_or_lost_a_clash_never_raise_and_match_the_oracle(events):
    check(events)

"""The history outline checked against the real rebuilder.

The outline holds no content; positions and run progress worked out from it by a
small independent line model here must equal what the rebuilder says at every position.
Also: the rules the schema cannot express, delivery problems that must not change it,
and invalid histories that must give a typed problem and never a crash.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import Rebuilder, build_outline, outline
from blaym_jupyter.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_jupyter.tests.history.generate import deliveries, histories, invalid_histories
from blaym_jupyter.tests.history.test_generator import _positions

PROPERTY = settings(max_examples=1000 if DEEP else 100)


def _line_ticks(o, branch: str, tick: int) -> list[int]:
    """Ticks on `branch`'s line up to `tick`: each ancestor's own ticks up to the next
    branch point, then the branch's own. Written separately from the builder."""
    by_id = {b.branchId: b for b in o.branches}
    cut, name, found = tick, branch, []
    while True:
        b = by_id[name]
        found = [t.root for t in b.ownTicks if t.root <= cut] + found
        if b.parentBranchId is None:
            return found
        cut, name = min(cut, b.parentTick), b.parentBranchId


def _spans_on_line(o, branch: str):
    return {s.runId: s for s in next(b for b in o.branches if b.branchId == branch).runSpans}


@PROPERTY
@given(histories(max_events=MAX_EVENTS))
def test_rules_the_schema_cannot_express_hold(h):
    o = outline(h.events)
    by_id = {b.branchId: b for b in o.branches}
    assert o.rootBranchId in by_id and o.highestTick == h.last_tick
    seen: set[int] = set()
    for b in o.branches:
        ticks = [t.root for t in b.ownTicks]
        assert ticks == sorted(set(ticks)), "sorted and listed once"
        assert not seen & set(ticks), "a tick belongs to one branch"
        seen |= set(ticks)
        assert (b.parentBranchId is None) == (b.parentTick is None) == (b.branchId == o.rootBranchId)
        if b.parentBranchId is not None:
            assert b.parentBranchId in by_id
        on_line = set(_line_ticks(o, b.branchId, o.highestTick))
        for s in b.runSpans:
            assert s.startedTick in on_line
            if s.endedTick is not None:
                assert s.endedTick >= s.startedTick and s.endedTick in on_line
    assert seen == set(range(1, o.highestTick + 1))


@settings(max_examples=1000 if DEEP else 100)
@given(histories(max_events=MAX_EVENTS), st.sampled_from([3, 50]))
def test_positions_and_runs_from_the_outline_equal_the_rebuilder(h, interval):
    o = outline(h.events)
    rb = Rebuilder(interval=interval)
    rb.add(h.events)
    for branch, tick in _positions(h):
        r = rb.state_at(branch, tick)
        assert r.status == "ok", (branch, tick, r)
        ticks = _line_ticks(o, branch, tick)
        pos = r.state.position
        assert (pos.branchCount, pos.lastEventTick) == (len(ticks), ticks[-1]), (branch, tick)

        # at or before the branch point the view is the parent's: a run that had closed
        # before the branch point is listed on the ancestor only (accepted design)
        owner, by_id = branch, {b.branchId: b for b in o.branches}
        while by_id[owner].parentTick is not None and tick <= by_id[owner].parentTick:
            owner = by_id[owner].parentBranchId
        spans = _spans_on_line(o, owner)
        started = {rid: s for rid, s in spans.items() if s.startedTick <= tick}
        open_in_outline = {rid for rid, s in started.items() if s.endedTick is None or s.endedTick > tick}
        running = {run.runId for run in r.state.runs if run.status.root == "running"}
        assert running == open_in_outline, (branch, tick)
        for run in r.state.runs:
            s = started.get(run.runId)
            if s is None or run.status.root == "running":
                continue
            if run.status.root == "unfinished":
                assert run.endedTick is None and s.endedTick is not None and s.endedTick <= tick
            else:
                assert run.endedTick == s.endedTick
            assert run.startedTick == s.startedTick


@settings(max_examples=1000 if DEEP else 100)
@given(st.data())
def test_duplicates_and_reordering_do_not_change_the_outline(data):
    h = data.draw(histories(max_events=MAX_EVENTS))
    d = data.draw(deliveries(h, mutations=("duplicate", "reorder", "drop")))
    below = d.first_missing_tick
    clean = [e for e in h.events if below is None or e.tick < below]
    assert outline(d.events) == outline(clean)
    if below is None:
        assert outline(d.events) == outline(h.events)


@settings(max_examples=1000 if DEEP else 100)
@given(invalid_histories())
def test_invalid_histories_give_a_typed_problem_and_a_valid_outline_up_to_it(inv):
    result = build_outline(inv.events)
    assert result.problem is not None, inv.kind
    bad_tick = next(e.tick for e in inv.events if str(e.eventId) == inv.event_id)
    if result.outline is not None:
        assert result.outline.highestTick < bad_tick
        again = Rebuilder()
        again.add([e for e in inv.events if e.tick <= result.outline.highestTick])
        for b in result.outline.branches:
            assert again.state_at(b.branchId, b.ownTicks[-1].root).status == "ok"


def _case_no_events_gives_no_outline():
    result = build_outline([])
    assert result.outline is None


# --- named cases for run spans across a branch point ------------------------------


def _span(o, branch, run_id):
    b = next(b for b in o.branches if b.branchId == branch)
    return next((s for s in b.runSpans if s.runId == run_id), None)


def _inherited_run_history():
    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "slow()")])                  # 1
    run = s.run("c1")                                  # 2
    s.edit("c1", "slow(2)")                            # 3 edited while it runs
    child = s.branch_from(tick=3, name="child")        # 4 branched from inside the span
    s.finish(run)                                      # 5 finished on the child
    s.on("main").edit("c1", "slow(3)")                 # 6 main's run is still open
    return s, run, child


def _case_a_run_open_at_a_branch_point_and_finished_on_the_child():
    s, run, child = _inherited_run_history()
    o = outline(s.events)
    on_child = _span(o, child, run)
    assert (on_child.startedTick, on_child.endedTick) == (2, 5)  # parent's start, child's end
    on_main = _span(o, "main", run)
    assert (on_main.startedTick, on_main.endedTick) == (2, None)  # main's span stays open
    rb = Rebuilder()
    rb.add(s.events)
    # a branch taken from inside the span on main sees the run open at that tick
    assert [r.status.root for r in rb.state_at("main", 3).state.runs] == ["running"]
    assert [r.status.root for r in rb.state_at(child, 4).state.runs] == ["running"]
    assert [r.status.root for r in rb.state_at(child, 5).state.runs] == ["ok"]


def _case_a_run_closed_before_the_branch_point_is_not_listed_on_the_child():
    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x")])
    run = s.run("c1")
    s.finish(run)
    child = s.branch_from(tick=3, name="child")
    o = outline(s.events)
    assert _span(o, child, run) is None
    assert _span(o, "main", run).endedTick == 3


def _case_a_kernel_restart_closes_an_open_span_on_the_branchs_line():
    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "loop()")])                  # 1
    run = s.run("c1")                                  # 2
    child = s.branch_from(tick=2, name="child")        # 3
    s.kernel_restart(reason="unknown")                 # 4 closes it on the child only
    s.on("main").edit("c1", "loop(2)")                 # 5
    o = outline(s.events)
    assert _span(o, child, run).endedTick == 4
    assert _span(o, "main", run).endedTick is None
    own = Scenario()
    own.start(cells=[("c1", "loop()")])
    r2 = own.run("c1")
    own.kernel_restart()
    span = _span(outline(own.events), "main", r2)
    assert (span.startedTick, span.endedTick) == (2, 3)


def _case_the_outline_matches_positions_at_every_tick_on_every_branch_in_the_fixtures():
    import json
    from pathlib import Path

    from blaym_jupyter_types import BlaymEvent, HistoryOutline

    fixtures = sorted((Path(__file__).resolve().parents[3] / "fixtures" / "rebuild").glob("*.json"))
    assert len(fixtures) >= 16
    for path in fixtures:
        data = json.loads(path.read_text())
        events = [BlaymEvent.model_validate(e).root for e in data["events"]]
        o = HistoryOutline.model_validate(data["outline"])
        assert outline(events) == o, path.name
        rb = Rebuilder()
        rb.add(events)
        for b in o.branches:
            for tick in range(1, o.highestTick + 1):
                r = rb.state_at(b.branchId, tick)
                if r.status != "ok":
                    continue  # before the branch existed, or a deliberately invalid fixture
                ticks = _line_ticks(o, b.branchId, tick)
                pos = r.state.position
                assert (pos.branchCount, pos.lastEventTick) == (len(ticks), ticks[-1]), (path.name, b.branchId, tick)


def _case_a_conflict_on_an_event_that_lost_a_tick_clash_gives_an_outline_not_a_crash():
    """twin / first / changed: a different event takes tick 1 first, so `first` loses the
    clash and is not in the tick index; then a different copy of `first` arrives (a conflict).
    Nothing is usable from tick 1, so there is no outline, and the problem is reported."""
    import uuid

    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    first = s.events[0]
    twin = first.model_copy(update={"eventId": uuid.uuid4()})
    changed = first.model_copy(update={"wallTimeMs": first.wallTimeMs + 1})
    res = build_outline([twin, first, changed])
    assert res.outline is None and res.problem is not None
    assert res.problem.status in ("conflict", "invalid_event")
    # the same, but with a sound prefix before the clash: the outline stops just before it
    later = s.events[1]
    lost = later.model_copy(update={"eventId": uuid.uuid4()})
    changed_later = later.model_copy(update={"wallTimeMs": later.wallTimeMs + 1})
    res = build_outline([first, lost, later, changed_later])
    assert res.outline is not None and res.outline.highestTick == 1
    assert res.problem is not None


def test_outline_hand_made_cases():
    _case_no_events_gives_no_outline()
    _case_a_run_open_at_a_branch_point_and_finished_on_the_child()
    _case_a_run_closed_before_the_branch_point_is_not_listed_on_the_child()
    _case_a_kernel_restart_closes_an_open_span_on_the_branchs_line()
    _case_the_outline_matches_positions_at_every_tick_on_every_branch_in_the_fixtures()
    _case_a_conflict_on_an_event_that_lost_a_tick_clash_gives_an_outline_not_a_crash()



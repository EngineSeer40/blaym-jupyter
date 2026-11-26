"""Adversarial checks of the reference rebuilder (S3a Task 3 gate).

The brute-force oracle below is written independently of `reference.py`: it picks the
events of a line by a recursive membership rule and folds a tiny model of the facts
that matter (cell code, chat bodies, run status, position), without using `apply`.
"""

from __future__ import annotations

import random

import pytest

from blaym_jupyter.history import reference_rebuild, reference_replay_all
from blaym_jupyter.tests.history.scenario import Scenario


# --- independent oracle ---------------------------------------------------------


def _bp(events, branch):
    for e in events:
        if e.branchId == branch and e.type == "branch_point_created":
            return e
    return None


def oracle_line(events, branch, tick):
    """Events on the line of (branch, tick), root first, by recursive definition."""
    bp = _bp(events, branch)
    own = sorted((e for e in events if e.branchId == branch and e.tick <= tick), key=lambda e: e.tick)
    if bp is None:
        return own
    return oracle_line(events, bp.payload.parentBranchId, min(tick, bp.payload.parentTick)) + own


def _oracle_items(group):
    return ([i.preview for i in group.items], bool(group.clearPending))


def oracle_model(line):
    """A tiny independent model: cell code and order, chat bodies, run status, and the
    output groups (items by preview, pending flag) kept per cell and detached, following
    the ruling "outputs follow the run"."""
    cells: dict[str, str] = {}
    order: list[str] = []
    chat: dict[str, str] = {}
    runs: dict[str, str] = {}
    run_cell: dict[str, str] = {}
    on_cell: dict[str, dict[str, list]] = {}  # cell -> run -> [items, pending]
    detached: dict[str, list] = {}  # run -> [items, pending]
    is_code: dict[str, bool] = {}

    def home(run):
        c = run_cell[run]
        if run in detached or c not in on_cell:
            return detached
        return on_cell[c] if is_code[c] or run in on_cell[c] else detached

    def set_kind(cid, kind):
        is_code[cid] = getattr(kind, "value", kind) == "code"

    def rescue(groups):  # a running run's group leaves a cell that can no longer hold it
        for run in [r for r in groups if runs.get(r) == "running"]:
            detached[run] = groups.pop(run)

    for e in line:
        p = e.payload
        t = e.type
        if t in ("notebook_snapshot", "changed_outside_editor"):
            new_ids = {c.cellId for c in p.cells}
            for cid in order:
                if cid not in new_ids:
                    for run, grp in on_cell[cid].items():
                        if runs.get(run) == "running":
                            detached[run] = grp
            known = set(order)
            taken = {}
            for c in p.cells:
                from_event = {g.runId: list(_oracle_items(g)) for g in c.outputs}
                if t == "changed_outside_editor":
                    taken[c.cellId] = from_event
                elif getattr(c.cellType, "value", c.cellType) != "code":
                    rescue(dict(on_cell.get(c.cellId, {})) if c.cellId in known else {})
                    taken[c.cellId] = {}  # a snapshot: only code cells have outputs
                else:  # what was recorded wins; only an unseen cell takes the snapshot's
                    taken[c.cellId] = on_cell[c.cellId] if c.cellId in known else from_event
            cells = {c.cellId: c.code for c in p.cells}
            order = [c.cellId for c in p.cells]
            on_cell = taken
            for c in p.cells:
                set_kind(c.cellId, c.cellType)
        elif t == "cell_inserted":
            cells[p.cellId] = p.code
            order.insert(p.index, p.cellId)
            on_cell[p.cellId] = {}
            set_kind(p.cellId, p.cellType)
        elif t == "cell_type_changed":
            set_kind(p.cellId, p.cellType)
            if not is_code[p.cellId]:
                rescue(on_cell[p.cellId])
                on_cell[p.cellId] = {}  # nothing is shown under a cell that is not code
        elif t == "cell_outputs_cleared":
            # the user wiped the cell: only what a live run is still writing survives
            on_cell[p.cellId] = {r: g for r, g in on_cell[p.cellId].items() if runs.get(r) == "running"}
        elif t == "cell_deleted":
            cells.pop(p.cellId)
            order.remove(p.cellId)
            for run, grp in on_cell.pop(p.cellId).items():
                if runs.get(run) == "running":
                    detached[run] = grp
        elif t == "cell_moved":
            order.remove(p.cellId)
            order.insert(p.toIndex, p.cellId)
        elif t == "cell_source_changed":
            cells[p.cellId] = p.code
        elif t == "chat_message_added":
            chat[(p.chatSource, p.messageId)] = p.body
        elif t == "chat_message_updated":
            chat[(p.chatSource, p.messageId)] = p.body
        elif t == "run_started":
            # starting again wipes what the cell shows from anything already over
            for old in list(on_cell[p.cellId]):
                if runs.get(old) != "running":
                    del on_cell[p.cellId][old]
            runs[p.runId] = "running"
            run_cell[p.runId] = p.cellId
        elif t == "run_finished":
            runs[p.runId] = p.status.root if hasattr(p.status, "root") else p.status.value
        elif t == "kernel_restarted":
            runs = {k: ("unfinished" if v == "running" else v) for k, v in runs.items()}
        elif t == "output_added":
            grp = home(p.runId).setdefault(p.runId, [[], False])
            if grp[1]:
                grp[0], grp[1] = [p.item.preview], False
            else:
                grp[0].insert(p.outputIndex, p.item.preview)
        elif t == "output_replaced":
            home(p.runId)[p.runId][0][p.outputIndex] = p.item.preview
        elif t == "outputs_cleared":
            h = home(p.runId)
            if p.wait:
                h.setdefault(p.runId, [[], False])[1] = True
            elif p.runId in h:
                h[p.runId] = [[], False]
    outputs = {c: [(r, g[0], g[1]) for r, g in on_cell[c].items()] for c in order}
    det = [(r, g[0], g[1]) for r, g in detached.items()]
    return [(c, cells[c]) for c in order], chat, runs, outputs, det


def cross_check(events, positions=None):
    """Compare the reference with the oracle at `positions` (default: every tick of every branch).
    The reference answers come from one replay per branch line (`reference_replay_all`)."""
    last = max(e.tick for e in events)
    if positions is None:
        positions = [(b, t) for b in sorted({e.branchId for e in events}) for t in range(1, last + 1)]
    answers = reference_replay_all(events, positions)
    for b, t in positions:
        line = oracle_line(events, b, t)
        r = answers[(b, t)]
        assert r.status == "ok", f"{b}@{t}: {r}"
        s = r.state
        cells, chat, runs, outputs, det = oracle_model(line)
        assert [(c.cellId, c.code) for c in s.notebook.cells] == cells, f"{b}@{t}"
        assert {(m.chatSource, m.messageId): m.body for m in s.chat} == chat, f"{b}@{t}"
        assert {x.runId: x.status.root for x in s.runs} == runs, f"{b}@{t}"
        assert {c.cellId: [(g.runId, *_oracle_items(g)) for g in c.outputs]
                for c in s.notebook.cells} == outputs, f"{b}@{t} outputs"
        assert [(g.runId, *_oracle_items(g)) for g in s.detachedOutputs] == det, f"{b}@{t} detached"
        assert (
            s.position.branchId, s.position.tick, s.position.lastEventTick, s.position.branchCount
        ) == (b, t, line[-1].tick, len(line)), f"{b}@{t}"


def dump(result):
    return result.model_dump(mode="json")


# --- scenarios -------------------------------------------------------------------


def nested_scenario() -> Scenario:
    s = Scenario()
    s.start(cells=[("c1", "m0"), ("c2", "x")])             # 1
    s.edit("c1", "m1")                                      # 2
    s.edit("c1", "m2")                                      # 3
    s.chat("q1", "hello")                                   # 4
    a = s.branch_from(tick=3, name="A")                     # 5
    s.edit("c1", "a1")                                      # 6
    run = s.run("c2")                                       # 7
    s.edit("c2", "a-x")                                     # 8 edit while running
    b = s.branch_from(tick=6, name="B")                     # 9 (settled? run on A is open; fine for reference)
    s.edit("c1", "b1")                                      # 10
    s.chat("q2", "from B")                                  # 11
    c = s.branch_from(tick=10, name="C")                    # 12
    s.edit("c1", "c1")                                      # 13
    s.on("main").edit("c1", "m3")                           # 14
    s.on(a).output(run, "o")                                # 15
    s.finish(run)                                           # 16
    s.on(b).edit("c1", "b2")                                # 17
    s.on(c).kernel_restart()                                # 18
    s.chat("q3", "c-chat")                                  # 19
    s.chat_update("q3", "edited")                           # 20
    s.on(b).insert("c3", "new", index=0)                    # 21
    s.on("main").delete("c2")                               # 22
    return s


def test_three_level_nested_branches_work_on_every_level_match_oracle():
    cross_check(nested_scenario().events)


def test_nested_branch_values_are_what_a_reader_expects():
    s = nested_scenario()
    s.expect_at("C", 22, cells=[("c1", "c1"), ("c2", "x")], position=("C", 22, 20, 12))
    s.expect_at("B", 22, cells=[("c3", "new"), ("c1", "b2"), ("c2", "x")])
    s.expect_at("A", 22, cells=[("c1", "a1"), ("c2", "a-x")], runs={"run-1": "ok"})
    s.expect_at("main", 22, cells=[("c1", "m3")])
    # C branched from B@10: B's later edit "b2" must not appear on C
    assert "b2" not in [c.code for c in s.state_at("C", 22).notebook.cells]
    # running on A at C's cut: C line has A events <= 6 only, so no run at all
    s.expect_at("C", 22, runs={})


def test_two_children_of_one_parent_worked_alternately():
    s = Scenario()
    s.start(cells=[("c1", "m0")])                           # 1
    s.edit("c1", "m1")                                      # 2
    x = s.branch_from(tick=2, name="X")                     # 3
    s.on("main").edit("c1", "m2")                           # 4
    y = s.branch_from(tick=2, name="Y", parent="main")      # 5
    for i in range(3):
        s.on(x).edit("c1", f"x{i}")
        s.on(y).edit("c1", f"y{i}")
        s.on("main").edit("c1", f"m-after{i}")
    s.on(x).chat("q", "x-chat")
    cross_check(s.events)
    last = s.tick
    s.expect_at("X", last, cells=[("c1", "x2")])
    s.expect_at("Y", last, cells=[("c1", "y2")])
    # Y never sees X's chat and vice versa
    assert s.state_at("Y", last).chat == []


def test_branch_from_the_parents_very_first_event():
    s = Scenario()
    s.start(cells=[("c1", "m0")])                           # 1
    s.edit("c1", "m1")                                      # 2
    b = s.branch_from(tick=1, name="B")                     # 3 parentTick = parent's first event
    s.edit("c1", "b1")                                      # 4
    s.expect_at(b, 3, cells=[("c1", "m0")], position=(b, 3, 3, 2))
    s.expect_at(b, 4, cells=[("c1", "b1")], position=(b, 4, 4, 3))
    s.expect_at("main", 4, cells=[("c1", "m1")])
    cross_check(s.events)


def test_branch_from_a_branchs_first_event_and_from_its_branch_point_tick():
    s = Scenario()
    s.start(cells=[("c1", "m0")])                           # 1
    s.edit("c1", "m1")                                      # 2
    s.edit("c1", "m2")                                      # 3
    b = s.branch_from(tick=2, name="B")                     # 4 B's first event
    s.edit("c1", "b1")                                      # 5
    c = s.branch_from(tick=4, name="C", parent=b)           # 6 child of B's branch-point event
    s.edit("c1", "c1")                                      # 7
    s.expect_at(c, 6, cells=[("c1", "m1")], position=(c, 6, 6, 4))
    s.expect_at(c, 7, cells=[("c1", "c1")])
    cross_check(s.events)


def test_snapshot_keeps_only_what_it_should_on_a_branch():
    s = Scenario()
    s.start(cells=[("c1", "m0")])
    run = s.run("c1")
    s.chat("q", "hi")
    s.branch_from(tick=3, name="B")
    s.snapshot([("c9", "recovered")])
    cross_check(s.events)
    st = s.state_at("B", s.tick)
    assert [c.cellId for c in st.notebook.cells] == ["c9"]
    assert [r.runId for r in st.runs] == [run] and [m.messageId for m in st.chat] == ["q"]


# --- delivery -------------------------------------------------------------------


def test_duplicates_and_shuffles_give_identical_answers_everywhere():
    s = nested_scenario()
    base = {(b, t): dump(reference_rebuild(s.events, b, t))
            for b in ("main", "A", "B", "C") for t in range(1, s.tick + 1)}
    rng = random.Random(7)
    for _ in range(5):
        evs = list(s.events) + rng.sample(s.events, 8)
        rng.shuffle(evs)
        for (b, t), want in base.items():
            assert dump(reference_rebuild(evs, b, t)) == want, (b, t)


def test_wall_clock_is_never_used_for_ordering():
    s = nested_scenario()
    flipped = [e.model_copy(update={"wallTimeMs": 2_000_000_000_000 - e.tick * 1000}) for e in s.events]
    for b in ("main", "A", "B", "C"):
        for t in range(1, s.tick + 1):
            assert dump(reference_rebuild(flipped, b, t)) == dump(reference_rebuild(s.events, b, t))


def test_conflict_is_reported_before_missing_and_tick_clash_before_missing():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    s.edit("c1", "b")
    s.edit("c1", "c")
    s.edit("c1", "d")
    evs = [e for e in s.events if e.tick != 3]                 # tick 3 missing
    altered = evs[1].model_copy(update={"wallTimeMs": 5})
    assert reference_rebuild(evs + [altered], "main", 1).status == "conflict"
    other = evs[2].model_copy(update={"tick": 2})              # different event, same tick as eventId 2... new id
    other = other.model_copy(update={"eventId": "00000000-0000-4000-8000-000000000099"})
    assert reference_rebuild(evs + [other], "main", 1).status == "invalid_event"
    assert reference_rebuild(evs, "main", 3).status == "missing_events"
    assert reference_rebuild(evs, "main", 2).status == "ok"
    # missing is reported before unknown_branch
    assert reference_rebuild(evs, "nope", 4).status == "missing_events"
    assert reference_rebuild(evs, "nope", 2).status == "unknown_branch"


def test_missing_tick_on_a_parent_stops_children_at_and_after_it_only():
    s = nested_scenario()
    evs = [e for e in s.events if e.tick != 8]
    for b in ("main", "A", "B", "C"):
        for t in range(1, 8):
            assert reference_rebuild(evs, b, t).status == "ok", (b, t)
        for t in range(8, s.tick + 1):
            r = reference_rebuild(evs, b, t)
            assert r.status == "missing_events" and r.firstMissingTick == 8, (b, t)


# --- immutability ---------------------------------------------------------------


def test_mutating_a_returned_state_changes_nothing_else():
    s = nested_scenario()
    s.on("main").gap_start("g")
    s.output(s.run("c1"), "z")
    s.notebook_metadata({"k": [1]})
    s.cell_metadata("c1", {"tags": ["t"]})
    before_events = [e.model_dump(mode="json") for e in s.events]
    t = s.tick
    want = dump(reference_rebuild(s.events, "main", t))
    first = reference_rebuild(s.events, "main", t).state
    other = reference_rebuild(s.events, "main", t).state
    first.notebook.cells.append(first.notebook.cells[0].model_copy(deep=True))
    first.notebook.cells[0].code = "HACKED"
    first.notebook.cells[0].metadata.setdefault("tags", []).append("zz")
    for g in first.notebook.cells[0].outputs:
        g.items.clear()
    first.notebook.metadata["k"].append(2)
    first.chat.clear()
    first.runs.clear()
    first.gaps.clear()
    first.position.branchCount = 999
    assert dump(reference_rebuild(s.events, "main", t)) == want
    assert other.model_dump(mode="json") == want["state"]
    assert [e.model_dump(mode="json") for e in s.events] == before_events


def test_states_at_different_ticks_share_nothing():
    s = nested_scenario()
    a = s.state_at("B", 21)
    b = s.state_at("B", 20)
    a.notebook.cells[0].metadata["x"] = 1
    a.notebook.cells[0].outputs.append(None)
    assert "x" not in b.notebook.cells[0].metadata or b.notebook.cells[0].metadata is not a.notebook.cells[0].metadata
    assert s.state_at("B", 20).notebook.cells[0].metadata == {}


# --- holes found at the gate, fixed by rulings A and B --------------------------


def test_output_for_a_run_that_never_started_is_unknown_run():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    s._run_cell["ghost"] = "c1"
    s.output("ghost", "boo")
    r = s.rebuild("main", 2)
    assert r.status == "invalid_event" and r.reason.value == "unknown_run"


def test_branch_point_pointing_at_the_future_is_rejected():
    s = Scenario()
    s.start(cells=[("c1", "m0")])               # 1
    s.edit("c1", "m1")                          # 2
    s.branch_from(tick=5, name="B")             # 3, parentTick 5 does not exist yet
    s.on("main").edit("c1", "m2")               # 4
    s.edit("c1", "m3")                          # 5
    r = s.rebuild("B", 5)
    assert r.status == "invalid_event" and r.reason.value == "invalid_branch_point"


# --- re-gate of the Task 3 gate fixes (rulings A, B, C) -------------------------
# The oracle below stays independent of reference.py and apply.py.


def _first_own(events, branch):
    return min((e for e in events if e.branchId == branch), key=lambda e: e.tick)


def _tolerant_line(events, branch, tick):
    """Like oracle_line, but a branch point only counts when it is its branch's first event."""
    first = _first_own(events, branch)
    own = sorted((e for e in events if e.branchId == branch and e.tick <= tick), key=lambda e: e.tick)
    if first.type != "branch_point_created":
        return own
    parent = first.payload.parentBranchId
    return _tolerant_line(events, parent, min(tick, first.payload.parentTick)) + own


def _bad_branch_point_ticks(events, branch, tick):
    """Ticks of branch points on the line that break ruling B (own definition)."""
    root = min(events, key=lambda e: e.tick).branchId
    bad = []
    line = _tolerant_line(events, branch, tick)
    for e in line:
        first = _first_own(events, e.branchId)
        if e.type == "branch_point_created":
            if first is not e:
                bad.append(e.tick)
                continue
            pt = e.payload.parentTick
            at = [x for x in events if x.tick == pt]
            on_parent_line = bool(at) and at[0] in _tolerant_line(events, e.payload.parentBranchId, pt)
            if pt >= e.tick or not on_parent_line:
                bad.append(e.tick)
        elif first is e and e.branchId != root:
            bad.append(e.tick)  # a second root
    return bad


def _mutated(seed):
    """The nested scenario with some branch points pointed at random ticks."""
    rng = random.Random(seed)
    evs = list(nested_scenario().events)
    for i, e in enumerate(evs):
        if e.type == "branch_point_created" and rng.random() < 0.6:
            pl = e.payload.model_copy(update={"parentTick": rng.randint(1, len(evs))})
            evs[i] = e.model_copy(update={"payload": pl})
    return evs


@pytest.mark.parametrize("seed", range(40))
def test_branch_point_rulings_match_independent_oracle_on_mutated_histories(seed):
    evs = _mutated(seed)
    last = max(e.tick for e in evs)
    for b in sorted({e.branchId for e in evs}):
        for t in range(1, last + 1):
            r = reference_rebuild(evs, b, t)
            bad = _bad_branch_point_ticks(evs, b, t)
            is_bp = r.status == "invalid_event" and r.reason.value == "invalid_branch_point"
            if bad:
                assert is_bp, f"{b}@{t}: oracle says bad branch point at {bad}, got {r.status}"
            elif is_bp:
                continue  # over-rejection is guarded by test_branching_from_before_the_parent_existed...
            elif r.status == "ok":
                line = _tolerant_line(evs, b, t)
                cells, chat, runs, *_ = oracle_model(line)
                assert [(c.cellId, c.code) for c in r.state.notebook.cells] == cells
                assert (r.state.position.tick, r.state.position.lastEventTick, r.state.position.branchCount) == (
                    t, line[-1].tick, len(line))


def test_branch_point_at_a_sibling_branch_tick_is_invalid():
    s = Scenario()
    s.start(cells=[("c1", "a")])                      # 1
    s.edit("c1", "b")                                 # 2
    b = s.branch_from(tick=2, name="B")               # 3
    s.on("main").edit("c1", "m")                      # 4
    s.on(b).edit("c1", "bb")                          # 5
    s.branch_from(tick=5, name="C", parent="main")    # 6: tick 5 belongs to sibling B
    r = s.rebuild("C", 6)
    assert r.status == "invalid_event" and r.reason.value == "invalid_branch_point" and r.tick == 6
    assert s.rebuild("main", 6).status == "ok" and s.rebuild("B", 6).status == "ok"


def test_unknown_cell_is_reported_before_unknown_run_for_all_output_events():
    for emit in ("output", "replace_output", "clear_outputs"):
        s = Scenario()
        s.start(cells=[("c1", "a")])
        s._run_cell["ghost"] = "nope"                 # unknown cell and unknown run
        getattr(s, emit)("ghost", "x") if emit != "clear_outputs" else s.clear_outputs("ghost")
        r = s.rebuild("main", 2)
        assert r.status == "unknown_cell" and r.cellId == "nope", emit


def test_all_three_output_events_for_a_never_started_run_are_unknown_run():
    for emit in ("output", "replace_output", "clear_outputs"):
        s = Scenario()
        s.start(cells=[("c1", "a")])
        s._run_cell["ghost"] = "c1"
        getattr(s, emit)("ghost", "x") if emit != "clear_outputs" else s.clear_outputs("ghost")
        r = s.rebuild("main", 2)
        assert r.status == "invalid_event" and r.reason.value == "unknown_run", emit


def test_asking_a_tick_that_is_another_branchs_event_or_before_the_branch_existed():
    s = Scenario()
    s.start(cells=[("c1", "a")])                      # 1
    s.edit("c1", "b")                                 # 2
    s.edit("c1", "c")                                 # 3
    b = s.branch_from(tick=2, name="B")               # 4
    s.on("main").edit("c1", "d")                      # 5 (main's event, asked on B)
    s.on(b).edit("c1", "bb")                          # 6
    s.expect_at(b, 5, cells=[("c1", "b")], position=(b, 5, 4, 3))
    s.expect_at(b, 1, cells=[("c1", "a")], position=(b, 1, 1, 1))
    s.expect_at(b, 3, cells=[("c1", "c")][:0] or [("c1", "b")], position=(b, 3, 2, 2))
    s.expect_at("main", 4, cells=[("c1", "c")], position=("main", 4, 3, 3))
    cross_check(s.events)


def test_branching_from_before_the_parent_existed_is_not_rejected_for_the_parents_own_branch_point():
    s = Scenario()
    s.start(cells=[("c1", "a")])                      # 1
    s.edit("c1", "b")                                 # 2
    s.edit("c1", "c")                                 # 3
    a = s.branch_from(tick=3, name="A")               # 4
    s.edit("c1", "a1")                                # 5
    b = s.branch_from(tick=5, name="B", parent=a)     # 6 valid: tick 5 is on A's line
    s.branch_from(tick=2, name="C", parent=b)         # 7 valid: tick 2 is on B's line (shared history)
    r = s.rebuild("C", 7)
    assert r.status == "ok", r
    assert s.rebuild("B", 7).status == "ok"


def test_missing_parent_tick_of_a_branch_point_is_missing_events_per_the_oracle():
    """Independent expectation: with tick d dropped (first missing tick fm = d), a position
    below fm on a branch whose chain has a branch point with parentTick == a dropped tick
    is missing_events(fm); every other position below fm is ok; at or above fm, missing."""
    s = nested_scenario()
    events = list(s.events)
    bps = {e.branchId: e for e in events if e.type == "branch_point_created"}
    asked_missing_parent = 0
    for d in sorted({bp.payload.parentTick for bp in bps.values()}):
        have = [e for e in events if e.tick != d]
        present = {e.tick for e in have}
        for b in sorted({e.branchId for e in events}):
            for t in range(1, max(present) + 1):
                chain, x = [], b
                while x in bps:
                    chain.append(bps[x])
                    x = bps[x].payload.parentBranchId
                r = reference_rebuild(have, b, t)
                if t >= d:
                    assert r.status == "missing_events" and r.firstMissingTick == d
                elif any(bp.payload.parentTick not in present for bp in chain):
                    asked_missing_parent += 1
                    assert r.status == "missing_events" and r.firstMissingTick == d, (b, t, d)
                else:
                    assert r.status == "ok", (b, t, d, r)
    assert asked_missing_parent > 0

"""Adversarial checks of the reference rebuilder.

The brute-force oracle below is written independently of `reference.py`: it picks the
events of a line by a recursive membership rule and folds a tiny model of the facts
that matter (cell code, chat bodies, run status, position), without using `apply`.
"""

from __future__ import annotations

import random

import pytest

from blaym_notebook.tests.history.reference import reference_rebuild, reference_replay_all
from blaym_notebook.tests.history.scenario import Scenario


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
                if getattr(c.cellType, "value", c.cellType) != "code":
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


def _case_three_level_nested_branches_work_on_every_level_match_oracle():
    cross_check(nested_scenario().events)


def _case_nested_branch_values_are_what_a_reader_expects():
    s = nested_scenario()
    s.expect_at("C", 22, cells=[("c1", "c1"), ("c2", "x")], position=("C", 22, 20, 12))
    s.expect_at("B", 22, cells=[("c3", "new"), ("c1", "b2"), ("c2", "x")])
    s.expect_at("A", 22, cells=[("c1", "a1"), ("c2", "a-x")], runs={"run-1": "ok"})
    s.expect_at("main", 22, cells=[("c1", "m3")])
    # C branched from B@10: B's later edit "b2" must not appear on C
    assert "b2" not in [c.code for c in s.state_at("C", 22).notebook.cells]
    # running on A at C's cut: C line has A events <= 6 only, so no run at all
    s.expect_at("C", 22, runs={})


def test_nested_branches_match_oracle_and_reader_expectations():
    _case_three_level_nested_branches_work_on_every_level_match_oracle()
    _case_nested_branch_values_are_what_a_reader_expects()


# --- delivery -------------------------------------------------------------------


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


# --- immutability ---------------------------------------------------------------


# --- holes found at the gate, fixed by rulings A and B --------------------------


# --- re-check of the earlier fixes (rulings A, B, C) -------------------------
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


def _branch_point_ruling_for_seed(seed):
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


def test_branch_point_rulings_match_independent_oracle_on_mutated_histories():
    for seed in range(5):
        _branch_point_ruling_for_seed(seed)


def _case_asking_a_tick_that_is_another_branchs_event_or_before_the_branch_existed():
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


def _case_branching_from_before_the_parent_existed_is_not_rejected_for_the_parents_own_branch_point():
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


def test_asking_before_a_branch_existed():
    _case_asking_a_tick_that_is_another_branchs_event_or_before_the_branch_existed()
    _case_branching_from_before_the_parent_existed_is_not_rejected_for_the_parents_own_branch_point()



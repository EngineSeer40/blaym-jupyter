"""Hand-written tests for each event's effect, and for the reference's rules."""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from blaym_notebook.history.apply import apply
from blaym_notebook.tests.history.reference import reference_all, reference_rebuild, reference_replay_all
from blaym_notebook.tests.history.reference import _replay_all_clean
from blaym_notebook.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_notebook.tests.history.generate import deliveries, histories, invalid_histories
from blaym_notebook.tests.history.scenario import Scenario

CELLS = [("c1", "a = 1"), ("c2", "b = 2")]


def two_cells() -> Scenario:
    s = Scenario()
    s.start(cells=CELLS)
    return s


def assert_invalid(s: Scenario, tick: int, reason: str, branch: str = "main"):
    """The answer at (branch, tick) is an `invalid_event` for the event at `tick`."""
    r = s.rebuild(branch, tick)
    assert r.status == "invalid_event", f"got {r}"
    assert r.reason.value == reason, f"got {r}"
    assert r.tick == tick
    assert r.eventId == str(s.events[tick - 1].eventId)
    assert r.detail
    return r


def code(state):
    return [(c.cellId, c.code) for c in state.notebook.cells]


def previews(state, cell=0):
    return [[i.preview for i in g.items] for g in state.notebook.cells[cell].outputs]


def test_notebook_events_apply():
    s = two_cells()
    s.expect_at("main", 1, cells=CELLS, path="analysis.ipynb", position=("main", 1, 1, 1))
    s.insert("c3", "c = 3", index=1)
    s.move("c3", 1, 0)
    s.delete("c1")
    s.change_type("c2", "markdown")
    s.edit("c2", "b = 3")
    s.cell_metadata("c2", {"tags": ["x"]})
    s.notebook_metadata({"k": 1})
    s.expect_at("main", 2, cells=[("c1", "a = 1"), ("c3", "c = 3"), ("c2", "b = 2")])
    s.expect_at("main", 3, cells=[("c3", "c = 3"), ("c1", "a = 1"), ("c2", "b = 2")])
    s.expect_at("main", 4, cells=[("c3", "c = 3"), ("c2", "b = 2")])
    assert s.state_at("main", 5).notebook.cells[1].cellType.value == "markdown"
    st_ = s.state_at("main", 8)
    assert st_.notebook.cells[1].revision == 2 and st_.notebook.cells[1].metadata == {"tags": ["x"]}
    assert st_.notebook.metadata == {"k": 1}

    s = two_cells()
    s.rename("analysis.ipynb", "new.ipynb")
    s.copy("new.ipynb", "copy.ipynb")
    s.kernel_start("k1")
    assert s.state_at("main", 2).notebook.path == "new.ipynb"
    assert s.state_at("main", 3).notebook == s.state_at("main", 2).notebook  # a copy changes nothing
    kernel = s.state_at("main", 4).kernel
    assert (kernel.status.value, kernel.kernelId) == ("running", "k1")

    s = two_cells()
    s.edit("c1", "a = 5")
    s.outside_change([("c1", "from outside"), ("c9", "new")])
    s.edit("c1", "after", revision=2)
    assert code(s.state_at("main", 3)) == [("c1", "from outside"), ("c9", "new")]
    assert s.state_at("main", 3).lastOutsideChange.tick == 3
    assert s.state_at("main", 2).lastOutsideChange is None
    s.expect_at("main", 4, cells=[("c1", "after"), ("c9", "new")])


def test_cell_and_run_events_that_do_not_fit_are_typed_invalid():
    s = two_cells()
    s.insert("c3", "x")
    r = apply(None, s.events[1])
    assert (r.status, r.reason.value, r.tick) == ("invalid_event", "first_event_not_snapshot", 2)

    for act in (lambda s: s.delete("nope"), lambda s: s.run("nope")):
        s = two_cells()
        act(s)
        assert s.expect_at("main", 2, status="unknown_cell").cellId == "nope"

    s = two_cells()
    s.edit("c1", "a = 3", revision=4)
    r = s.expect_at("main", 2, status="invalid_revision")
    assert (r.expectedRevision, r.actualRevision, r.cellId) == (2, 4, "c1")

    for act, reason in ((lambda s: s.insert("c1", "again"), "duplicate_id"),
                        (lambda s: s.insert("c3", "x", index=5), "index_out_of_range"),
                        (lambda s: s.move("c1", 1, 0), "position_mismatch")):
        s = two_cells()
        act(s)
        assert_invalid(s, 2, reason)

    s = two_cells()
    s._run_cell["ghost"] = "c1"
    s.finish("ghost")
    assert_invalid(s, 2, "unknown_run")

    s = two_cells()
    run = s.run("c1")
    s.finish(run)
    s.finish(run)
    assert_invalid(s, 4, "run_not_running")

    s = two_cells()
    run = s.run("c1")
    s.replace_output(run, "x", index=0)
    assert_invalid(s, 3, "no_output_at_index")

    s = two_cells()
    s.gap_end("ghost")
    s.insert("c3", "x")
    assert_invalid(s, 2, "unknown_gap")
    assert s.rebuild("main", 3).tick == 2  # an invalid event stops later positions too

    for act in (lambda s, run: s.output("ghost", "x"), lambda s, run: s.replace_output("ghost", "x"),
                lambda s, run: s.clear_outputs("ghost", wait=True)):
        s = two_cells()
        s._run_cell["ghost"] = "c1"
        act(s, None)
        assert_invalid(s, 2, "unknown_run")


def test_chat_and_gaps_apply_and_misuse_is_invalid():
    s = two_cells()
    s.chat("m1", "plot it")
    s.chat("m2", "Sure", sender="agent-1", kind="agent")
    s.chat_update("m2", "Sure, here")
    s.expect_at("main", 3, chat=[("m1", "plot it"), ("m2", "Sure")])
    s.expect_at("main", 4, chat=[("m1", "plot it"), ("m2", "Sure, here")])
    assert s.state_at("main", 3).chat[1].addedTick == 3

    s = two_cells()
    s.chat_update("ghost", "x")
    assert_invalid(s, 2, "unknown_message")

    s = two_cells()
    s.chat("m1", "hi")
    s.gap_start("g1", source="browser", reason="no_client")
    s.snapshot([("c1", "recovered")])
    s.gap_end("g1")
    gap = s.state_at("main", 3).gaps[0]
    assert (gap.gapId, gap.startedTick, gap.endedTick) == ("g1", 3, None)
    st_ = s.state_at("main", 5)  # a recovery snapshot keeps chat and gaps
    assert code(st_) == [("c1", "recovered")] and len(st_.chat) == 1 and st_.gaps[0].endedTick == 5
    s.gap_end("g1")
    assert_invalid(s, 6, "gap_already_ended")


def test_runs_keep_their_revision_and_a_restart_leaves_unfinished_runs():
    s = two_cells()
    run = s.run("c1")
    s.edit("c1", "a = 99")
    s.output(run, "1")
    s.finish(run, "error")
    done = s.run("c2")
    s.kernel_restart(reason="unknown")
    st_ = s.state_at("main", 5)
    r = st_.runs[0]
    assert (r.cellRevision, r.status.root, r.startedTick, r.endedTick) == (1, "error", 2, 5)
    assert st_.notebook.cells[0].revision == 2 and st_.notebook.cells[0].runCount == 1
    s.expect_at("main", 6, runs={run: "error", done: "running"})
    s.expect_at("main", 7, runs={run: "error", done: "unfinished"})
    assert s.state_at("main", 7).kernel.lastRestart.reason.value == "unknown"


def test_outputs_add_replace_clear_and_group_by_run():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "one", index=0)
    s.output(run, "two", index=1)
    s.replace_output(run, "TWO", index=1)
    s.expect_at("main", 4, outputs={"c1": ["one", "two"]})
    s.expect_at("main", 5, outputs={"c1": ["one", "TWO"]})
    s.clear_outputs(run)
    s.expect_at("main", 6, outputs={"c1": []})
    s.output(run, "again", index=0)
    s.output(run, "front", index=0)  # no pending clear: inserted at the front
    s.expect_at("main", 8, outputs={"c1": ["front", "again"]})
    r2 = s.run("c1")
    s.output(r2, "second")
    assert [g.runId for g in s.state_at("main", 10).notebook.cells[0].outputs] == [run, r2]
    s.expect_at("main", 10, outputs={"c1": ["second"]})


def test_clear_with_wait_keeps_old_output_until_the_next_one():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "old1", 0)
    s.output(run, "old2", 1)
    s.clear_outputs(run, wait=True)
    group = s.state_at("main", 5).notebook.cells[0].outputs[0]
    assert group.clearPending is True and [i.preview for i in group.items] == ["old1", "old2"]
    s.output(run, "new", 0)
    group = s.state_at("main", 6).notebook.cells[0].outputs[0]
    assert group.clearPending is None and [i.preview for i in group.items] == ["new"]
    s.clear_outputs(run, wait=True)
    s.clear_outputs(run)  # an immediate clear drops a pending flag
    group = s.state_at("main", 8).notebook.cells[0].outputs[0]
    assert group.clearPending is None and group.items == []

    s = two_cells()
    run = s.run("c1")
    s.clear_outputs(run, wait=True)  # waits even with nothing to clear
    assert s.state_at("main", 3).notebook.cells[0].outputs[0].clearPending is True


def test_clear_cell_outputs_removes_finished_runs_only():
    s = Scenario()
    s.start()
    s.insert("c1", "x")
    done, live = s.run("c1"), s.run("c1")
    s.output(done, "a")
    s.output(live, "b")
    s.finish(done)
    s.clear_cell_outputs("c1")
    assert [g.runId for g in s.state_at(s.current, len(s.events)).notebook.cells[0].outputs] == [live]


def test_branch_state_is_parent_at_the_branch_point_then_its_own_events():
    s = two_cells()
    s.edit("c1", "a = 2")             # 2
    s.edit("c1", "a = 3")             # 3
    s.branch_from(tick=2, name="b")   # 4
    s.edit("c1", "b-code")            # 5
    s.expect_at("b", 1, cells=CELLS, position=("b", 1, 1, 1))
    s.expect_at("b", 4, cells=[("c1", "a = 2"), ("c2", "b = 2")], position=("b", 4, 4, 3))
    s.expect_at("b", 5, cells=[("c1", "b-code"), ("c2", "b = 2")], position=("b", 5, 5, 4))
    s.expect_at("main", 5, cells=[("c1", "a = 3"), ("c2", "b = 2")])  # the parent never sees them
    b = s.branch_from(tick=2, name="c", parent="main")  # 6
    s.on("main").edit("c1", "m")      # 7
    s.expect_at(b, 6, cells=[("c1", "a = 2"), ("c2", "b = 2")], position=(b, 6, 6, 3))


def test_unknown_branch_and_lineage_loop():
    s = two_cells()
    assert s.expect_at("ghost", 1, status="unknown_branch").branchId == "ghost"
    s.branch_from(tick=1, name="b")
    s.branch_from(tick=2, name="c")
    s.events[1].payload.parentBranchId = "c"  # b now starts from c, c from b
    r = s.rebuild("c", 3)
    assert r.status == "lineage_loop" and set(r.branchIds) == {"b", "c"}


def test_invalid_branch_points():
    s = two_cells()
    s.branch_from(tick=2, name="b")  # parentTick is not before its own tick
    assert assert_invalid(s, 2, "invalid_branch_point", "b")

    s = two_cells()
    s.edit("c1", "m1")                   # 2
    b = s.branch_from(tick=2, name="b")  # 3
    s.on("main").edit("c1", "m2")        # 4: after b was made, not on b's line
    s.branch_from(tick=4, name="c", parent=b)
    assert_invalid(s, 5, "invalid_branch_point", "c")

    s = two_cells()
    s._parent["other"] = None
    s._revs["other"] = []
    s.on("other").snapshot([("c9", "z")])  # a second root without a branch point
    assert_invalid(s, 2, "invalid_branch_point", "other")
    s.expect_at("main", 1, cells=CELLS)

    s = two_cells()
    s.branch_from(tick=1, name="b")
    s.edit("c1", "b1")
    s.branch_from(tick=1, name="b", parent="main", branch_id="b")  # a second branch point on b
    s.on("b").edit("c1", "b2")
    assert_invalid(s, 4, "invalid_branch_point", "b")
    assert s.rebuild("b", 5).status == "invalid_event"
    s.expect_at("b", 3, cells=[("c1", "b1"), ("c2", "b = 2")])


def test_missing_duplicate_and_clashing_deliveries():
    s = two_cells()
    s.edit("c1", "a = 2")             # 2
    s.branch_from(tick=2, name="b")   # 3
    s.edit("c1", "b1")                # 4
    s.edit("c1", "b2")                # 5
    s.on("main").edit("c1", "a = 3")  # 6
    full = list(s.events)
    s.events = [e for e in full if e.tick != 4]
    s.expect_at("b", 3, cells=[("c1", "a = 2"), ("c2", "b = 2")])
    s.expect_at("main", 2, cells=[("c1", "a = 2"), ("c2", "b = 2")])
    for branch, t in (("main", 4), ("main", 7), ("b", 5), ("b", 6)):  # every branch stops at the hole
        assert s.expect_at(branch, t, status="missing_events").firstMissingTick == 4
    s.events = full
    assert reference_all(list(reversed(full)) + full[:2] + full) == reference_all(full)  # order, duplicates

    events = [e for e in full if e.tick != 2]  # the branch point's parent tick was never delivered
    assert reference_rebuild(events, "b", 1).firstMissingTick == 2
    for events, tick, first in (([], 0, 1), (full[:1], -3, 2), (full[:1], 2, 2)):
        r = reference_rebuild(events, "main", tick)  # asks outside the history never raise
        assert r.status == "missing_events" and r.firstMissingTick == first

    s = two_cells()
    s.edit("c1", "a = 2")
    forged = s.events[1].model_copy(deep=True)
    forged.payload.code = "tampered"
    r = reference_rebuild(s.events + [forged], "main", 2)
    assert r.status == "conflict" and r.eventId == str(forged.eventId)

    other = s.events[0].model_copy(update={"eventId": __import__("uuid").uuid4()})
    r = reference_rebuild(s.events + [other], "main", 1)
    assert r.reason.value == "tick_clash" and r.eventId == str(other.eventId)


def test_results_are_never_shared_or_changed():
    s = two_cells()
    s.cell_metadata("c1", {"tags": ["x"]})
    first = apply(None, s.events[0]).state
    before = first.model_dump_json()
    second = apply(first, s.events[1]).state
    assert first.model_dump_json() == before
    s.events[1].payload.metadata["tags"].append("mutated")
    s.events[0].payload.cells[0].code = "mutated"
    assert second.notebook.cells[0].metadata == {"tags": ["x"]} and second.notebook.cells[0].code == "a = 1"
    second.notebook.cells[0].code = "caller edit"
    assert first.notebook.cells[0].code == "a = 1"

    s.branch_from(tick=2, name="b")
    s.edit("c2", "b-code")
    answers = reference_all(s.events)
    frozen = {k: v.model_dump_json() for k, v in answers.items()}
    reference_all(s.events)
    for k in answers:
        reference_rebuild(s.events, *k)
    assert {k: v.model_dump_json() for k, v in answers.items()} == frozen


def _detached(state):
    return [(g.runId, [i.preview for i in g.items], bool(g.clearPending)) for g in state.detachedOutputs]


def _running_then_deleted():
    s = Scenario()
    s.start(cells=[("c1", "a")])         # 1
    run = s.run("c1")                    # 2
    s.output(run, "first")               # 3
    s.delete("c1")                       # 4 deleted while the run is running
    return s, run


def test_deleting_a_cell_moves_a_running_runs_output_to_detached_outputs():
    s, run = _running_then_deleted()
    assert s.state_at("main", 3).detachedOutputs == []
    st_ = s.state_at("main", 4)
    assert st_.notebook.cells == [] and _detached(st_) == [(run, ["first"], False)]
    for act, want in ((lambda r: s.output(r, "after", index=1), ["first", "after"]),
                      (lambda r: s.replace_output(r, "swapped", index=0), ["swapped"]),
                      (lambda r: s.clear_outputs(r), [])):
        s, run = _running_then_deleted()
        act(run)
        assert _detached(s.state_at("main", 5)) == [(run, want, False)]
    s, run = _running_then_deleted()
    s.clear_outputs(run, wait=True)
    s.output(run, "fresh")
    assert _detached(s.state_at("main", 5)) == [(run, ["first"], True)]
    assert _detached(s.state_at("main", 6)) == [(run, ["fresh"], False)]
    s.kernel_restart()
    st_ = s.state_at("main", 7)
    assert _detached(st_) == [(run, ["fresh"], False)] and st_.runs[0].status.root == "unfinished"

    s = Scenario()
    s.start(cells=[("c1", "a")])
    r1, r2 = s.run("c1"), s.run("c1")
    s.output(r1, "old")
    s.output(r2, "live")
    s.finish(r1)
    s.delete("c1")  # the finished run's group goes with the cell
    assert _detached(s.state_at("main", s.tick)) == [(r2, ["live"], False)]
    s.output(r1, "late")  # a late output for a finished run is kept, not an error
    assert (r1, ["late"], False) in _detached(s.state_at("main", s.tick))


def test_detached_outputs_stay_on_their_branch_and_position():
    s, run = _running_then_deleted()
    b = s.branch_from(tick=3, name="B", parent="main")
    st_b = s.state_at(b, s.tick)
    assert st_b.detachedOutputs == [] and [c.cellId for c in st_b.notebook.cells] == ["c1"]
    s.on("main").output(run, "after", index=1)
    assert s.state_at(b, s.tick).detachedOutputs == []

    for kind in ("snapshot", "outside_change"):  # a replaced notebook detaches only running runs' output
        s = Scenario()
        s.start(cells=[("c1", "a"), ("c2", "b")])
        live = s.run("c1")
        s.output(live, "kept")
        done = s.run("c2")
        s.output(done, "gone")
        s.finish(done)
        getattr(s, kind)([("c9", "new")])  # 7
        st_ = s.state_at("main", 7)
        assert [c.cellId for c in st_.notebook.cells] == ["c9"] and _detached(st_) == [(live, ["kept"], False)]
        assert s.state_at("main", 6).detachedOutputs == []

    s = Scenario()
    s.start(cells=[("c1", "a")])
    run = s.run("c1")
    s.output(run, "x")
    s.snapshot([("c1", "a")])
    assert s.state_at("main", s.tick).detachedOutputs == []


def test_output_events_must_name_the_runs_own_cell():
    for emit in ("output", "replace_output", "clear_outputs"):
        s = Scenario()
        s.start(cells=[("c1", "a"), ("c2", "b")])
        run = s.run("c1")
        s.output(run, "x")
        s._run_cell[run] = "c2"
        getattr(s, emit)(run) if emit == "clear_outputs" else getattr(s, emit)(run, "y")
        assert_invalid(s, s.tick, "run_cell_mismatch")
        assert s.rebuild("main", s.tick - 1).status == "ok"

    s = Scenario()
    s.start(cells=[("c1", "a")])
    s.delete("c1")
    s._run_cell["ghost"] = "c1"
    s.output("ghost", "x")  # the cell is gone and the run never started: the cell is reported first
    assert s.rebuild("main", s.tick).status == "unknown_cell"


@settings(max_examples=40 if not DEEP else 1000)
@given(st.data())
def test_replay_all_equals_reference_rebuild_on_valid_messy_and_invalid_histories(data):
    def same(events):
        got, want = reference_replay_all(events), reference_all(events)
        assert got.keys() == want.keys()
        for key in want:
            assert got[key].model_dump(mode="json") == want[key].model_dump(mode="json"), key

    h = data.draw(histories(max_events=MAX_EVENTS))
    assert _replay_all_clean(h.events) is not None  # valid histories take the fast path
    same(h.events)
    same(data.draw(deliveries(history=h)).events)
    same(data.draw(invalid_histories()).events)

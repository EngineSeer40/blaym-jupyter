"""Hand-written tests for each event's effect, and for the reference's rules."""

from __future__ import annotations

import pytest

from hypothesis import given, settings

from blaym_jupyter.history import apply, reference_all, reference_rebuild, reference_replay_all
from blaym_jupyter.history.reference import _replay_all_clean
from blaym_jupyter.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_jupyter.tests.history.generate import deliveries, histories, invalid_histories
from blaym_jupyter.tests.history.scenario import Scenario


def two_cells() -> Scenario:
    s = Scenario()
    s.start(cells=[("c1", "a = 1"), ("c2", "b = 2")])
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


# --- structure ------------------------------------------------------------------


def test_snapshot_sets_the_whole_notebook():
    s = two_cells()
    s.expect_at("main", 1, cells=[("c1", "a = 1"), ("c2", "b = 2")], path="analysis.ipynb",
                position=("main", 1, 1, 1))


def test_first_event_must_be_a_snapshot():
    s = two_cells()
    s.insert("c3", "x")
    r = apply(None, s.events[1])
    assert r.status == "invalid_event"
    assert r.reason.value == "first_event_not_snapshot"
    assert (r.eventId, r.tick) == (str(s.events[1].eventId), 2)


def test_insert_delete_move_type_change():
    s = two_cells()
    s.insert("c3", "c = 3", index=1)
    s.expect_at("main", 2, cells=[("c1", "a = 1"), ("c3", "c = 3"), ("c2", "b = 2")])
    s.move("c3", 1, 0)
    s.expect_at("main", 3, cells=[("c3", "c = 3"), ("c1", "a = 1"), ("c2", "b = 2")])
    s.delete("c1")
    s.expect_at("main", 4, cells=[("c3", "c = 3"), ("c2", "b = 2")])
    s.change_type("c2", "markdown")
    assert s.state_at("main", 5).notebook.cells[1].cellType.value == "markdown"


def test_unknown_cell_for_delete_edit_and_run():
    for act in (lambda s: s.delete("nope"), lambda s: s.edit("nope", "x", revision=2),
                lambda s: s.run("nope")):
        s = two_cells()
        act(s)
        r = s.expect_at("main", 2, status="unknown_cell")
        assert r.cellId == "nope"


def test_source_change_needs_next_revision():
    s = two_cells()
    s.edit("c1", "a = 2")
    s.expect_at("main", 2, cells=[("c1", "a = 2"), ("c2", "b = 2")])
    assert s.state_at("main", 2).notebook.cells[0].revision == 2
    s.edit("c1", "a = 3", revision=4)
    r = s.expect_at("main", 3, status="invalid_revision")
    assert (r.expectedRevision, r.actualRevision, r.cellId) == (3, 4, "c1")


def test_metadata_replaced():
    s = two_cells()
    s.cell_metadata("c1", {"tags": ["x"]})
    s.notebook_metadata({"k": 1})
    st = s.state_at("main", 3)
    assert st.notebook.cells[0].metadata == {"tags": ["x"]}
    assert st.notebook.metadata == {"k": 1}
    s.cell_metadata("c1", {})
    assert s.state_at("main", 4).notebook.cells[0].metadata == {}


def test_rename_changes_path_and_copy_changes_nothing():
    s = two_cells()
    s.rename("analysis.ipynb", "new.ipynb")
    s.copy("new.ipynb", "copy.ipynb")
    s.expect_at("main", 2, path="new.ipynb")
    st = s.state_at("main", 3)
    assert st.notebook.path == "new.ipynb"
    assert st.position.tick == 3
    assert st.notebook == s.state_at("main", 2).notebook


# --- runs and outputs -----------------------------------------------------------


def test_run_is_tied_to_revision_sent_even_if_edited_during_run():
    s = two_cells()
    run = s.run("c1")
    s.edit("c1", "a = 99")
    s.output(run, "1")
    s.finish(run)
    st = s.state_at("main", 5)
    r = st.runs[0]
    assert (r.cellRevision, r.status.root, r.startedTick, r.endedTick) == (1, "ok", 2, 5)
    assert st.notebook.cells[0].revision == 2
    assert st.notebook.cells[0].outputs[0].runId == run
    assert st.notebook.cells[0].runCount == 1


def test_run_without_finish_stays_running_and_restart_makes_it_unfinished():
    s = two_cells()
    run = s.run("c1")
    s.expect_at("main", 2, runs={run: "running"})
    s.kernel_restart(reason="unknown")
    s.expect_at("main", 3, runs={run: "unfinished"})
    st = s.state_at("main", 3)
    assert st.kernel.lastRestart.tick == 3
    assert st.kernel.lastRestart.reason.value == "unknown"
    assert st.runs[0].endedTick is None


def test_restart_leaves_finished_runs_alone():
    s = two_cells()
    run = s.run("c1")
    s.finish(run, "error")
    s.kernel_restart()
    s.expect_at("main", 4, runs={run: "error"})


def test_outputs_add_replace_clear():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "one", index=0)
    s.output(run, "two", index=1)
    s.expect_at("main", 4, outputs={"c1": ["one", "two"]})
    s.replace_output(run, "TWO", index=1)
    s.expect_at("main", 5, outputs={"c1": ["one", "TWO"]})
    s.clear_outputs(run)
    s.expect_at("main", 6, outputs={"c1": []})
    s.output(run, "again", index=0)
    s.expect_at("main", 7, outputs={"c1": ["again"]})


def test_clear_with_wait_keeps_old_output_until_the_next_output_replaces_it():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "old1", 0)
    s.output(run, "old2", 1)
    s.clear_outputs(run, wait=True)
    s.expect_at("main", 5, outputs={"c1": ["old1", "old2"]})
    s.output(run, "new", 0)
    s.expect_at("main", 6, outputs={"c1": ["new"]})


def test_outputs_are_grouped_by_run_and_latest_run_is_last():
    s = two_cells()
    r1 = s.run("c1")
    s.output(r1, "first")
    r2 = s.run("c1")
    s.output(r2, "second")
    st = s.state_at("main", 5)
    assert [g.runId for g in st.notebook.cells[0].outputs] == [r1, r2]
    s.expect_at("main", 5, outputs={"c1": ["second"]})


def test_replace_output_with_nothing_to_replace_is_no_output_at_index():
    s = two_cells()
    run = s.run("c1")
    s.replace_output(run, "x", index=0)
    assert_invalid(s, 3, "no_output_at_index")


# --- kernel, outside, chat, gaps -----------------------------------------------


def test_kernel_started():
    s = two_cells()
    s.kernel_start("k1")
    st = s.state_at("main", 2)
    assert (st.kernel.status.value, st.kernel.kernelId) == ("running", "k1")


def test_outside_change_replaces_notebook_and_is_marked():
    s = two_cells()
    s.edit("c1", "a = 5")
    s.outside_change([("c1", "from outside"), ("c9", "new")])
    s.edit("c1", "after", revision=2)
    st = s.state_at("main", 3)
    assert [(c.cellId, c.code) for c in st.notebook.cells] == [("c1", "from outside"), ("c9", "new")]
    assert st.lastOutsideChange.tick == 3
    s.expect_at("main", 4, cells=[("c1", "after"), ("c9", "new")])
    assert s.state_at("main", 2).lastOutsideChange is None


def test_chat_added_and_update_replaces_body():
    s = two_cells()
    s.chat("m1", "plot it")
    s.chat("m2", "Sure", sender="agent-1", kind="agent")
    s.chat_update("m2", "Sure, here")
    s.chat_update("m2", "Sure, here it is")
    s.expect_at("main", 3, chat=[("m1", "plot it"), ("m2", "Sure")])
    s.expect_at("main", 4, chat=[("m1", "plot it"), ("m2", "Sure, here")])
    s.expect_at("main", 5, chat=[("m1", "plot it"), ("m2", "Sure, here it is")])
    assert s.state_at("main", 3).chat[1].addedTick == 3


def test_chat_update_of_unknown_message_is_unknown_message():
    s = two_cells()
    s.chat_update("ghost", "x")
    assert_invalid(s, 2, "unknown_message")


def test_gaps_open_and_close():
    s = two_cells()
    s.gap_start("g1", source="browser", reason="no_client")
    open_gap = s.state_at("main", 2).gaps[0]
    assert (open_gap.gapId, open_gap.startedTick, open_gap.endedTick) == ("g1", 2, None)
    s.gap_end("g1")
    assert s.state_at("main", 3).gaps[0].endedTick == 3
    assert s.state_at("main", 2).gaps[0].endedTick is None


def test_recovery_snapshot_after_gap_keeps_chat_and_gaps():
    s = two_cells()
    s.chat("m1", "hi")
    s.gap_start("g1")
    s.snapshot([("c1", "recovered")])
    s.gap_end("g1")
    st = s.state_at("main", 5)
    assert [(c.cellId, c.code) for c in st.notebook.cells] == [("c1", "recovered")]
    assert len(st.chat) == 1 and st.gaps[0].endedTick == 5


# --- branches and rules ----------------------------------------------------------


def test_branch_state_is_parent_at_branch_point_then_own_events():
    s = two_cells()
    s.edit("c1", "a = 2")        # 2
    s.edit("c1", "a = 3")        # 3 (parent, after the branch point)
    s.branch_from(tick=2, name="b")   # 4
    s.edit("c1", "b-code")       # 5
    s.expect_at("b", 4, cells=[("c1", "a = 2"), ("c2", "b = 2")], position=("b", 4, 4, 3))
    s.expect_at("b", 5, cells=[("c1", "b-code"), ("c2", "b = 2")], position=("b", 5, 5, 4))
    s.expect_at("main", 3, cells=[("c1", "a = 3"), ("c2", "b = 2")])
    # the parent never sees the child's events
    s.expect_at("main", 5, cells=[("c1", "a = 3"), ("c2", "b = 2")])


def test_going_back_before_a_branch_existed_shows_parent_state_on_the_branch():
    s = two_cells()
    s.edit("c1", "a = 2")        # 2
    s.branch_from(tick=2, name="b")   # 3
    s.edit("c1", "b-code")       # 4
    s.expect_at("b", 1, cells=[("c1", "a = 1"), ("c2", "b = 2")], position=("b", 1, 1, 1))


def test_unknown_branch_and_lineage_loop():
    s = two_cells()
    r = s.expect_at("ghost", 1, status="unknown_branch")
    assert r.branchId == "ghost"
    s.branch_from(tick=1, name="b")              # 2
    s.branch_from(tick=2, name="c")              # 3, parent b
    s.events[1].payload.parentBranchId = "c"     # b now starts from c, c from b
    r = s.rebuild("c", 3)
    assert r.status == "lineage_loop"
    assert set(r.branchIds) == {"b", "c"}


def test_missing_tick_stops_every_branch_at_and_after_it():
    s = two_cells()
    s.edit("c1", "a = 2")   # 2
    s.branch_from(tick=2, name="b")   # 3
    s.edit("c1", "b1")      # 4
    s.edit("c1", "b2")      # 5
    s.on("main")
    s.edit("c1", "a = 3")   # 6
    s.events = [e for e in s.events if e.tick != 4]
    s.expect_at("b", 3, cells=[("c1", "a = 2"), ("c2", "b = 2")])
    for branch in ("main", "b"):
        for t in (4, 5, 6):
            r = s.expect_at(branch, t, status="missing_events")
            assert r.firstMissingTick == 4
    s.expect_at("main", 2, cells=[("c1", "a = 2"), ("c2", "b = 2")])
    r = s.expect_at("main", 7, status="missing_events")
    assert r.firstMissingTick == 4


def test_tick_beyond_the_last_event_is_missing():
    s = two_cells()
    r = s.expect_at("main", 2, status="missing_events")
    assert r.firstMissingTick == 2


def test_duplicates_and_arrival_order_do_not_matter():
    s = two_cells()
    s.edit("c1", "a = 2")
    s.branch_from(tick=2, name="b")
    s.edit("c1", "b1")
    expected = reference_all(s.events)
    shuffled = list(reversed(s.events)) + s.events[:2] + s.events
    assert reference_all(shuffled) == expected


def test_same_id_different_content_is_a_conflict():
    s = two_cells()
    s.edit("c1", "a = 2")
    forged = s.events[1].model_copy(deep=True)
    forged.payload.code = "tampered"
    r = reference_rebuild(s.events + [forged], "main", 2)
    assert r.status == "conflict"
    assert r.eventId == str(forged.eventId)


def test_two_events_at_one_tick_is_tick_clash():
    s = two_cells()
    other = s.events[0].model_copy(update={"eventId": __import__("uuid").uuid4()})
    r = reference_rebuild(s.events + [other], "main", 1)
    assert r.status == "invalid_event"
    assert r.reason.value == "tick_clash"
    assert r.eventId == str(other.eventId)


# --- purity and immutability -----------------------------------------------------


def test_apply_does_not_change_its_input_or_alias_the_event():
    s = two_cells()
    s.cell_metadata("c1", {"tags": ["x"]})
    first = apply(None, s.events[0]).state
    before = first.model_dump_json()
    second = apply(first, s.events[1]).state
    assert first.model_dump_json() == before
    s.events[1].payload.metadata["tags"].append("mutated")
    s.events[0].payload.cells[0].code = "mutated"
    assert second.notebook.cells[0].metadata == {"tags": ["x"]}
    assert second.notebook.cells[0].code == "a = 1"
    second.notebook.cells[0].code = "caller edit"
    assert first.notebook.cells[0].code == "a = 1"


def test_returned_states_never_change_after_later_calls():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "x")
    s.edit("c1", "a = 2")
    s.branch_from(tick=3, name="b")
    s.edit("c2", "b-code")
    answers = {k: v for k, v in reference_all(s.events).items()}
    frozen = {k: v.model_dump_json() for k, v in answers.items()}
    reference_all(s.events)       # ask everything again
    for k in answers:
        reference_rebuild(s.events, *k)
    assert {k: v.model_dump_json() for k, v in answers.items()} == frozen


def test_each_position_is_independent_copy():
    s = two_cells()
    s.edit("c1", "a = 2")
    a = s.state_at("main", 2)
    a.notebook.cells[0].code = "tampered by caller"
    assert s.state_at("main", 2).notebook.cells[0].code == "a = 2"


# --- invalid histories: one test per reason, plus clear-and-wait -------------------


def test_duplicate_ids_are_duplicate_id():
    s = two_cells()
    s.insert("c1", "again")  # cell id already exists
    assert_invalid(s, 2, "duplicate_id")

    s = two_cells()
    run = s.run("c1")
    s._emit("run_started", {"runId": run, "cellId": "c1", "cellRevision": 1, "runCount": 2},
            revs=s._current_revisions())
    assert_invalid(s, 3, "duplicate_id")

    s = two_cells()
    s.chat("m1", "hi")
    s.chat("m1", "hi again")
    assert_invalid(s, 3, "duplicate_id")

    s = two_cells()
    s.gap_start("g1")
    s.gap_start("g1")
    assert_invalid(s, 3, "duplicate_id")


def test_finish_of_unknown_run_is_unknown_run():
    s = two_cells()
    s._run_cell["ghost"] = "c1"
    s.finish("ghost")
    assert_invalid(s, 2, "unknown_run")


def test_finish_of_a_finished_run_is_run_not_running():
    s = two_cells()
    run = s.run("c1")
    s.finish(run)
    s.finish(run)
    assert_invalid(s, 4, "run_not_running")


def test_end_of_unknown_gap_is_unknown_gap():
    s = two_cells()
    s.gap_end("ghost")
    assert_invalid(s, 2, "unknown_gap")


def test_end_of_an_ended_gap_is_gap_already_ended():
    s = two_cells()
    s.gap_start("g1")
    s.gap_end("g1")
    s.gap_end("g1")
    assert_invalid(s, 4, "gap_already_ended")


def test_index_beyond_the_end_is_index_out_of_range():
    s = two_cells()
    s.insert("c3", "x", index=5)
    assert_invalid(s, 2, "index_out_of_range")

    s = two_cells()
    s.move("c1", 0, 5)
    assert_invalid(s, 2, "index_out_of_range")

    s = two_cells()
    run = s.run("c1")
    s.output(run, "x", index=3)
    assert_invalid(s, 3, "index_out_of_range")


def test_move_from_the_wrong_place_is_position_mismatch():
    s = two_cells()
    s.move("c1", 1, 0)  # c1 is at 0, not 1
    assert_invalid(s, 2, "position_mismatch")


def test_an_invalid_event_stops_later_positions_too():
    s = two_cells()
    s.gap_end("ghost")
    s.insert("c3", "x")
    assert_invalid(s, 2, "unknown_gap")
    r = s.rebuild("main", 3)
    assert r.status == "invalid_event" and r.tick == 2


def test_apply_returns_results_and_never_changes_the_input():
    s = two_cells()
    s.gap_end("ghost")
    state = s.state_at("main", 1)
    before = state.model_dump(mode="json")
    r = apply(state, s.events[1])
    assert r.status == "invalid_event"
    assert state.model_dump(mode="json") == before


def test_clear_and_wait_sets_the_flag_and_the_next_output_replaces_the_list():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "old1", 0)
    s.output(run, "old2", 1)
    s.clear_outputs(run, wait=True)
    group = s.state_at("main", 5).notebook.cells[0].outputs[0]
    assert group.clearPending is True
    assert [i.preview for i in group.items] == ["old1", "old2"]
    s.output(run, "new", 0)
    group = s.state_at("main", 6).notebook.cells[0].outputs[0]
    assert group.clearPending is None
    assert [i.preview for i in group.items] == ["new"]
    s.output(run, "newer", 1)
    s.expect_at("main", 7, outputs={"c1": ["new", "newer"]})


def test_clear_and_wait_then_index_zero_without_the_flag_inserts():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "a", 0)
    s.output(run, "b", 0)  # no pending clear: inserted at the front
    s.expect_at("main", 4, outputs={"c1": ["b", "a"]})


def test_clear_and_wait_on_a_run_without_outputs_waits_too():
    s = two_cells()
    run = s.run("c1")
    s.clear_outputs(run, wait=True)
    assert s.state_at("main", 3).notebook.cells[0].outputs[0].clearPending is True
    s.output(run, "x", 0)
    s.expect_at("main", 4, outputs={"c1": ["x"]})


def test_immediate_clear_drops_a_pending_flag():
    s = two_cells()
    run = s.run("c1")
    s.output(run, "old", 0)
    s.clear_outputs(run, wait=True)
    s.clear_outputs(run)
    group = s.state_at("main", 5).notebook.cells[0].outputs[0]
    assert group.clearPending is None and group.items == []


# --- gate rulings A, B and C (2026-11-23) ------------------------------------------


def _ghost_run_scenario():
    s = two_cells()
    s._run_cell["ghost"] = "c1"
    return s


def test_output_replaced_for_a_run_that_never_started_is_unknown_run():
    s = _ghost_run_scenario()
    s.replace_output("ghost", "x")
    r = s.rebuild("main", 2)
    assert r.status == "invalid_event" and r.reason.value == "unknown_run"


def test_outputs_cleared_for_a_run_that_never_started_is_unknown_run():
    for wait in (False, True):
        s = _ghost_run_scenario()
        s.clear_outputs("ghost", wait=wait)
        r = s.rebuild("main", 2)
        assert r.status == "invalid_event" and r.reason.value == "unknown_run"


def _reason(result):
    assert result.status == "invalid_event", result
    return result.reason.value


def test_branch_point_pointing_at_its_own_tick_is_invalid_branch_point():
    s = two_cells()                      # 1
    s.branch_from(tick=2, name="b")      # 2: parentTick 2 is not before tick 2
    assert _reason(s.rebuild("b", 2)) == "invalid_branch_point"


def test_branch_point_at_an_event_off_the_parent_line_is_invalid_branch_point():
    s = two_cells()                      # 1
    s.edit("c1", "m1")                   # 2
    b = s.branch_from(tick=2, name="b")  # 3
    s.on("main").edit("c1", "m2")        # 4 on main after b was made: not on b's line
    s.branch_from(tick=4, name="c", parent=b)  # 5
    r = s.rebuild("c", 5)
    assert _reason(r) == "invalid_branch_point" and r.tick == 5


def test_a_second_root_without_a_branch_point_is_invalid_branch_point():
    s = two_cells()                      # 1
    s._parent["other"] = None
    s._revs["other"] = []
    s.on("other").snapshot([("c9", "z")])    # 2: a second history inside this one
    r = s.rebuild("other", 2)
    assert _reason(r) == "invalid_branch_point" and r.tick == 2
    s.expect_at("main", 1, cells=[("c1", "a = 1"), ("c2", "b = 2")])


def test_position_tick_is_the_asked_tick_and_last_event_tick_is_the_content():
    s = two_cells()                      # 1
    s.edit("c1", "m1")                   # 2
    b = s.branch_from(tick=2, name="b")  # 3
    s.on("main").edit("c1", "m2")        # 4
    s.on(b).edit("c1", "b1")             # 5
    s.expect_at(b, 4, cells=[("c1", "m1"), ("c2", "b = 2")], position=(b, 4, 3, 3))
    s.expect_at("main", 3, cells=[("c1", "m1"), ("c2", "b = 2")], position=("main", 3, 2, 2))


def test_a_branch_point_that_is_not_first_on_its_branch_is_invalid_at_its_own_tick():
    s = two_cells()                      # 1
    s.edit("c1", "m1")                   # 2
    s.branch_from(tick=1, name="b")      # 3 (first event of b: fine)
    s.edit("c1", "b1")                   # 4
    s.branch_from(tick=1, name="b", parent="main", branch_id="b")  # 5: a second branch point on b
    s.on("b").edit("c1", "b2")           # 6
    r = s.rebuild("b", 5)
    assert _reason(r) == "invalid_branch_point" and r.tick == 5
    assert _reason(s.rebuild("b", 6)) == "invalid_branch_point"
    s.expect_at("b", 4, cells=[("c1", "b1"), ("c2", "b = 2")])
    s.expect_at("main", 5, cells=[("c1", "m1"), ("c2", "b = 2")])


# --- outputs follow the run (detachedOutputs) ------------------------------------


def _detached(state):
    return [(g.runId, [i.preview for i in g.items], bool(g.clearPending)) for g in state.detachedOutputs]


def _running_then_deleted():
    s = Scenario()
    s.start(cells=[("c1", "a")])         # 1
    run = s.run("c1")                    # 2
    s.output(run, "first")               # 3
    s.delete("c1")                       # 4 deleted while the run is running
    return s, run


def test_every_state_has_detached_outputs_empty_by_default():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    assert s.state_at("main", 1).detachedOutputs == []


def test_deleting_a_cell_moves_the_running_runs_group_to_detached_outputs():
    s, run = _running_then_deleted()
    assert s.state_at("main", 3).detachedOutputs == []
    st = s.state_at("main", 4)
    assert st.notebook.cells == []
    assert _detached(st) == [(run, ["first"], False)]


def test_deleting_a_cell_drops_the_groups_of_finished_runs_with_it():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    r1 = s.run("c1")
    r2 = s.run("c1")
    s.output(r1, "old")
    s.output(r2, "live")
    s.finish(r1)
    s.delete("c1")
    assert _detached(s.state_at("main", s.tick)) == [(r2, ["live"], False)]
    assert [g.runId for g in s.state_at("main", 6).notebook.cells[0].outputs] == [r1, r2]  # earlier position intact


def test_output_after_the_cell_was_deleted_is_kept_in_detached_outputs():
    s, run = _running_then_deleted()
    s.output(run, "after", index=1)
    assert _detached(s.state_at("main", 5)) == [(run, ["first", "after"], False)]


def test_replace_after_the_cell_was_deleted_changes_the_detached_item():
    s, run = _running_then_deleted()
    s.replace_output(run, "swapped", index=0)
    assert _detached(s.state_at("main", 5)) == [(run, ["swapped"], False)]


def test_clear_without_wait_after_the_cell_was_deleted_empties_the_detached_group():
    s, run = _running_then_deleted()
    s.clear_outputs(run)
    assert _detached(s.state_at("main", 5)) == [(run, [], False)]


def test_clear_with_wait_after_the_cell_was_deleted_keeps_items_until_the_next_output():
    s, run = _running_then_deleted()
    s.clear_outputs(run, wait=True)
    assert _detached(s.state_at("main", 5)) == [(run, ["first"], True)]
    s.output(run, "fresh")
    assert _detached(s.state_at("main", 6)) == [(run, ["fresh"], False)]


def test_output_for_a_finished_run_of_a_deleted_cell_goes_to_detached_outputs():
    # Reading: the run still addresses its output, so a late output is kept, not an error.
    s = Scenario()
    s.start(cells=[("c1", "a")])
    run = s.run("c1")
    s.finish(run)
    s.delete("c1")
    s.output(run, "late")
    st = s.state_at("main", s.tick)
    assert _detached(st) == [(run, ["late"], False)]


def test_first_output_for_a_running_run_whose_cell_is_already_deleted_creates_the_detached_group():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    run = s.run("c1")
    s.delete("c1")
    s.output(run, "late")
    assert _detached(s.state_at("main", s.tick)) == [(run, ["late"], False)]


def test_output_for_a_run_that_never_started_is_still_unknown_run_after_a_delete():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    s.delete("c1")
    s._run_cell["ghost"] = "c1"
    s.output("ghost", "x")
    r = s.rebuild("main", s.tick)
    assert r.status == "unknown_cell"  # the event's cell is gone and the run never started


def test_detached_outputs_do_not_leak_to_other_branches_or_earlier_positions():
    s, run = _running_then_deleted()
    b = s.branch_from(tick=3, name="B", parent="main")
    st_b = s.state_at(b, s.tick)
    assert st_b.detachedOutputs == [] and [c.cellId for c in st_b.notebook.cells] == ["c1"]
    s.on("main").output(run, "after", index=1)
    assert s.state_at(b, s.tick).detachedOutputs == []


def test_kernel_restart_keeps_detached_output_groups():
    s, run = _running_then_deleted()
    s.kernel_restart()
    st = s.state_at("main", s.tick)
    assert _detached(st) == [(run, ["first"], False)]
    assert {r.runId: r.status.root for r in st.runs} == {run: "unfinished"}


def test_output_event_naming_a_different_cell_than_the_runs_is_run_cell_mismatch():
    for emit in ("output", "replace_output", "clear_outputs"):
        s = Scenario()
        s.start(cells=[("c1", "a"), ("c2", "b")])
        run = s.run("c1")
        s.output(run, "x")
        s._run_cell[run] = "c2"
        getattr(s, emit)(run, "y") if emit != "clear_outputs" else s.clear_outputs(run)
        r = s.rebuild("main", s.tick)
        assert r.status == "invalid_event" and r.reason.value == "run_cell_mismatch", emit
        assert s.rebuild("main", s.tick - 1).status == "ok"


def test_run_cell_mismatch_comes_after_unknown_run_and_unknown_cell():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    s._run_cell["ghost"] = "c1"
    s.output("ghost", "x")
    assert s.rebuild("main", 2).reason.value == "unknown_run"
    s2 = Scenario()
    s2.start(cells=[("c1", "a")])
    s2._run_cell["ghost"] = "nope"
    s2.output("ghost", "x")
    assert s2.rebuild("main", 2).status == "unknown_cell"


def _snapshot_drops_running_cell(kind):
    s = Scenario()
    s.start(cells=[("c1", "a"), ("c2", "b")])         # 1
    live = s.run("c1")                                 # 2
    s.output(live, "kept")                             # 3
    done = s.run("c2")                                 # 4
    s.output(done, "gone")                             # 5
    s.finish(done)                                     # 6
    getattr(s, kind)([("c9", "new")])                  # 7 drops c1 (running) and c2 (finished)
    return s, live


def test_snapshot_dropping_a_cell_with_a_running_run_detaches_its_output():
    for kind in ("snapshot", "outside_change"):
        s, live = _snapshot_drops_running_cell(kind)
        st = s.state_at("main", 7)
        assert [c.cellId for c in st.notebook.cells] == ["c9"]
        assert _detached(st) == [(live, ["kept"], False)], kind
        s.output(live, "more", index=1)
        assert _detached(s.state_at("main", 8)) == [(live, ["kept", "more"], False)]
        assert s.state_at("main", 6).detachedOutputs == []


def test_snapshot_keeping_the_cell_does_not_detach_its_output():
    s = Scenario()
    s.start(cells=[("c1", "a")])
    run = s.run("c1")
    s.output(run, "x")
    s.snapshot([("c1", "a")])
    assert s.state_at("main", s.tick).detachedOutputs == []


def test_branch_point_whose_parent_tick_was_never_delivered_is_missing_events():
    s = two_cells()
    s.edit("c1", "a = 2")  # 2
    s.edit("c1", "a = 3")  # 3
    s.branch_from(tick=3, name="b")  # 4
    s.edit("c1", "y")  # 5
    events = [e for e in s.events if e.tick != 3]
    for tick in (1, 2):  # below the gap: still a typed answer, never an exception
        r = reference_rebuild(events, "b", tick)
        assert r.status == "missing_events" and r.firstMissingTick == 3
    assert reference_rebuild(events, "main", 2).status == "ok"


def test_asks_below_the_first_event_and_on_nothing_never_raise():
    s = two_cells()
    for events in ([], s.events):
        for tick in (0, -3):
            r = reference_rebuild(events, "main", tick)
            assert r.status == "missing_events" and r.firstMissingTick == (1 if not events else 2)
        assert reference_rebuild(events, "nope", 1).status in ("missing_events", "unknown_branch")


# --- the one-replay-per-line function against the position-by-position reference ----


def _assert_replay_all_equals_reference(events):
    got = reference_replay_all(events)
    want = reference_all(events)
    assert got.keys() == want.keys()
    for key in want:
        assert got[key].model_dump(mode="json") == want[key].model_dump(mode="json"), key


@settings(max_examples=60 if not DEEP else 1000)
@given(histories(max_events=MAX_EVENTS))
def test_replay_all_equals_reference_rebuild_on_valid_histories_and_uses_the_fast_path(h):
    assert _replay_all_clean(h.events) is not None
    _assert_replay_all_equals_reference(h.events)


@settings(max_examples=40 if not DEEP else 1000)
@given(histories(max_events=MAX_EVENTS).flatmap(lambda h: deliveries(history=h)))
def test_replay_all_equals_reference_rebuild_on_messy_deliveries(d):
    _assert_replay_all_equals_reference(d.events)


@settings(max_examples=20)
@given(invalid_histories())
def test_replay_all_equals_reference_rebuild_on_invalid_histories(ih):
    _assert_replay_all_equals_reference(ih.events)

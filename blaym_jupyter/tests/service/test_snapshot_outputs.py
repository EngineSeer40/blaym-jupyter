"""A snapshot never removes outputs recorded from the kernel (S5 fix, live-testing LT-2)."""

from __future__ import annotations

import pytest

from blaym_jupyter.testing.memory import MemorySearchIndex
from blaym_jupyter.tests.history.scenario import Scenario, output_item
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, recorder


def base():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def send(rig, events):
    for out in rig.outcomes(events):
        assert out.status == "accepted", out


def cell_at(rig, tick=None, cell="c1"):
    state = rig.state(rig.root, tick or len(rig.stored())).state
    assert not state.gaps
    return next(c for c in state.notebook.cells if c.cellId == cell)


def texts(cell):
    return [i.preview for g in cell.outputs for i in g.items]


def snapshot_with(s, cells):
    """A snapshot as the browser sends it; `cells` are (id, type, output previews)."""
    raw = recorder(s.snapshot([(c, "x = 1") for c, _, _ in cells]))
    for payload, (_, kind, previews) in zip(raw["payload"]["cells"], cells):
        payload["cellType"] = kind
        payload["outputs"] = [{"runId": "page", "items": [output_item(t) for t in previews]}] if previews else []
    return raw


def test_a_rerun_then_a_snapshot_with_no_outputs_keeps_the_output():
    rig, s = Rig(), base()
    for text in ("first", "second"):
        run = s.run("c1")
        s.output(run, text)
        s.finish(run)
    send(rig, s.events)
    before = texts(cell_at(rig))
    send(rig, [snapshot_with(s, [("c1", "code", [])])])
    assert texts(cell_at(rig)) == before and "second" in before


def test_a_snapshot_built_before_a_run_and_applied_after_it_keeps_that_runs_output():
    rig, s = Rig(), base()
    stale = snapshot_with(s, [("c1", "code", ["old page output"])])
    run = s.run("c1")
    s.output(run, "from the run")
    s.finish(run)
    send(rig, [e for e in s.events if e.type != "notebook_snapshot" or e.tick == 1])
    send(rig, [stale])
    assert texts(cell_at(rig)) == ["from the run"]


def test_a_snapshot_during_a_streaming_run_is_accepted_and_the_final_output_is_complete():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "line 1")
    send(rig, s.events)
    send(rig, [snapshot_with(s, [("c1", "code", [])])])
    send(rig, [s.output(run, "line 2", index=1), s.finish(run)])
    assert texts(cell_at(rig)) == ["line 1", "line 2"]


def test_a_snapshot_showing_only_the_first_lines_keeps_the_full_output():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "line 1\nline 2\nline 3")
    s.finish(run)
    send(rig, s.events)
    send(rig, [snapshot_with(s, [("c1", "code", ["line 1"])])])
    assert texts(cell_at(rig)) == ["line 1\nline 2\nline 3"]


def test_a_first_snapshot_and_a_new_cell_keep_the_snapshots_outputs():
    rig, s = Rig(), base()
    first = recorder(s.events[0])
    first["payload"]["cells"][0]["outputs"] = [{"runId": "page", "items": [output_item("on open")]}]
    send(rig, [first])
    send(rig, [snapshot_with(s, [("c1", "code", []), ("c2", "code", ["came with the cell"])])])
    assert texts(cell_at(rig)) == ["on open"]
    assert texts(cell_at(rig, cell="c2")) == ["came with the cell"]


def test_code_to_markdown_by_snapshot_drops_outputs_and_the_earlier_tick_keeps_them():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "kept earlier")
    s.finish(run)
    send(rig, s.events)
    earlier = len(rig.stored())
    send(rig, [snapshot_with(s, [("c1", "markdown", ["ignored"])])])
    assert texts(cell_at(rig)) == []
    assert texts(cell_at(rig, earlier)) == ["kept earlier"]


# --- a code cell stops being code while its run is still running ------------------------------


def _retype(s, how, kind):
    if how == "snapshot":
        return snapshot_with(s, [("c1", kind, [])])
    return recorder(s.change_type("c1", kind))


def _running_with_one_line():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "line 1")
    send(rig, s.events)
    return rig, s, run


def detached_texts(rig):
    state = rig.state(rig.root, len(rig.stored())).state
    return [i.preview for g in state.detachedOutputs for i in g.items]


@pytest.mark.parametrize("kind", ["markdown", "raw"])
@pytest.mark.parametrize("how", ["snapshot", "type_changed"])
def test_output_after_a_running_cell_stops_being_code_is_kept_detached(how, kind):
    rig, s, run = _running_with_one_line()
    send(rig, [_retype(s, how, kind)])
    send(rig, [s.output(run, "line 2", index=1), s.replace_output(run, "line 2b", index=1),
               s.output(run, "line 0", index=0), s.finish(run)])
    assert texts(cell_at(rig)) == []
    assert detached_texts(rig) == ["line 0", "line 1", "line 2b"]


@pytest.mark.parametrize("how", ["snapshot", "type_changed"])
def test_the_first_output_after_a_running_cell_stops_being_code_does_not_land_on_it(how):
    rig, s = Rig(), base()
    run = s.run("c1")
    send(rig, s.events)
    send(rig, [_retype(s, how, "markdown")])
    send(rig, [s.output(run, "first"), s.finish(run)])
    assert texts(cell_at(rig)) == []
    assert detached_texts(rig) == ["first"]


@pytest.mark.parametrize("how", ["snapshot", "type_changed"])
def test_a_running_cell_turned_back_into_code_keeps_receiving_its_output(how):
    rig, s, run = _running_with_one_line()
    send(rig, [_retype(s, how, "markdown"), _retype(s, how, "code")])
    send(rig, [s.output(run, "line 2", index=1), s.finish(run)])
    assert detached_texts(rig) == ["line 1", "line 2"]


def test_a_deleted_running_cell_that_comes_back_keeps_receiving_its_output():
    rig, s, run = _running_with_one_line()
    send(rig, [snapshot_with(s, [("c2", "code", [])]), snapshot_with(s, [("c1", "code", [])])])
    send(rig, [s.output(run, "line 2", index=1), s.finish(run)])
    assert detached_texts(rig) == ["line 1", "line 2"]


@pytest.mark.parametrize("every_edit", [False, True])
@pytest.mark.parametrize("one_batch", [False, True])
def test_rebuilding_the_index_equals_indexing_live_when_snapshot_outputs_are_ignored(every_edit, one_batch):
    idx = MemorySearchIndex()
    rig, s = Rig(search_index=idx, index_every_edit=every_edit), base()
    run = s.run("c1")
    s.output(run, "recorded")
    snapshot_with(s, [("c1", "code", ["page only"])])
    snapshot_with(s, [("c1", "markdown", ["on markdown"])])
    s.output(run, "after retype", index=1)
    s.finish(run)
    snapshot_with(s, [("c1", "code", []), ("c9", "code", ["new cell output"])])
    events = [recorder(e) for e in s.events]
    # `snapshot_with` edits the wire form only: put its cell types and outputs back in
    edits = {3: [("code", ["page only"])], 4: [("markdown", ["on markdown"])],
             7: [("code", []), ("code", ["new cell output"])]}
    for i, cells in edits.items():
        for payload, (kind, previews) in zip(events[i]["payload"]["cells"], cells):
            payload["cellType"] = kind
            payload["outputs"] = [{"runId": "page", "items": [output_item(t) for t in previews]}] if previews else []
    for batch in [events] if one_batch else [[e] for e in events]:
        send(rig, batch)

    def indexed():
        return sorted((d.tick, d.kind, d.text) for d in idx._docs)

    live = indexed()
    found = {text for _, _, text in live}
    assert {"recorded", "after retype", "new cell output"} <= found
    assert not {"page only", "on markdown"} & found
    rig.service.rebuild_search(TOKEN, rig.exp)
    assert indexed() == live


# --- a new run replaces finished output (Task 1b) ---------------------------------------------


def _finished_run(s, text, cell="c1"):
    run = s.run(cell)
    s.output(run, text)
    s.finish(run)
    return run


def test_a_rerun_replaces_the_earlier_output_and_the_earlier_tick_keeps_it():
    rig, s = Rig(), base()
    _finished_run(s, "first")
    send(rig, s.events)
    earlier = len(rig.stored())
    sent = len(s.events)
    _finished_run(s, "second")
    send(rig, s.events[sent:])
    assert texts(cell_at(rig)) == ["second"]
    assert texts(cell_at(rig, earlier)) == ["first"]


def test_a_rerun_that_prints_nothing_leaves_no_output():
    rig, s = Rig(), base()
    _finished_run(s, "first")
    s.finish(s.run("c1"))
    send(rig, s.events)
    assert cell_at(rig).outputs == []


def test_a_new_run_keeps_the_output_of_a_run_still_running():
    rig, s = Rig(), base()
    _finished_run(s, "over")
    one = s.run("c1")
    s.output(one, "one")
    two = s.run("c1")
    s.output(two, "two")
    s.output(one, "one more", index=1)
    send(rig, s.events)
    assert texts(cell_at(rig)) == ["one", "one more", "two"]


def test_a_run_replaces_pasted_output():
    rig, s = Rig(), base()
    s.insert("c2", "y", outputs=["pasted"])
    send(rig, s.events)
    earlier = len(rig.stored())
    sent = len(s.events)
    s.run("c2")
    send(rig, s.events[sent:])
    assert cell_at(rig, cell="c2").outputs == []
    assert texts(cell_at(rig, earlier, cell="c2")) == ["pasted"]


def test_code_to_markdown_and_back_by_type_change_leaves_no_output():
    rig, s = Rig(), base()
    _finished_run(s, "shown while code")
    send(rig, s.events)
    earlier = len(rig.stored())
    send(rig, [_retype(s, "type_changed", "markdown")])
    assert texts(cell_at(rig)) == []
    send(rig, [_retype(s, "type_changed", "code")])
    assert texts(cell_at(rig)) == []
    assert texts(cell_at(rig, earlier)) == ["shown while code"]

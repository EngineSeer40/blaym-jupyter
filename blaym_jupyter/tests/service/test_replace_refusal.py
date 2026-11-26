"""Task 18a: an outside change follows the snapshot rule for outputs and is never refused for a run."""

from __future__ import annotations

from blaym_jupyter.tests.history.scenario import Scenario, output_item
from blaym_jupyter.tests.service.helpers import Rig, recorder


def base():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def send(rig, events):
    for out in rig.outcomes(events):
        assert out.status == "accepted", out


def outside(s, cells):
    """An outside change as the watcher sends it; `cells` are (id, type, output previews)."""
    raw = recorder(s.outside_change([(c, "x = 1") for c, _, _ in cells]))
    for payload, (_, kind, previews) in zip(raw["payload"]["cells"], cells):
        payload["cellType"] = kind
        payload["outputs"] = [{"runId": "file", "items": [output_item(t) for t in previews]}] if previews else []
    return raw


def cell_at(rig, tick=None, cell="c1"):
    state = rig.state(rig.root, tick or len(rig.stored())).state
    assert not state.gaps
    return next(c for c in state.notebook.cells if c.cellId == cell)


def texts(cell):
    return [i.preview for g in cell.outputs for i in g.items]


def test_outside_change_with_no_outputs_keeps_the_recorded_output():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "recorded")
    s.finish(run)
    send(rig, s.events)
    send(rig, [outside(s, [("c1", "code", [])])])
    assert texts(cell_at(rig)) == ["recorded"]


def test_outside_change_during_a_run_is_accepted_and_the_runs_output_is_complete():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "line 1")
    send(rig, s.events)
    send(rig, [outside(s, [("c1", "code", ["from file"])])])
    send(rig, [s.output(run, "line 2", index=1), s.finish(run)])
    assert texts(cell_at(rig)) == ["line 1", "line 2"]


def test_outside_change_new_cell_takes_the_files_outputs():
    rig, s = Rig(), base()
    send(rig, s.events)
    send(rig, [outside(s, [("c1", "code", []), ("c2", "code", ["from file"])])])
    assert texts(cell_at(rig, cell="c2")) == ["from file"]


def test_outside_code_to_markdown_drops_outputs_and_the_earlier_tick_keeps_them():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "kept earlier")
    s.finish(run)
    send(rig, s.events)
    earlier = len(rig.stored())
    send(rig, [outside(s, [("c1", "markdown", ["ignored"])])])
    assert texts(cell_at(rig)) == []
    assert texts(cell_at(rig, earlier)) == ["kept earlier"]

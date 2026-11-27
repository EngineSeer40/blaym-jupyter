"""A snapshot never removes outputs recorded from the kernel."""

from __future__ import annotations


from blaym_notebook.tests.history.scenario import Scenario, output_item
from blaym_notebook.tests.service.helpers import Rig, recorder


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


# --- a new run replaces finished output ---------------------------------------------


def outside(s, cells):
    """An outside change as the watcher sends it; `cells` are (id, type, output previews)."""
    raw = recorder(s.outside_change([(c, "x = 1") for c, _, _ in cells]))
    for payload, (_, kind, previews) in zip(raw["payload"]["cells"], cells):
        payload["cellType"] = kind
        payload["outputs"] = [{"runId": "file", "items": [output_item(t) for t in previews]}] if previews else []
    return raw


def test_outside_change_with_no_outputs_keeps_the_recorded_output():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.output(run, "recorded")
    s.finish(run)
    send(rig, s.events)
    send(rig, [outside(s, [("c1", "code", [])])])
    assert texts(cell_at(rig)) == ["recorded"]

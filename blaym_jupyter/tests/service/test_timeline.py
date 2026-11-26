"""S5 Task 11: the timeline index and events-between reads.

One row per event type for the classification, one agreement test against the reference rebuilder
(tick order, branch ownership, run results), and the limits.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings

from blaym_jupyter_types import (
    BlaymEvent,
    EventsBetweenRequest,
    EventsSinceRequest,
    OutlineRequest,
    StateAtRequest,
    TimelineIndexRequest,
)

from blaym_jupyter.history import reference_rebuild
from blaym_jupyter.service.core import UnknownHistory
from blaym_jupyter.service.timeline import entry_of, kind_of
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, replay


def _with_mimes(ev, mimes):
    d = ev.model_dump(mode="json", exclude_none=True)
    d["payload"]["item"]["mimeTypes"] = mimes
    return BlaymEvent.model_validate(d).root


def _scenario():
    """One event of every type (24), plus the variants that change the kind."""
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.insert("c2", "y = 2")
    s.move("c2", 1, 0)
    s.change_type("c2", "markdown")
    s.edit("c1", "x = 2")
    s.cell_metadata("c1", {"tags": ["a"]})
    s.notebook_metadata({"k": 1})
    r1 = s.run("c1")
    s.output(r1, "hi")
    s.replace_output(r1, "hi2")
    s.finish(r1, "ok")
    r2 = s.run("c1")
    s.finish(r2, "error")
    r3 = s.run("c1")
    s.finish(r3, "interrupted")
    r4 = s.run("c1")
    s.clear_outputs(r4)
    s.clear_cell_outputs("c1")
    s.kernel_start()
    s.kernel_restart()
    s.rename("analysis.ipynb", "b.ipynb")
    s.copy("b.ipynb", "c.ipynb")
    s.outside_change([("c1", "z")], path="b.ipynb")
    s.chat("m1", "why?")
    s.chat("m2", "because", sender="agent-1", kind="agent")
    s.chat_update("m2", "because of x")
    s.gap_start()
    s.gap_end()
    s.branch_from(s.tick, "try")
    return s


# (event type, how to pick it among the scenario's events) -> expected kind
ROWS = [
    ("notebook_snapshot", 0, "snapshot"),
    ("cell_inserted", 0, "insert"),
    ("cell_moved", 0, "move"),
    ("cell_type_changed", 0, "type"),
    ("cell_source_changed", 0, "edit"),
    ("cell_metadata_changed", 0, "other"),
    ("notebook_metadata_changed", 0, "other"),
    ("run_started", 0, "run_started"),
    ("output_added", 0, "text_output"),
    ("output_replaced", 0, "text_output"),
    ("run_finished", 0, "run_ok"),
    ("run_finished", 1, "run_error"),
    ("run_finished", 2, "run_interrupted"),
    ("outputs_cleared", 0, "other"),
    ("cell_outputs_cleared", 0, "other"),
    ("kernel_started", 0, "kernel_start"),
    ("kernel_restarted", 0, "kernel_restart"),
    ("notebook_renamed", 0, "other"),
    ("notebook_copied", 0, "other"),
    ("changed_outside_editor", 0, "outside"),
    ("chat_message_added", 0, "chat_prompt"),
    ("chat_message_added", 1, "chat_reply"),
    ("chat_message_updated", 0, "chat_reply"),
    ("recording_gap_started", 0, "gap"),
    ("recording_gap_ended", 0, "gap"),
    ("branch_point_created", 0, "branch_point"),
]


def test_every_event_type_has_its_kind():
    s = _scenario()
    s.on("main")
    s.delete("c1")
    by_type: dict[str, list] = {}
    for ev in s.events:
        by_type.setdefault(ev.type, []).append(ev)
    assert len(by_type) == 24, sorted(by_type)  # the contract's event types (the spec says 23; it has 24)
    for type_, nth, kind in ROWS + [("cell_deleted", 0, "delete")]:
        assert kind_of(by_type[type_][nth]) == kind, (type_, nth)
    assert {t for t, _, _ in ROWS} | {"cell_deleted"} == set(by_type)


def test_output_kinds_follow_the_mime_types():
    s = Scenario()
    s.start(cells=[("c1", "x")])
    run = s.run("c1")
    s.output(run, "t")
    ev = s.events[-1]
    assert kind_of(_with_mimes(ev, ["application/vnd.plotly.v1+json", "text/plain"])) == "chart"
    assert kind_of(_with_mimes(ev, ["text/html", "text/plain"])) == "table"
    assert kind_of(_with_mimes(ev, ["application/vnd.dataresource+json"])) == "table"
    assert kind_of(_with_mimes(ev, ["image/png"])) == "text_output"


def test_entry_carries_author_cell_and_run():
    s = _scenario()
    entries = {ev.type: entry_of(ev) for ev in s.events}
    assert entries["changed_outside_editor"][3] == "outside"
    assert entries["notebook_snapshot"][3] == "unknown" and entries["notebook_snapshot"][4] is None
    assert entries["cell_source_changed"][5] == "c1" and entries["notebook_snapshot"][5] is None
    started = next(ev for ev in s.events if ev.type == "run_started")
    assert entry_of(started)[6] == started.payload.runId


def ticks_of(response):
    return [t.root for t in response.ticks]


def _own(outline, branch_id):
    return {t.root for b in outline.branches if b.branchId == branch_id for t in b.ownTicks}


@settings(max_examples=15, deadline=None, derandomize=True)
@given(h=histories(max_events=40, service_safe=True))
def test_index_agrees_with_the_rebuilder_on_every_branch(h):
    assume(all(ev.type != "notebook_copied" for ev in h.events))  # recorders may not send it (S5-I)
    rig = Rig()
    try:
        ids = replay(rig, h.events)
    except AssertionError:  # the generator branched from an unsettled moment: the service refuses it
        assume(False)
    outline = rig.service.outline(TOKEN, OutlineRequest(explorationId=rig.exp))
    for gen_branch, branch in ids.items():
        idx = rig.service.timeline_index(TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId=branch))
        ticks = ticks_of(idx)
        since = rig.service.events_since(TOKEN, EventsSinceRequest(explorationId=rig.exp, branchId=branch, afterTick=0))
        assert ticks == [e.root.tick for e in since.events]  # the line, by the older read
        assert ticks == sorted(set(ticks)) and _own(outline, branch) <= set(ticks)
        root = next(b for b in outline.branches if b.parentBranchId is None).branchId
        if branch == root:
            assert set(ticks) == _own(outline, root)
        assert not idx.truncated
        assert len({len(c) for c in (idx.ticks, idx.timesMs, idx.kinds, idx.authors, idx.confidences,
                                     idx.cellIds, idx.runIds)}) == 1
        last = ticks[-1]
        state = reference_rebuild(rig.stored(), branch, last)
        assert state.status == "ok"
        statuses = [getattr(r.status.root, "value", r.status.root) for r in state.state.runs]
        finished = [x for x in statuses if x in ("ok", "error", "interrupted")]
        kinds = [k.value for k in idx.kinds if k.value.startswith("run_") and k.value != "run_started"]
        assert sorted(kinds) == sorted("run_" + s for s in finished)
        assert sum(k.value == "run_started" for k in idx.kinds) == len(state.state.runs)


@pytest.fixture
def seeded():
    rig = Rig()
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 12):
        s.edit("c1", f"x = {i}")
    replay(rig, s.events)
    return rig, rig.root


def test_limits_and_windows(seeded):
    rig, root = seeded
    svc = rig.service

    def idx(**kw):
        return svc.timeline_index(TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId=root, **kw))

    assert ticks_of(idx()) == list(range(1, 13)) and not idx().truncated
    cut = idx(limit=5)
    assert ticks_of(cut) == [1, 2, 3, 4, 5] and cut.truncated and cut.nextTick == 6
    rest = idx(fromTick=cut.nextTick, toTick=9)  # both ends inclusive
    assert ticks_of(rest) == [6, 7, 8, 9] and not rest.truncated and rest.nextTick is None
    assert ticks_of(idx(fromTick=50)) == [] and idx(fromTick=50).kinds == []

    def between(**kw):
        return svc.events_between(TOKEN, EventsBetweenRequest(
            explorationId=rig.exp, branchId=root, fromTick=3, toTick=10, **kw))

    full = between()
    assert [e.root.tick for e in full.events] == list(range(3, 11)) and not full.truncated
    part = between(maxEvents=3)
    assert [e.root.tick for e in part.events] == [3, 4, 5] and part.truncated and part.nextTick == 6


def test_unknown_history_and_branch_and_credential(seeded):
    rig, root = seeded
    with pytest.raises(UnknownHistory):
        rig.service.timeline_index(TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId="nope"))
    with pytest.raises(UnknownHistory):
        rig.service.events_between(TOKEN, EventsBetweenRequest(
            explorationId="elsewhere", branchId=root, fromTick=1, toTick=2))


def test_rerun_and_snapshot_after_a_run_agree_with_the_rebuilder():
    """Run results come from run events only; a snapshot's own outputs add no output kinds."""
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    first = s.run("c1")
    s.output(first, "one")
    s.finish(first, "ok")
    again = s.run("c1")  # a re-run replaces the finished output
    s.output(again, "two")
    s.finish(again, "error")
    s.snapshot([("c1", "x = 1")])
    rig = Rig()
    replay(rig, s.events)
    idx = rig.service.timeline_index(TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId=rig.root))
    kinds = [k.value for k in idx.kinds]
    assert kinds == ["snapshot", "run_started", "text_output", "run_ok", "run_started", "text_output",
                     "run_error", "snapshot"]
    state = reference_rebuild(rig.stored(), rig.root, idx.ticks[-1].root).state
    assert sorted(getattr(r.status.root, "value", r.status.root) for r in state.runs) == ["error", "ok"]

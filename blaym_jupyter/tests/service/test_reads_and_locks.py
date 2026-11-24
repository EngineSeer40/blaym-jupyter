"""S5 Task 2: reads never wait for a write and never see uncommitted state.

The write is slowed in the store (the real wait of a slow disk); a read must answer from the last
committed state within 50 ms. A refused event whose dry run already changed the cached tip must
never be visible to a read. Generated writer/reader mixes are compared with the reference rebuilder.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blaym_jupyter_types import (
    BlaymEvent,
    EventsSinceRequest,
    GapSource,
    OutlineRequest,
    RebuildInvalidEvent,
    RebuildOk,
    ResolveCellRevisionRequest,
    StateAtRequest,
)

from blaym_jupyter.history import build_outline, reference_rebuild
from blaym_jupyter.history.apply import Failure
from blaym_jupyter.service import core
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.service.core import Service
from blaym_jupyter.testing.fakes import IdFactory
from blaym_jupyter.testing.memory import MemoryEventStore
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create, recorder
from blaym_jupyter.tests.history.scenario import Scenario

FAST = 0.05


class SlowStore(MemoryEventStore):
    """Appends take `delay` seconds once armed; `entered` is set when one has begun."""

    def __init__(self):
        super().__init__()
        self.delay = 0.0
        self.entered = threading.Event()
        self.hydrations = 0

    def append(self, *a, **kw):
        if self.delay:
            self.entered.set()
            time.sleep(self.delay)
        return super().append(*a, **kw)

    def events_for(self, exploration_id):
        self.hydrations += 1
        if self.delay:
            time.sleep(self.delay)
        return super().events_for(exploration_id)


def slow_rig():
    rig = Rig()
    rig.events = SlowStore()
    rig.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("svc"))
    return rig


def base_scenario():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def reads(rig):
    """Every read, each timed; returns {name: (seconds, answer)}."""
    svc, exp = rig.service, rig.exp
    root = rig.root
    calls = {
        "state_at": lambda: svc.state_at(TOKEN, StateAtRequest(explorationId=exp, branchId=root, tick=1)),
        "state_tip": lambda: svc.state_at(TOKEN, StateAtRequest(
            explorationId=exp, branchId=root, tick=svc._exps[exp].tip_tick)),
        "outline": lambda: svc.outline(TOKEN, OutlineRequest(explorationId=exp)),
        "events_since": lambda: svc.events_since(TOKEN, EventsSinceRequest(
            explorationId=exp, branchId=root, afterTick=0)),
        "resolve": lambda: svc.resolve_cell_revision(TOKEN, ResolveCellRevisionRequest(
            explorationId=exp, cellId="c1", code="x = 1")),
        "cell_revisions": lambda: svc.cell_revisions(TOKEN, exp),
    }
    out = {}
    for name, call in calls.items():
        t = time.perf_counter()
        answer = call()
        out[name] = (time.perf_counter() - t, answer)
    return out


def test_reads_answer_from_committed_state_while_a_two_second_write_runs():
    rig, s = slow_rig(), base_scenario()
    rig.outcomes(s.events)
    root = rig.root
    rig.events.delay = 2.0
    edit = recorder(s.edit("c1", "x = 2"))
    writer = threading.Thread(target=lambda: rig.outcomes([edit]))
    start = time.perf_counter()
    writer.start()
    assert rig.events.entered.wait(5)
    answers = reads(rig)
    assert time.perf_counter() - start < 1.5, "the reads must be done long before the write ends"
    for name, (seconds, _) in answers.items():
        assert seconds < FAST, f"{name} waited {seconds:.3f}s for the write"
    tip = answers["state_tip"][1]
    assert isinstance(tip, RebuildOk) and tip.state.notebook.cells[0].code == "x = 1"
    assert [t.root for t in answers["outline"][1].branches[0].ownTicks] == [1]
    assert [e.root.tick for e in answers["events_since"][1].events] == [1]
    assert answers["resolve"][1].root.status == "ok"
    assert answers["cell_revisions"][1] == {"c1": 1}
    writer.join(10)
    rig.events.delay = 0
    assert rig.state(root, 2).state.notebook.cells[0].code == "x = 2"
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 2}


def test_a_cold_service_hydrates_once_and_concurrent_readers_agree():
    rig, s = slow_rig(), base_scenario()
    rig.outcomes(s.events + [s.edit("c1", "x = 2")])
    root = rig.root
    cold = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("cold"))
    rig.events.delay = 0.2  # a slow load
    rig.events.hydrations = 0
    got = []

    def read():
        got.append(cold.state_at(TOKEN, StateAtRequest(explorationId=rig.exp, branchId=root, tick=2)))

    threads = [threading.Thread(target=read) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert rig.events.hydrations == 1
    assert len(got) == 6 and all(g == got[0] for g in got) and isinstance(got[0], RebuildOk)


def test_a_refused_event_changed_in_place_in_the_tip_is_never_visible_to_a_read(monkeypatch):
    rig, s = Rig(), base_scenario()
    rig.outcomes(s.events)
    root = rig.root
    rig.outcomes([s.edit("c1", "x = 2")])
    entered, release = threading.Event(), threading.Event()
    real_step = core.step

    def step_then_refuse(state, ev):
        out = real_step(state, ev)  # the tip is now changed in place
        if ev.type == "cell_source_changed" and ev.payload.code == "REFUSED":
            entered.set()
            assert release.wait(10)
            raise Failure(RebuildInvalidEvent(status="invalid_event", eventId=str(ev.eventId), tick=ev.tick,
                                              reason="duplicate_id", detail="planted"))
        return out

    monkeypatch.setattr(core, "step", step_then_refuse)
    bad = recorder(s.edit("c1", "REFUSED"))
    result = []
    writer = threading.Thread(target=lambda: result.append(rig.outcomes([bad])[0]))
    writer.start()
    assert entered.wait(5)
    # the premise: the cached tip really holds the refused code right now
    assert rig.service._exps[rig.exp].tips[root].notebook.cells[0].code == "REFUSED"
    tip_tick = rig.service._exps[rig.exp].tip_tick
    state = rig.service.state_at(TOKEN, StateAtRequest(explorationId=rig.exp, branchId=root, tick=tip_tick)).state
    assert state.notebook.cells[0].code == "x = 2"
    assert rig.service.resolve_cell_revision(TOKEN, ResolveCellRevisionRequest(
        explorationId=rig.exp, cellId="c1", code="REFUSED")).root.status == "code_differs"
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 2}
    release.set()
    writer.join(10)
    assert result[0].status == "refused" and result[0].reason == "invalid_event"
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 2}


# -- generated reader/writer mixes against the reference rebuilder ----------------------------

def _gap(e):
    if e.type == "recording_gap_started" and e.payload.source == "service":
        return e.model_copy(update={"payload": e.payload.model_copy(update={"source": GapSource.browser})})
    return e


MARKER = "REFUSED-MARKER"


def write_history(rig, h, stop, refuse_every):
    ids = {}
    n = 0
    for ev in h.events:
        if ev.type == "notebook_copied":
            break
        if ev.type == "branch_point_created":
            out = create(rig, ids[ev.payload.parentBranchId], ev.payload.parentTick, ev.payload.name)
            if out.status != "ok":
                break
            ids[ev.branchId] = out.branch.branchId
            continue
        if not ids:
            o = rig.outcomes([_gap(ev)])[0]
            ids[ev.branchId] = o.branchId
        else:
            if n % refuse_every == 0:
                cell = getattr(ev.payload, "cellId", None)
                if cell is not None:  # a refused event that changes the cached tip's code in place
                    bad = {"schemaVersion": 2, "eventId": str(uuid.uuid4()), "explorationId": rig.exp,
                           "wallTimeMs": 1, "userId": "local", "workspaceId": "local", "originId": "o",
                           "type": "cell_source_changed",
                           "payload": {"cellId": cell, "code": MARKER, "revision": 10_000}}
                    rig.outcomes([bad], target=ids[ev.branchId])
            o = rig.outcomes([_gap(ev)], target=ids[ev.branchId])[0]
            if o.status != "accepted":  # for example a snapshot while a run is running (S5-T)
                break
        n += 1
    stop.set()


def reader(rig, stop, seen):
    svc, exp = rig.service, rig.exp
    while not stop.is_set():
        outline = svc.outline(TOKEN, OutlineRequest(explorationId=exp))
        if outline is None:
            continue
        for b in outline.branches:
            if not b.ownTicks:
                continue
            tick = b.ownTicks[-1].root
            res = svc.state_at(TOKEN, StateAtRequest(explorationId=exp, branchId=b.branchId, tick=tick))
            seen.append(("state", b.branchId, tick, res))
        seen.append(("outline", outline))


@settings(max_examples=25, deadline=None)
@given(h=histories(max_events=30, service_safe=True), refuse_every=st.integers(2, 4))
def test_generated_readers_and_a_writer_never_see_uncommitted_or_half_applied_state(h, refuse_every):
    rig = Rig(interval=4)
    stop = threading.Event()
    seen_by = [[] for _ in range(3)]
    threads = [threading.Thread(target=reader, args=(rig, stop, seen)) for seen in seen_by]
    writer = threading.Thread(target=write_history, args=(rig, h, stop, refuse_every))
    for t in threads:
        t.start()
    writer.start()
    writer.join(60)
    for t in threads:
        t.join(60)
    stored = rig.stored()
    final = stored[-1].tick if stored else 0
    for seen in seen_by:
        for item in seen:
            if item[0] == "state":
                _, branch, tick, res = item
                assert tick <= final
                ref = reference_rebuild(stored, branch, tick)
                assert res.status == ref.status == "ok"
                assert res.state == ref.state, "a read saw a state that differs from the committed history"
                assert MARKER not in res.state.model_dump_json()
            else:
                outline = item[1]
                top = max(t.root for b in outline.branches for t in b.ownTicks)
                assert top <= final
                assert outline == build_outline(stored[:top]).outline
    assert all(MARKER not in e.model_dump_json() for e in stored)

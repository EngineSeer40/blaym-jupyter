"""Reads never wait for a write: they answer from the last committed state."""

from __future__ import annotations

import threading

from blaym_notebook_types import (
    EventsSinceRequest,
    OutlineRequest,
    RebuildOk,
    ResolveCellRevisionRequest,
    StateAtRequest,
)

from blaym_notebook.service.auth import FixedTokenAuthorizer
from blaym_notebook.service.core import Service
from blaym_notebook.testing.fakes import IdFactory
from blaym_notebook.testing.memory import MemoryEventStore
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, recorder


class GatedStore(MemoryEventStore):
    """Once armed, an append stops at `entered` until `release` is set."""

    def __init__(self):
        super().__init__()
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def append(self, *a, **kw):
        if self.armed:
            self.entered.set()
            assert self.release.wait(10)
        return super().append(*a, **kw)


def test_reads_answer_from_committed_state_while_a_write_is_held_in_the_store():
    rig = Rig()
    rig.events = GatedStore()
    rig.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("svc"))
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    rig.outcomes(s.events)
    root, exp, svc = rig.root, rig.exp, rig.service
    rig.events.armed = True
    edit = recorder(s.edit("c1", "x = 2"))
    writer = threading.Thread(target=lambda: rig.outcomes([edit]))
    writer.start()
    assert rig.events.entered.wait(5)
    tip = svc.state_at(TOKEN, StateAtRequest(explorationId=exp, branchId=root, tick=svc._exps[exp].tip_tick))
    outline = svc.outline(TOKEN, OutlineRequest(explorationId=exp))
    since = svc.events_since(TOKEN, EventsSinceRequest(explorationId=exp, branchId=root, afterTick=0))
    resolved = svc.resolve_cell_revision(TOKEN, ResolveCellRevisionRequest(explorationId=exp, cellId="c1", code="x = 1"))
    revisions = svc.cell_revisions(TOKEN, exp)
    assert writer.is_alive(), "the reads returned only because the write finished"
    assert isinstance(tip, RebuildOk) and tip.state.notebook.cells[0].code == "x = 1"
    assert [t.root for t in outline.branches[0].ownTicks] == [1]
    assert [e.root.tick for e in since.events] == [1]
    assert resolved.root.status == "ok" and revisions == {"c1": 1}
    rig.events.release.set()
    writer.join(10)
    assert rig.state(root, 2).state.notebook.cells[0].code == "x = 2"
    assert svc.cell_revisions(TOKEN, exp) == {"c1": 2}

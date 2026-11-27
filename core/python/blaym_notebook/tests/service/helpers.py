"""Shared helpers for the service tests."""

from __future__ import annotations

from blaym_notebook_types import GapSource

from blaym_notebook_types import IngestRequest, StateAtRequest

from blaym_notebook.service.auth import FixedTokenAuthorizer
from blaym_notebook.service.core import Service
from blaym_notebook.testing.fakes import FakeClock, IdFactory
from blaym_notebook.testing.memory import MemoryBlobStore, MemoryEventStore
from blaym_notebook.tests.history.scenario import EXPLORATION

TOKEN = "t0ken"


class Rig:
    def __init__(self, **kw):
        self.events = MemoryEventStore()
        self.blobs = MemoryBlobStore()
        self.clock = FakeClock()
        self.service = Service(self.events, self.blobs, FixedTokenAuthorizer(TOKEN), self.clock,
                               new_id=IdFactory("svc"), **kw)
        self.exp = EXPLORATION

    def send(self, events, target=None, exp=None, credential=TOKEN):
        req = {"explorationId": exp or self.exp, "editor": "jupyterlab", "instanceId": "tab-1",
               "events": [recorder(e) for e in events]}
        if target:
            req["targetBranchId"] = target
        return self.service.ingest(credential, req)

    def outcomes(self, events, **kw):
        return [o.root if hasattr(o, "root") else o for o in self.send(events, **kw).outcomes]

    def stored(self):
        return self.events.events_for(self.exp)

    def state(self, branch, tick):
        return self.service.state_at(TOKEN, StateAtRequest(explorationId=self.exp, branchId=branch, tick=tick))

    @property
    def root(self):
        return self.events.exploration(self.exp).active_branch_id


def recorder(event):
    """A history event (or a mapping) as a RecorderEvent mapping: no tick, no branch."""
    if isinstance(event, dict):
        return event
    d = event.model_dump(mode="json", exclude_none=True)
    d.pop("tick", None)
    d.pop("branchId", None)
    return d


def strip_hash(d):
    if isinstance(d, dict):
        return {k: strip_hash(v) for k, v in d.items() if k != "contentHash"}
    if isinstance(d, list):
        return [strip_hash(v) for v in d]
    return d


def key(ev, ids=None):
    """A stored or generated event without its tick, with branch ids mapped through `ids`
    (generated id -> service id) and content hashes left out."""
    import json
    from blaym_notebook.ports.events import canonical_event_json
    ids = ids or {}
    d = json.loads(canonical_event_json(ev))
    d.pop("tick", None)
    d["branchId"] = ids.get(d["branchId"], d["branchId"])
    if d["type"] == "branch_point_created":  # made by the service: its own id, time and origin
        for k in ("eventId", "wallTimeMs", "originId", "authorHint", "authorConfidence", "userId", "workspaceId"):
            d.pop(k, None)
        d["payload"]["parentBranchId"] = ids.get(d["payload"]["parentBranchId"], d["payload"]["parentBranchId"])
    return strip_hash(d)


def create(rig, parent, tick, name="try"):
    from blaym_notebook_types import CreateBranchRequest
    r = rig.service.create_branch(TOKEN, CreateBranchRequest(
        explorationId=rig.exp, fromBranchId=parent, fromTick=tick, name=name))
    return r.root


def replay(rig, events, ids=None, rnd=None, until=None):
    """Send generated/scenario events to the service in tick order. A branch point becomes
    a create_branch call; others are ingested to their branch (through targetBranchId or, at
    random, after set_active_branch). `ids` maps generated branch ids to service ids.
    Returns the ids map; `until` stops after that many events."""
    ids = {} if ids is None else ids
    for i, ev in enumerate(events):
        if until is not None and i >= until:
            break
        if ev.type == "branch_point_created":
            out = create(rig, ids[ev.payload.parentBranchId], ev.payload.parentTick, ev.payload.name)
            assert out.status == "ok", out
            ids[ev.branchId] = out.branch.branchId
            continue
        if ev.type == "recording_gap_started" and ev.payload.source == "service":
            ev = ev.model_copy(update={"payload": ev.payload.model_copy(update={"source": GapSource.browser})})
        if not ids:
            o = rig.outcomes([ev])[0]
            ids[ev.branchId] = o.branchId
        elif rnd is not None and rnd.random() < 0.5:
            from blaym_notebook_types import SetActiveBranchRequest
            rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=rig.exp, branchId=ids[ev.branchId]))
            o = rig.outcomes([ev])[0]
        else:
            o = rig.outcomes([ev], target=ids[ev.branchId])[0]
        assert o.status == "accepted" and not o.duplicate, o
    return ids

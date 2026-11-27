"""Ingest through the service on in-memory ports."""

from __future__ import annotations

import uuid


from blaym_jupyter_types import RebuildInvalidRevision, RebuildOk
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import Rig, recorder



def basic():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def test_first_snapshot_makes_root_branch_active_and_tick_1():
    rig, s = Rig(), basic()
    [o] = rig.outcomes(s.events)
    assert o.status == "accepted" and o.tick == 1 and not o.duplicate
    assert o.branchId == rig.root
    assert [b.branch_id for b in rig.events.branches(rig.exp)] == [rig.root]
    assert rig.stored()[0].branchId == rig.root


def test_identical_resend_is_duplicate_with_existing_record_and_no_write_or_notice():
    rig, s = Rig(), basic()
    s.edit("c1", "x = 2")
    seen = []
    rig.outcomes(s.events[:1])
    rig.service.subscribe(rig.exp, seen.append)
    first = rig.outcomes(s.events)
    again = rig.outcomes(s.events)
    assert [o.duplicate for o in first] == [True, False]
    assert [(o.tick, o.duplicate) for o in again] == [(1, True), (2, True)]
    assert len(rig.stored()) == 2 and [a.tick for a in seen] == [2]


def test_same_id_different_content_is_conflict_and_first_stays():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    r = recorder(s.events[0])
    r["payload"]["path"] = "other.ipynb"
    [o] = rig.outcomes([r])
    assert o.status == "refused" and o.reason == "conflict"
    assert rig.stored()[0].payload.path == "analysis.ipynb"


def test_invalid_event_refused_with_typed_result_gap_once_and_snapshot_closes_it():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    s.edit("c1", "x = 2")
    s.edit("c1", "x = 3", revision=9)  # revision jump
    bad = recorder(s.events[-1])
    [o] = rig.outcomes([bad])
    assert o.status == "refused" and o.reason == "invalid_event"
    assert isinstance(o.rebuild.root if hasattr(o.rebuild, "root") else o.rebuild, RebuildInvalidRevision)
    stored = rig.stored()
    assert [e.type for e in stored] == ["notebook_snapshot", "recording_gap_started"]
    gap = stored[-1]
    assert gap.payload.source == "service" and gap.payload.reason == "unknown"
    assert gap.originId == "blaym-service" and gap.authorHint == "blaym" and gap.authorConfidence == "known"
    unknown_run = {**recorder(s.events[0]), "eventId": str(uuid.uuid4()), "type": "run_finished",
                   "payload": {"runId": "nope", "status": "ok"}}
    [o2] = rig.outcomes([unknown_run])
    assert o2.reason == "invalid_event" and len(rig.stored()) == 2  # no second gap
    snap = recorder(s.snapshot([("c1", "x = 2")]))
    [o3] = rig.outcomes([snap])
    types = [e.type for e in rig.stored()]
    assert types[-2:] == ["recording_gap_ended", "notebook_snapshot"] and o3.tick == 4
    assert rig.stored()[-2].payload.gapId == gap.payload.gapId
    assert isinstance(rig.state(rig.root, 4), RebuildOk)


# -- generated sequences ----------------------------------------------------------


def _strip_hash(d):
    if isinstance(d, dict):
        return {k: _strip_hash(v) for k, v in d.items() if k != "contentHash"}
    if isinstance(d, list):
        return [_strip_hash(v) for v in d]
    return d



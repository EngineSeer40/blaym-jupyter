"""Branches, the active branch, events_since and the generated multi-branch replay."""

from __future__ import annotations

from blaym_notebook_types import GapSource

import random

import pytest
from hypothesis import given, settings, strategies as st

from blaym_notebook.history import build_outline
from blaym_notebook.tests.history.reference import reference_rebuild
from blaym_notebook_types import OutlineRequest, RebuildOk, SetActiveBranchRequest

from blaym_notebook.privacy.redact import redact_event
from blaym_notebook.tests.history.generate import histories
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, create, key, replay


def setup(rig=None):
    rig = rig or Rig()
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    ids = replay(rig, s.events)
    return rig, s, ids


def fresh_service(rig):
    """A second service over the same stores (nothing hydrated)."""
    from blaym_notebook.service.auth import FixedTokenAuthorizer
    from blaym_notebook.service.core import Service
    from blaym_notebook.testing.fakes import IdFactory
    other = Rig()
    other.events, other.blobs = rig.events, rig.blobs
    other.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("other"))
    return other


def test_every_create_branch_refusal_reason():
    from blaym_notebook_types import CreateBranchRequest
    rig, s, ids = setup()
    root = ids["main"]
    r = create(rig, root, 1)
    assert r.status == "ok"
    child = r.branch.branchId  # tick 3
    stored = list(rig.stored())
    assert create(rig, "nope", 1).reason == "unknown_moment"
    assert create(rig, root, 99).reason == "unknown_moment"  # above the highest tick
    assert create(rig, root, 3).reason == "not_on_line"  # the child's own branch point
    assert create(rig, child, 2).reason == "not_on_line"  # root tick 2 is after the cut at 1
    assert create(Rig(), root, 1).reason == "unknown_moment"  # unknown exploration
    assert "tick" in create(rig, root, 99).detail
    bad = rig.service.create_branch("wrong", CreateBranchRequest(
        explorationId=rig.exp, fromBranchId=root, fromTick=1, name="x"))
    assert bad.root.reason == "unauthorized"
    with pytest.raises(Exception):  # tick below 1 is not even a valid request
        CreateBranchRequest(explorationId=rig.exp, fromBranchId=root, fromTick=0, name="x")
    assert rig.stored() == stored  # no refusal wrote anything


def test_hole_and_unrebuildable_moments():
    import json
    from blaym_notebook.ports.events import canonical_event_json
    rig, s, ids = setup()
    root = ids["main"]
    rows = rig.events._events[rig.exp]
    d = json.loads(rows[2])
    d["tick"], d["eventId"] = 3, "5c1f0a6e-0000-4000-8000-0000000000aa"
    rows[3] = json.dumps(d, sort_keys=True, separators=(",", ":"))  # revision 2 again: invalid
    d["tick"], d["eventId"] = 5, "5c1f0a6e-0000-4000-8000-0000000000bb"
    rows[5] = json.dumps(d, sort_keys=True, separators=(",", ":"))  # tick 4 is a hole
    other = fresh_service(rig)
    assert create(other, root, 2).status == "ok"  # also writes tick 6
    other = fresh_service(rig)
    assert create(other, root, 4).reason == "unknown_moment"
    assert create(other, root, 3).reason == "moment_not_rebuildable"


def test_late_delivery_to_a_non_active_branch_and_unknown_target():
    rig, s, ids = setup()
    root = ids["main"]
    child = create(rig, root, 2).branch.branchId
    s.on("main").edit("c1", "late")
    out = rig.outcomes([s.events[-1]], target=root)[0]
    assert out.status == "accepted" and out.branchId == root and rig.root == child
    assert rig.state(root, out.tick).state.notebook.cells[0].code == "late"
    s.edit("c1", "other")
    assert rig.outcomes([s.events[-1]], target="nope")[0].reason == "unknown_branch"


# -- generated multi-branch histories through the service ---------------------------


STATS = {"created": 0, "unsettled": 0}


def run_generated(rig, h, rnd, sample=True):
    """Replay `h` through the service one event at a time with an independent model."""
    ids: dict = {}
    active_model = None
    n_stored = 0
    for i, ev in enumerate(h.events):
        if ev.type == "notebook_copied":
            break
        if ev.type == "branch_point_created":
            ref = reference_rebuild(h.events[:i], ev.payload.parentBranchId, ev.payload.parentTick)
            running = ref.status == "ok" and any(r.status.root == "running" for r in ref.state.runs)
            out = create(rig, ids[ev.payload.parentBranchId], ev.payload.parentTick, ev.payload.name)
            STATS["unsettled" if running else "created"] += 1
            if running:
                assert out.status == "refused" and out.reason == "unsettled_moment"
                break
            assert out.status == "ok", out
            ids[ev.branchId] = out.branch.branchId
            active_model = ids[ev.branchId]
        else:
            before = rig.root if ids else None
            if not ids:
                replay(rig, [ev], ids=ids)
                active_model = ids[ev.branchId]
            else:
                if rnd.random() < 0.5:
                    rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(
                        explorationId=rig.exp, branchId=ids[ev.branchId]))
                    active_model = ids[ev.branchId]
                    o = rig.outcomes([_gap(ev)])[0]
                else:
                    o = rig.outcomes([_gap(ev)], target=ids[ev.branchId])[0]
                assert o.status == "accepted" and not o.duplicate, o
        stored = rig.stored()
        assert [e.tick for e in stored] == list(range(1, len(stored) + 1)) == list(range(1, i + 2))
        assert key(stored[-1]) == key(_expected(ev), ids)
        assert rig.root == active_model
        if sample and rnd.random() < 0.4:
            br = rnd.choice(sorted(set(e.branchId for e in stored)))
            t = rnd.choice([e.tick for e in stored if e.branchId == br])
            assert rig.state(br, t).model_dump() == reference_rebuild(stored, br, t).model_dump()
            assert isinstance(rig.state(br, t), RebuildOk)
    stored = rig.stored()
    assert [key(e) for e in stored] == [key(_expected(e), ids) for e in h.events[:len(stored)]]
    assert rig.service.outline(TOKEN, OutlineRequest(explorationId=rig.exp)) == build_outline(stored).outline
    return ids


def _gap(ev):
    if ev.type == "recording_gap_started" and ev.payload.source == "service":
        return ev.model_copy(update={"payload": ev.payload.model_copy(update={"source": GapSource.browser})})
    return ev


def _expected(ev):
    return redact_event(_gap(ev))


@settings(max_examples=120)
@given(h=histories(shapes=("one", "nested", "siblings", "before_parent", "first", "mixed"), max_events=40, service_safe=True),
       seed=st.integers(0, 10**6))
def test_generated_multi_branch_histories_through_the_service(h, seed):
    run_generated(Rig(interval=5), h, random.Random(seed))



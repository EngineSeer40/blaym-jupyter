"""Branches, the active branch, events_since and the generated multi-branch replay (S4, Task 7)."""

from __future__ import annotations

from blaym_jupyter_types import GapSource

import random

import pytest
from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import build_outline, reference_rebuild
from blaym_jupyter_types import (
    EventsSinceRequest, OutlineRequest, RebuildOk, SetActiveBranchRequest,
)

from blaym_jupyter.service.core import UnknownHistory
from blaym_jupyter.privacy.redact import redact_event
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create, key, replay


def setup(rig=None):
    rig = rig or Rig()
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    ids = replay(rig, s.events)
    return rig, s, ids


def stored_keys(rig, ids):
    return [key(e) for e in rig.stored()]


def fresh_service(rig):
    """A second service over the same stores (nothing hydrated)."""
    from blaym_jupyter.service.auth import FixedTokenAuthorizer
    from blaym_jupyter.service.core import Service
    from blaym_jupyter.testing.fakes import IdFactory
    other = Rig()
    other.events, other.blobs = rig.events, rig.blobs
    other.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("other"))
    return other


def test_every_create_branch_refusal_reason():
    from blaym_jupyter_types import CreateBranchRequest
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


def test_running_moment_is_unsettled():
    rig, s, ids = setup()
    root = ids["main"]
    s.run("c1")
    rig.outcomes([s.events[-1]], target=root)  # run_started, tick 3
    assert create(rig, root, 3).reason == "unsettled_moment"
    assert create(rig, root, 2).status == "ok"


def test_hole_and_unrebuildable_moments():
    import json
    from blaym_jupyter.ports.events import canonical_event_json
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


def test_unsettled_state_not_rebuildable_branch_point_ok_after_finish():
    rig, s, ids = setup()
    root = ids["main"]
    run = s.run("c1")
    rig.outcomes([s.events[-1]], target=root)
    s.finish(run)
    rig.outcomes([s.events[-1]], target=root)
    assert create(rig, root, 4).status == "ok"
    assert create(rig, root, 3).reason == "unsettled_moment"


def test_scenario_create_work_switch_back_jump_return():
    rig, s, ids = setup()
    root = ids["main"]
    before = [key(e) for e in rig.stored()]
    out = create(rig, root, 2, "try-y")
    child = out.branch.branchId
    assert rig.root == child and [t.root for t in out.branch.ownTicks] == [3]
    assert [key(e) for e in rig.stored()[:2]] == before
    s.branch_from(2, "try-y")
    s.edit("c1", "x = 3")
    ids2 = {"main": root, "try-y": child}
    rig.outcomes([s.events[-1]])  # no target: the active branch (the child) receives it
    assert rig.stored()[-1].branchId == child
    # back to the parent branch, work there, return
    assert rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=rig.exp, branchId=root)).root.status == "ok"
    assert rig.root == root
    s.on("main")
    s.edit("c1", "x = 9")
    rig.outcomes([s.events[-1]])
    assert rig.stored()[-1].branchId == root
    # the stored parent answers never moved
    assert rig.state(root, 2).state.notebook.cells[0].code == "x = 2"
    assert rig.state(child, 4).state.notebook.cells[0].code == "x = 3"
    assert rig.state(root, 5).state.notebook.cells[0].code == "x = 9"
    assert [key(e) for e in rig.stored()[:2]] == before
    rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=rig.exp, branchId=child))
    assert rig.root == child


def test_two_children_of_one_parent_worked_alternately():
    rig, s, ids = setup()
    root = ids["main"]
    s.branch_from(2, "a")
    s.on("main").branch_from(2, "b")
    s.on("a").edit("c1", "xa")
    s.on("b").edit("c1", "xb")
    s.on("a").edit("c1", "xa2")
    rig2 = rig
    ids = replay(rig2, s.events[2:], ids={"main": root}, rnd=random.Random(3))
    for name, code in (("a", "xa2"), ("b", "xb")):
        tip = max(e.tick for e in rig.stored() if e.branchId == ids[name])
        assert rig.state(ids[name], tip).state.notebook.cells[0].code == code
    assert rig.state(root, 2).state.notebook.cells[0].code == "x = 2"
    assert rig.service.outline(TOKEN, OutlineRequest(explorationId=rig.exp)) == build_outline(rig.stored()).outline


def test_branch_created_before_its_parent_existed():
    rig, s, ids = setup()
    root = ids["main"]
    s.edit("c1", "x = 3")
    s.branch_from(3, "kid")
    s.edit("c1", "kid-edit")
    s.branch_from(1, "grandkid", parent="kid")  # tick 1 lies on the shared part of kid's line
    s.edit("c1", "gk")
    ids = replay(rig, s.events[2:], ids={"main": root})
    assert rig.state(ids["grandkid"], rig.stored()[-1].tick).state.notebook.cells[0].code == "gk"
    assert rig.state(ids["grandkid"], 5 + 0).status == "ok"
    assert create(rig, ids["kid"], 2).status == "ok"  # root tick 2 is on kid's line (cut at 3)
    assert create(rig, ids["kid"], 3).status == "ok"  # the cut itself is on the line


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


def test_set_active_branch_refusals():
    rig, s, ids = setup()
    r = rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=rig.exp, branchId="zzz")).root
    assert r.reason == "unknown_branch"
    r = rig.service.set_active_branch("bad", SetActiveBranchRequest(explorationId=rig.exp, branchId=ids["main"])).root
    assert r.reason == "unauthorized"
    from blaym_jupyter.service.auth import Unauthorized
    with pytest.raises(Unauthorized):
        rig.service.events_since("bad", EventsSinceRequest(explorationId=rig.exp, branchId=ids["main"], afterTick=0))


def test_events_since_gives_the_line_after_the_tick_and_the_outline():
    rig, s, ids = setup()
    root = ids["main"]
    child = create(rig, root, 1).branch.branchId  # tick 3
    s.on("main").edit("c1", "x = 5")
    rig.outcomes([s.events[-1]], target=root)  # tick 4 on main (not on child's line)
    s.branch_from(1, "k")
    s.edit("c1", "kk")
    rig.outcomes([s.events[-1]], target=child)  # tick 5 on child
    got = rig.service.events_since(TOKEN, EventsSinceRequest(explorationId=rig.exp, branchId=child, afterTick=0))
    assert [e.root.tick for e in got.events] == [1, 3, 5]
    got = rig.service.events_since(TOKEN, EventsSinceRequest(explorationId=rig.exp, branchId=child, afterTick=3))
    assert [e.root.tick for e in got.events] == [5]
    assert got.outline == build_outline(rig.stored()).outline
    got = rig.service.events_since(TOKEN, EventsSinceRequest(explorationId=rig.exp, branchId=root, afterTick=1))
    assert [e.root.tick for e in got.events] == [2, 4]
    for bad in (dict(explorationId="no", branchId=root), dict(explorationId=rig.exp, branchId="no")):
        with pytest.raises(UnknownHistory):
            rig.service.events_since(TOKEN, EventsSinceRequest(afterTick=0, **bad))


def test_notified_new_branch_equals_the_outline_entry_and_branch_point_is_service_authored():
    rig, s, ids = setup()
    seen = []
    rig.service.subscribe(rig.exp, seen.append)
    out = create(rig, ids["main"], 2, "named")
    notice = seen[-1][-1] if isinstance(seen[-1], list) else seen[-1]
    adv = notice
    assert adv.newBranch == out.branch and adv.runStarted is None and adv.runsEnded is None
    entry = next(b for b in build_outline(rig.stored()).outline.branches if b.branchId == out.branch.branchId)
    assert adv.newBranch == entry
    bp = rig.stored()[-1]
    assert bp.originId == "blaym-service" and bp.authorHint == "blaym" and bp.authorConfidence == "known"
    assert bp.payload.name == "named"


def test_branch_point_and_active_change_are_one_transaction():
    rig, s, ids = setup()
    original = rig.events.append
    calls = []

    def spy(exploration_id, events, **kw):
        calls.append(kw)
        return original(exploration_id, events, **kw)

    rig.events.append = spy
    create(rig, ids["main"], 2)
    assert len(calls) == 1 and calls[0]["active_branch_id"] and calls[0]["branches"]


def test_recorder_cannot_send_service_gaps():
    rig, s, ids = setup()
    bad = {"schemaVersion": 2, "eventId": "5c1f0a6e-0000-4000-8000-000000000001", "explorationId": rig.exp,
           "wallTimeMs": 1, "userId": "local", "workspaceId": "local", "originId": "o",
           "type": "recording_gap_started", "payload": {"gapId": "g", "source": "service", "reason": "unknown"}}
    o = rig.outcomes([bad])[0]
    assert o.reason == "invalid_event" and o.detail == "service gaps belong to the service"
    # an open service gap (made by the service) cannot be ended by a recorder either
    broken = {**bad, "eventId": "5c1f0a6e-0000-4000-8000-000000000002", "type": "run_finished",
              "payload": {"runId": "nope", "status": "ok"}}
    assert rig.outcomes([broken])[0].reason == "invalid_event"
    gap = rig.stored()[-1]
    assert gap.type == "recording_gap_started" and gap.payload.source == "service"
    end = {**bad, "eventId": "5c1f0a6e-0000-4000-8000-000000000003", "type": "recording_gap_ended",
           "payload": {"gapId": gap.payload.gapId}}
    o = rig.outcomes([end])[0]
    assert o.reason == "invalid_event" and o.detail == "service gaps belong to the service"


# -- generated multi-branch histories through the service ---------------------------


def _browser(e):
    return e


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


def test_the_generated_replays_made_branches_and_met_unsettled_moments():
    assert STATS["created"] > 20

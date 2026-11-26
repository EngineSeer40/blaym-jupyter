"""S5-T: no outside change while a run on the branch is running (a snapshot keeps outputs, so it may)."""

from __future__ import annotations

from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create, recorder
from blaym_jupyter.tests.history.scenario import Scenario


def base():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def outside(s):
    return recorder(s.outside_change([("c1", "x = 1"), ("c2", "new")]))


def refused_reason(out):
    return out.reason if out.status == "refused" else None


def started(rig, s):
    rig.outcomes(s.events)
    run = s.run("c1")
    rig.outcomes([s.events[-1]])
    return run


def test_snapshot_is_accepted_while_a_run_is_running():
    rig, s = Rig(), base()
    run = started(rig, s)
    assert rig.outcomes([s.snapshot([("c1", "y")])])[0].status == "accepted"
    state = rig.state(rig.root, len(rig.stored())).state
    assert [g.runId for g in state.notebook.cells[0].outputs] == []  # no output yet, and no gap
    assert not state.gaps and rig.outcomes([s.output(run, "after")])[0].status == "accepted"


def test_outside_change_is_refused_while_a_run_is_running_and_nothing_is_stored_or_gapped():
    rig, s = Rig(), base()
    started(rig, s)
    before = len(rig.stored())
    out = rig.outcomes([s.outside_change([("c1", "y")])])[0]
    assert refused_reason(out) == "run_in_progress"
    assert len(rig.stored()) == before  # no event, and no service gap either
    assert rig.state(rig.root, before).state.notebook.cells[0].code == "x = 1"


def test_snapshot_and_outside_change_are_accepted_after_run_finished():
    rig, s = Rig(), base()
    run = started(rig, s)
    snap = recorder(s.outside_change([("c1", "y")]))
    assert refused_reason(rig.outcomes([snap])[0]) == "run_in_progress"
    assert rig.outcomes([s.finish(run)])[0].status == "accepted"
    assert rig.outcomes([snap])[0].status == "accepted"  # the same snapshot, delivered again
    run2 = s.run("c1")
    rig.outcomes([s.events[-1]])
    assert refused_reason(rig.outcomes([outside(s)])[0]) == "run_in_progress"
    rig.outcomes([s.finish(run2)])
    assert rig.outcomes([outside(s)])[0].status == "accepted"


def test_snapshot_and_outside_change_are_accepted_after_a_kernel_restart():
    rig, s = Rig(), base()
    started(rig, s)
    snap = recorder(s.outside_change([("c1", "y")]))
    assert refused_reason(rig.outcomes([snap])[0]) == "run_in_progress"
    assert rig.outcomes([s.kernel_restart()])[0].status == "accepted"
    assert rig.outcomes([snap])[0].status == "accepted"
    assert rig.outcomes([outside(s)])[0].status == "accepted"


def test_a_duplicate_delivery_of_an_accepted_outside_change_stays_a_duplicate_during_a_later_run():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    snap = recorder(s.outside_change([("c1", "y")]))
    first = rig.outcomes([snap])[0]
    assert first.status == "accepted" and not first.duplicate
    s.run("c1")
    rig.outcomes([s.events[-1]])
    again = rig.outcomes([snap])[0]
    assert again.status == "accepted" and again.duplicate
    assert refused_reason(rig.outcomes([s.outside_change([("c1", "z")])])[0]) == "run_in_progress"


def test_other_events_are_accepted_during_a_run():
    rig, s = Rig(), base()
    run = started(rig, s)
    for e in (s.edit("c1", "x = 2"), s.insert("c2", "y"), s.output(run, "out"), s.cell_metadata("c1", {"a": 1})):
        assert rig.outcomes([e])[0].status == "accepted", e.type


def test_only_a_run_on_the_target_branch_blocks():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    root = rig.root
    child = create(rig, root, 1).branch.branchId
    s.branch_from(1, "child", branch_id=child)  # keep the scenario's own bookkeeping in step
    run = s.run("c1")
    ev = recorder(s.events[-1])
    assert rig.outcomes([ev], target=child)[0].status == "accepted"
    snap_parent = recorder(s.outside_change([("c1", "p")]))
    assert rig.outcomes([snap_parent], target=root)[0].status == "accepted"  # the run is on the child
    snap_child = recorder(s.outside_change([("c1", "q")]))
    assert refused_reason(rig.outcomes([snap_child], target=child)[0]) == "run_in_progress"
    rig.outcomes([s.finish(run)], target=child)
    assert rig.outcomes([snap_child], target=child)[0].status == "accepted"


def test_the_first_snapshot_of_a_history_is_never_refused():
    rig, s = Rig(), base()
    assert rig.outcomes(s.events)[0].status == "accepted"


# -- reviewer additions ------------------------------------------------------------------------

def test_a_refused_outside_change_has_no_side_effect_not_even_a_blob():
    from blaym_jupyter_types import StoreLimits
    from blaym_jupyter.tests.history.scenario import output_item
    rig, s = Rig(limits=StoreLimits(inlineOutputBytes=1024)), base()
    started(rig, s)
    before_events, before_blobs = len(rig.stored()), list(rig.blobs.hashes(rig.exp))
    big = "z" * 5000  # over the inline limit: ingest would move it to a blob
    snap = s.outside_change([("c1", "y")])
    raw = recorder(snap)
    raw["payload"]["cells"][0]["outputs"] = [{"runId": "r-old", "items": [output_item(big)]}]
    out = rig.outcomes([raw])[0]
    assert refused_reason(out) == "run_in_progress", out
    assert len(rig.stored()) == before_events
    assert list(rig.blobs.hashes(rig.exp)) == before_blobs
    from blaym_jupyter.tests.service.helpers import TOKEN as _t
    assert rig.service.cell_revisions(_t, rig.exp) == {"c1": 1}


def test_a_cold_service_refuses_from_the_stored_run_and_a_refusal_changes_nothing_for_later_events():
    from blaym_jupyter.service.auth import FixedTokenAuthorizer
    from blaym_jupyter.service.core import Service
    from blaym_jupyter.testing.fakes import IdFactory
    rig, s = Rig(), base()
    run = started(rig, s)
    cold = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("cold"))
    rig.service = cold
    snap = recorder(s.outside_change([("c1", "y")]))
    assert refused_reason(rig.outcomes([snap])[0]) == "run_in_progress"
    assert refused_reason(rig.outcomes([snap])[0]) == "run_in_progress"  # still refused, still no gap
    n = len(rig.stored())
    assert rig.outcomes([s.output(run, "late output")])[0].status == "accepted"  # not orphaned
    assert rig.outcomes([s.finish(run)])[0].status == "accepted"
    assert len(rig.stored()) == n + 2
    assert rig.outcomes([snap])[0].status == "accepted"

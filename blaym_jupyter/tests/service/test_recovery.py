"""Recovery rules (S4, Task 9), on the in-memory ports with a manual clock."""

from __future__ import annotations

from blaym_jupyter_types import RebuildOk

from blaym_jupyter.tests.history.scenario import Scenario, reference_rebuild
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create
from blaym_jupyter_types import SetActiveBranchRequest


def started(cells=(("c1", "x = 1"),)):
    """A rig with a root branch holding a snapshot, ready for events."""
    rig, s = Rig(), Scenario()
    s.start(cells=list(cells))
    rig.outcomes(s.events)
    return rig, s


def open_run(rig, s, branch=None):
    mark = len(s.events)
    run = s.run("c1")
    rig.outcomes(s.events[mark:], target=branch)
    return run


def types(rig):
    return [(e.tick, e.branchId, e.type) for e in rig.stored()]


_restarts = iter(range(1, 1000))


def reopen(rig):
    """A new Service over the same stores: what a restart gives (nothing cached)."""
    from blaym_jupyter.service.auth import FixedTokenAuthorizer
    from blaym_jupyter.service.core import Service
    from blaym_jupyter.testing.fakes import IdFactory
    rig.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory(f"again{next(_restarts)}"))


def recovered(rig, restart=False):
    if restart:
        reopen(rig)
    rig.service.recover()
    return rig.service.recovery_report


def set_active(rig, branch):
    rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=rig.exp, branchId=branch))


def test_a_clean_history_gets_one_start_up_gap_on_the_active_branch():  # rule (c)
    rig, _ = started()
    report = recovered(rig)
    gap = rig.stored()[-1]
    assert gap.type == "recording_gap_started" and gap.branchId == rig.root
    assert (gap.payload.source.value, gap.payload.reason.value) == ("service", "server_unavailable")
    assert (gap.originId, gap.authorHint.value, gap.authorConfidence.value) == ("blaym-service", "blaym", "known")
    assert report.gaps_written == [(rig.exp, rig.root, gap.payload.gapId)] and not report.runs_closed


def test_repeated_recovery_changes_nothing_the_second_time():  # idempotence
    rig, s = started()
    open_run(rig, s)
    first = recovered(rig)
    before = types(rig)
    second = recovered(rig)
    assert first.changed and not second.changed
    assert types(rig) == before and second.gaps_written == [] and second.runs_closed == []


def test_an_open_run_on_the_active_branch_is_closed_and_a_late_real_finish_is_refused():  # (d)
    rig, s = started()
    run = open_run(rig, s)
    report = recovered(rig)
    last = rig.stored()[-1]
    assert (last.type, last.payload.runId, last.payload.status.value) == ("run_finished", run, "interrupted")
    assert (last.originId, last.authorHint.value) == ("blaym-service", "blaym")
    assert report.runs_closed == [(rig.exp, rig.root, run)]
    # the recorder never saw the crash: its later real run_finished finds the run closed
    out = rig.outcomes([s.finish(run)])[0]
    assert (out.status, out.reason) == ("refused", "invalid_event")
    assert "run_not_running" in repr(out.rebuild) + out.detail


def test_open_runs_on_a_branch_that_is_not_active_are_closed_too():  # supervisor correction (i)
    rig, s = started()
    root = rig.root
    out = create(rig, root, 1, "b")
    b = out.branch.branchId
    s.current = "main"
    s.on("main")
    run_b = open_run(rig, s, branch=b)  # run left open on b
    set_active(rig, root)
    run_root = open_run(rig, s, branch=root)  # and one on the root, which is active
    report = recovered(rig)
    assert sorted((br, r) for _, br, r in report.runs_closed) == sorted([(b, run_b), (root, run_root)])
    for br in (root, b):
        tip = rig.state(br, max(e.tick for e in rig.stored() if e.branchId == br)).state
        assert not [r for r in tip.runs if r.status.root == "running"]
    gaps = [e for e in rig.stored() if e.type == "recording_gap_started"]
    assert [g.branchId for g in gaps] == [root]  # the gap goes on the active branch only


def test_several_open_runs_close_in_start_order():
    rig, s = started((("c1", "a"), ("c2", "b")))
    r1 = open_run(rig, s)
    mark = len(s.events)
    r2 = s.run("c2")
    rig.outcomes(s.events[mark:])
    recovered(rig)
    finished = [e.payload.runId for e in rig.stored() if e.type == "run_finished"]
    assert finished == [r1, r2]


def test_the_start_up_gap_closes_at_the_next_snapshot_not_at_a_plain_edit():  # correction (ii)
    rig, s = started()
    recovered(rig)
    gap_id = rig.stored()[-1].payload.gapId
    rig.outcomes([s.edit("c1", "x = 2")])
    tip = rig.state(rig.root, rig.stored()[-1].tick).state
    assert [(g.gapId, g.endedTick) for g in tip.gaps] == [(gap_id, None)]  # still open after an edit
    rig.outcomes([s.snapshot([("c1", "x = 2")])])
    stored = rig.stored()
    assert [e.type for e in stored[-2:]] == ["recording_gap_ended", "notebook_snapshot"]
    tip = rig.state(rig.root, stored[-1].tick).state
    assert all(g.endedTick is not None for g in tip.gaps)
    assert not recovered(rig).problems


def test_a_missing_blob_is_reported_with_its_tick_and_recovery_still_completes():
    rig, s = started()
    run = open_run(rig, s)
    big = s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": 0, "item": {
        "outputType": "stream", "mimeTypes": ["text/plain"], "contentHash": "sha256:" + "ab" * 32,
        "preview": "pre", "truncated": True, "sizeBytes": 99999}}, revs=s._current_revisions())
    # a crash can leave an event whose blob is gone: plant it past the service's own checks
    rig.events.append(rig.exp, [{**__import__("blaym_jupyter.tests.service.helpers", fromlist=["recorder"]).recorder(big),
                                  "branchId": rig.root}])
    report = recovered(rig, restart=True)
    assert report.missing_blobs == [(rig.exp, big_tick(rig), "sha256:" + "ab" * 32)]
    assert report.runs_closed == [(rig.exp, rig.root, run)]  # recovery went on to close the run


def big_tick(rig):
    return next(e.tick for e in rig.stored() if e.type == "output_added")


def test_blob_files_nobody_references_are_reported_not_deleted():
    rig, _ = started()
    stored = rig.blobs.put(rig.exp, b"x" * 50)
    report = recovered(rig)
    assert (rig.exp, stored.contentHash) in report.orphan_blobs
    assert rig.blobs.exists(rig.exp, stored.contentHash)


def test_a_history_with_a_hole_is_reported_and_left_alone():  # (e)
    rig, s = started()
    rig.outcomes([s.edit("c1", "x = 2"), s.edit("c1", "x = 3")])
    del rig.events._events[rig.exp][2]
    tip, count = rig.events.tip_tick(rig.exp), len(rig.stored())
    report = recovered(rig, restart=True)
    assert len(report.problems) == 1 and rig.exp in report.problems[0]
    assert rig.events.tip_tick(rig.exp) == tip and len(rig.stored()) == count
    assert not report.gaps_written


def test_a_hole_in_one_history_does_not_stop_the_others():
    rig, s = started()
    other = Scenario()
    other.start(cells=[("c1", "y")])
    rig2 = rig  # same stores: a second exploration id
    ev = other.events[0].model_copy(update={"explorationId": "exploration-two", "eventId": "00000000-0000-4000-8000-000000000002"})
    rig2.outcomes([ev], exp="exploration-two")
    rig.outcomes([s.edit("c1", "x = 2")])
    del rig.events._events[rig.exp][1]
    report = recovered(rig, restart=True)
    assert len(report.problems) == 1
    assert [g[0] for g in report.gaps_written] == ["exploration-two"]


def test_cached_state_is_right_after_recovery_and_the_next_ingest_works():
    rig, s = started()
    out = create(rig, rig.root, 1, "b")
    b = out.branch.branchId
    open_run(rig, s, branch=b)
    recovered(rig)
    for br in (rig.root, b):
        last = max(e.tick for e in rig.stored() if e.branchId == br)
        got = rig.state(br, last)
        assert isinstance(got, RebuildOk)
        assert got.state == reference_rebuild(rig.stored(), br, last).state
    assert rig.events.tip_tick(rig.exp) == rig.service._exps[rig.exp].tip_tick
    set_active(rig, b)
    assert rig.outcomes([s.edit("c1", "x = 5")], target=b)[0].status == "accepted"


def test_recovery_notifications_come_in_tick_order():
    rig, s = started()
    out = create(rig, rig.root, 1, "b")
    open_run(rig, s, branch=out.branch.branchId)
    open_run(rig, s, branch=rig.root)
    seen = []
    rig.service.subscribe(rig.exp, lambda adv: seen.append((adv.tick, adv.branchId, adv.runsEnded)))
    recovered(rig)
    ticks = [t for t, _, _ in seen]
    assert ticks == sorted(ticks) and len(ticks) == 3 and ticks[0] == rig.stored()[-3].tick
    assert sum(1 for _, _, ended in seen if ended) == 2


def test_a_rerun_after_a_half_finished_recovery_closes_the_runs_without_a_second_gap():
    # the crash landed after the gap commit and before the run_finished batch: same state as this
    rig, s = started()
    recovered(rig)  # gap only
    open_run(rig, s)
    report = recovered(rig, restart=True)
    assert report.gaps_written == [] and len(report.runs_closed) == 1
    assert sum(1 for e in rig.stored() if e.type == "recording_gap_started") == 1


def test_a_history_ending_in_a_branch_point_with_no_snapshot_on_the_child_recovers():
    rig, s = started()
    out = create(rig, rig.root, 1, "child")
    child = out.branch.branchId
    report = recovered(rig, restart=True)
    assert not report.problems and not report.runs_closed
    assert rig.outcomes([s.edit("c1", "x = 9")], target=child)[0].status == "accepted"


def test_an_open_unknown_gap_is_not_a_start_up_gap_so_recovery_adds_one_next_to_it():
    # pins today's behaviour: the dedupe only recognises reason server_unavailable
    from blaym_jupyter.service.core import _open_service_gaps
    rig, s = started()
    rig.service._open_gap(rig.service._exp(rig.exp), rig.exp, rig.root)  # what a refused event leaves
    recovered(rig)
    tip = rig.state(rig.root, rig.stored()[-1].tick).state
    assert sorted(g.reason.value for g in tip.gaps if g.endedTick is None) == ["server_unavailable", "unknown"]
    rig.outcomes([s.snapshot([("c1", "x = 1")])])  # one snapshot closes both
    tip = rig.state(rig.root, rig.stored()[-1].tick).state
    assert not _open_service_gaps(tip)


def test_restarts_with_nothing_recorded_between_them_add_one_gap_but_activity_between_adds_another():
    rig, s = started()
    for _ in range(3):
        recovered(rig, restart=True)
    assert sum(1 for e in rig.stored() if e.type == "recording_gap_started") == 1
    out = rig.outcomes([s.snapshot([("c1", "x = 1")])])  # recording resumed: gap closed
    assert out[0].status == "accepted", out
    recovered(rig, restart=True)  # a viewer-only process start
    assert sum(1 for e in rig.stored() if e.type == "recording_gap_started") == 2

"""Recovery rules, on the in-memory ports with a manual clock."""

from __future__ import annotations


from blaym_notebook.tests.history.scenario import EXPLORATION, Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, recorder


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
    from blaym_notebook.service.auth import FixedTokenAuthorizer
    from blaym_notebook.service.core import Service
    from blaym_notebook.testing.fakes import IdFactory
    rig.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory(f"again{next(_restarts)}"))


def recovered(rig, restart=False):
    if restart:
        reopen(rig)
    rig.service.recover()
    return rig.service.recovery_report


def test_a_clean_history_gets_one_start_up_gap_on_the_active_branch():  # rule (c)
    rig, _ = started()
    report = recovered(rig)
    gap = rig.stored()[-1]
    assert gap.type == "recording_gap_started" and gap.branchId == rig.root
    assert (gap.payload.source.value, gap.payload.reason.value) == ("service", "server_unavailable")
    assert (gap.originId, gap.authorHint.value, gap.authorConfidence.value) == ("blaym-service", "blaym", "known")
    assert report.gaps_written == [(rig.exp, rig.root, gap.payload.gapId)] and not report.runs_closed


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


def test_a_history_with_a_hole_is_reported_and_left_alone():  # (e)
    rig, s = started()
    rig.outcomes([s.edit("c1", "x = 2"), s.edit("c1", "x = 3")])
    del rig.events._events[rig.exp][2]
    tip, count = rig.events.tip_tick(rig.exp), len(rig.stored())
    report = recovered(rig, restart=True)
    assert len(report.problems) == 1 and rig.exp in report.problems[0]
    assert rig.events.tip_tick(rig.exp) == tip and len(rig.stored()) == count
    assert not report.gaps_written


def test_opening_the_service_clears_staging_debris_and_lists_an_orphan_blob_without_deleting_it(tmp_path):
    from blaym_notebook.local import open_service
    from blaym_notebook.service.auth import FixedTokenAuthorizer
    svc = open_service(tmp_path, FixedTokenAuthorizer(TOKEN))
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    svc.ingest(TOKEN, {"explorationId": EXPLORATION, "editor": "jupyterlab", "instanceId": "tab-1",
                       "events": [recorder(e) for e in s.events]})
    stored = svc._blobs.put(EXPLORATION, b"x" * 50)  # a blob file no event refers to
    svc.close()
    debris = tmp_path / "explorations" / EXPLORATION / "staging" / "op1"
    debris.mkdir(parents=True)
    (debris / "half").write_bytes(b"partial")
    svc = open_service(tmp_path, FixedTokenAuthorizer(TOKEN))
    try:
        report = svc.recovery_report
        assert report.staging_removed >= 1 and not debris.exists()
        assert (EXPLORATION, stored.contentHash) in report.orphan_blobs
        assert svc._blobs.exists(EXPLORATION, stored.contentHash)
    finally:
        svc.close()

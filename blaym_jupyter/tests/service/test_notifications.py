from __future__ import annotations

from blaym_jupyter_types import RebuildOk
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import Rig


def build():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    r = s.run("c1")
    s.finish(r)
    return s


def test_callbacks_run_after_commit_in_tick_order_and_see_only_committed_ticks():
    rig, s = Rig(), build()
    seen = []

    def cb(adv):
        seen.append((adv.tick, rig.events.tip_tick(rig.exp), type(rig.state(adv.branchId, adv.tick)) is RebuildOk))

    rig.outcomes(s.events[:1])
    rig.service.subscribe(rig.exp, cb)
    rig.outcomes(s.events)
    assert [t for t, _, _ in seen] == [2, 3, 4]
    assert all(tip >= t and ok for t, tip, ok in seen)


def test_raising_callback_does_not_break_ingest_and_is_collected():
    rig, s = Rig(), build()
    got = []

    def boom(adv):
        raise RuntimeError("subscriber bug")

    rig.service.subscribe(rig.exp, boom)
    rig.service.subscribe(rig.exp, got.append)
    outs = rig.outcomes(s.events)
    assert [o.status for o in outs] == ["accepted"] * 4
    assert len(rig.stored()) == 4 and len(got) == 4
    assert len(rig.service.notifier.errors) == 4


def test_refused_and_duplicate_events_send_nothing_service_gap_does():
    rig, s = Rig(), build()
    rig.outcomes(s.events)
    got = []
    rig.service.subscribe(rig.exp, got.append)
    rig.outcomes(s.events)
    assert got == []
    s.edit("c1", "z", revision=9)
    rig.outcomes(s.events[-1:])
    assert [a.tick for a in got] == [5]  # the service's own gap event, not the refused one


def test_notices_carry_branch_and_runs():
    rig, s = Rig(), build()
    got = []
    rig.outcomes(s.events[:1])
    rig.service.subscribe(rig.exp, got.append)
    rig.outcomes(s.events)
    assert all(a.branchId == rig.root and a.explorationId == rig.exp for a in got)
    assert got[1].runStarted == "run-1" and got[2].runsEnded == ["run-1"]

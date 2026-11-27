from __future__ import annotations

from blaym_notebook_types import RebuildOk
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import Rig


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







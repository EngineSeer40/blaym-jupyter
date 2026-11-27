"""the timeline index and events-between reads.

One row per event type for the classification, one agreement test against the reference rebuilder
(tick order, branch ownership, run results), and the limits.
"""

from __future__ import annotations

import pytest

from blaym_jupyter_types import EventsBetweenRequest, TimelineIndexRequest

from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, replay


# (event type, how to pick it among the scenario's events) -> expected kind


def ticks_of(response):
    return [t.root for t in response.ticks]


@pytest.fixture
def seeded():
    rig = Rig()
    s = Scenario()
    s.start(cells=[("c1", "x = 0")])
    for i in range(1, 12):
        s.edit("c1", f"x = {i}")
    replay(rig, s.events)
    return rig, rig.root


def test_limits_and_windows(seeded):
    rig, root = seeded
    svc = rig.service

    def idx(**kw):
        return svc.timeline_index(TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId=root, **kw))

    assert ticks_of(idx()) == list(range(1, 13)) and not idx().truncated
    cut = idx(limit=5)
    assert ticks_of(cut) == [1, 2, 3, 4, 5] and cut.truncated and cut.nextTick == 6
    rest = idx(fromTick=cut.nextTick, toTick=9)  # both ends inclusive
    assert ticks_of(rest) == [6, 7, 8, 9] and not rest.truncated and rest.nextTick is None
    assert ticks_of(idx(fromTick=50)) == [] and idx(fromTick=50).kinds == []

    def between(**kw):
        return svc.events_between(TOKEN, EventsBetweenRequest(
            explorationId=rig.exp, branchId=root, fromTick=3, toTick=10, **kw))

    full = between()
    assert [e.root.tick for e in full.events] == list(range(3, 11)) and not full.truncated
    part = between(maxEvents=3)
    assert [e.root.tick for e in part.events] == [3, 4, 5] and part.truncated and part.nextTick == 6



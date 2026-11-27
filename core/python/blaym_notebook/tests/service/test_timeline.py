"""the timeline index and events-between reads.

One row per event type for the classification, one agreement test against the reference rebuilder
(tick order, branch ownership, run results), and the limits.
"""

from __future__ import annotations

import pytest

from blaym_notebook_types import EventsBetweenRequest, TimelineIndexRequest

from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, replay


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




def test_cell_positions_fold_across_snapshot_insert_move_delete():
    rig = Rig()
    s = Scenario()
    s.start(cells=[("a", "1"), ("b", "2")])  # tick 1: [a, b]
    s.insert("c", "3", index=1)  # 2: [a, c, b], c at 1
    s.edit("b", "22")  # 3: b at 2
    s.move("b", 2, 0)  # 4: [b, a, c], b at 0
    run = s.run("a")  # 5: no cell position
    s.delete("c")  # 6 (after run's end events): c was at 2, then 2 cells
    replay(rig, s.events)
    got = rig.service.timeline_index(TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId=rig.root))
    def pos(r):
        return list(zip([None if i is None else i.root for i in r.cellIndexes], [n.root for n in r.cellCounts]))

    at = dict(zip(ticks_of(got), pos(got)))
    assert [at[t] for t in (1, 2, 3, 4)] == [(None, 2), (1, 3), (2, 3), (0, 3)]
    assert at[5] == (None, 3) and at[6] == (2, 2)
    windowed = rig.service.timeline_index(
        TOKEN, TimelineIndexRequest(explorationId=rig.exp, branchId=rig.root, fromTick=3, toTick=4))
    assert pos(windowed) == [(2, 3), (0, 3)]

"""The SQLite-backed service through open_service, against the in-memory one (S4, Task 7)."""

from __future__ import annotations

import random

from blaym_jupyter_types import OutlineRequest, SetActiveBranchRequest, StateAtRequest

from blaym_jupyter.local import SystemClock, open_service
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create, key, replay
from blaym_jupyter.tests.service.test_branches import run_generated
from blaym_jupyter.testing.fakes import IdFactory
from blaym_jupyter.tests.history.generate import histories
from hypothesis import given, settings, strategies as st, HealthCheck


class SqliteRig(Rig):
    def __init__(self, root, **kw):
        super().__init__()
        self.service.close()
        self.root_dir = root
        self.service = open_service(root, FixedTokenAuthorizer(TOKEN), clock=self.clock, new_id=IdFactory("svc"), **kw)
        self.events = self.service._events


def scenario():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    s.branch_from(2, "a")
    s.edit("c1", "xa")
    s.on("main").branch_from(2, "b")
    s.edit("c1", "xb")
    s.on("a").edit("c1", "xa2")
    return s


def test_sqlite_service_matches_memory_through_a_scenario_and_keeps_the_active_branch(tmp_path):
    s = scenario()
    mem, sql = Rig(), SqliteRig(tmp_path)
    ids = {}
    for rig in (mem, sql):
        ids[id(rig)] = replay(rig, s.events, rnd=random.Random(5))
    assert [key(e, {}) for e in mem.stored()] == [key(e, {}) for e in sql.stored()]
    assert mem.root == sql.root
    a = ids[id(sql)]["a"]
    sql.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=sql.exp, branchId=a))
    sql.service.close()
    again = open_service(tmp_path, FixedTokenAuthorizer(TOKEN), clock=sql.clock, recover=False)  # recovery would add its own events
    try:
        assert again._events.exploration(sql.exp).active_branch_id == a
        req = OutlineRequest(explorationId=sql.exp)
        assert again.outline(TOKEN, req) == mem.service.outline(TOKEN, req)
        last = mem.stored()[-1].tick
        assert again.state_at(TOKEN, StateAtRequest(explorationId=sql.exp, branchId=a, tick=last)).status == "ok"
    finally:
        again.close()


def test_system_clock_moves():
    c = SystemClock()
    t = c.now_ms()
    c.sleep(0.01)
    assert c.now_ms() >= t


@settings(max_examples=6, suppress_health_check=list(HealthCheck))
@given(h=histories(shapes=("nested", "siblings", "mixed"), max_events=25), seed=st.integers(0, 10**6))
def test_a_small_set_of_generated_histories_on_sqlite_equals_memory(h, seed, tmp_path_factory):
    root = tmp_path_factory.mktemp("sql")
    sql = SqliteRig(root, interval=5)
    mem = Rig(interval=5)
    try:
        i1 = run_generated(mem, h, random.Random(seed))
        i2 = run_generated(sql, h, random.Random(seed))
        assert [key(e, i1) for e in mem.stored()] == [key(e, i2) for e in sql.stored()]
        assert mem.root == sql.root
    finally:
        sql.service.close()

"""The SQLite-backed service through open_service, against the in-memory one."""

from __future__ import annotations

import random

from blaym_notebook_types import OutlineRequest, SetActiveBranchRequest, StateAtRequest

from blaym_notebook.local import open_service
from blaym_notebook.service.auth import FixedTokenAuthorizer
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, key, replay
from blaym_notebook.testing.fakes import IdFactory


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



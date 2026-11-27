"""The service's notification stream as fixtures for the navigator.

Each named history is driven through a real `Service` (memory ports, fake clock, fixed ids).
`fixtures/service/<name>.json` holds `{name, source, events, notices, outline, initialOutline,
eventsSince}`: the stored events, the `HistoryAdvanced` notices in the order published, the
outline built from the stored events, the outline of tick 1 only, and an `eventsSince`
example `{request, response}`. The TypeScript spec feeds them to the navigator.
Generated, never edited by hand. Regenerate with

    python -m blaym_notebook.tests.service.test_notification_fixtures --write
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


from blaym_notebook.history import outline as build_outline
from blaym_notebook_types import BlaymEvent, EventsSinceRequest, SetActiveBranchRequest
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, create

FIXTURE_DIR = Path(__file__).resolve().parents[5] / "fixtures" / "service"


def dump(model):
    return model.model_dump(mode="json", exclude_none=True)


class Driver:
    """A Scenario used as the event factory; the Service allocates the real branches and ticks."""

    def __init__(self):
        self.rig, self.s, self.ids, self.sent = Rig(), Scenario(), {}, 0
        self.notices: list = []
        self.rig.service.subscribe(self.rig.exp, self.notices.append)

    def feed(self, use_active=False, ok=True):
        """Send the scenario's new events. A branch point is a create_branch call."""
        for ev in self.s.events[self.sent:]:
            if ev.type == "branch_point_created":
                out = create(self.rig, self.ids[ev.payload.parentBranchId], ev.payload.parentTick, ev.payload.name)
                assert out.status == "ok", out
                self.ids[ev.branchId] = out.branch.branchId
                continue
            target = None if (use_active or not self.ids) else self.ids[ev.branchId]
            o = self.rig.outcomes([ev], target=target)[0]
            assert (o.status == "accepted") == ok, o
            if not self.ids:
                self.ids[ev.branchId] = o.branchId
        self.sent = len(self.s.events)

    def activate(self, name):
        self.rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(
            explorationId=self.rig.exp, branchId=self.ids[name]))


HISTORIES: dict = {}


def history(name, source):
    def register(fn):
        HISTORIES[name] = (source, fn)
        return fn
    return register


@history("one_branch_edits_and_a_run", "plain recording: edits, a run with outputs")
def one_branch(d):
    s = d.s
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    r = s.run("c1")
    s.output(r, "2")
    s.edit("c1", "x = 3")   # edited while it runs
    s.finish(r)
    s.edit("c1", "x = 4")
    d.feed()
    return "main", 3


@history("branch_work_return_work", "create a branch at a settled moment, work, set active back, work on the original")
def branch_and_return(d):
    s = d.s
    s.start(cells=[("c1", "m0")])
    s.edit("c1", "m1")
    s.edit("c1", "m2")
    d.feed()
    b = s.branch_from(tick=2, name="try")
    s.edit("c1", "b1")
    s.edit("c1", "b2")
    d.feed()
    d.activate("main")
    s.on("main").edit("c1", "m3")
    d.feed(use_active=True)
    return b, 3


@history("two_children_worked_alternately", "two children of one parent, worked alternately")
def two_children(d):
    s = d.s
    s.start(cells=[("c1", "m0")])
    s.edit("c1", "m1")
    x = s.branch_from(tick=2, name="X")
    y = s.branch_from(tick=2, name="Y", parent="main")
    for i in range(2):
        s.on(x).edit("c1", f"x{i}")
        s.on(y).edit("c1", f"y{i}")
        s.on("main").edit("c1", f"m-after{i}")
    d.feed()
    return x, 4


@history("run_finished_by_a_kernel_restart_on_a_branch", "a run on a branch ended by kernel_restarted")
def run_restart(d):
    s = d.s
    s.start(cells=[("c1", "m0")])
    s.edit("c1", "m1")
    b = s.branch_from(tick=2, name="try")
    s.edit("c1", "b1")
    r1 = s.run("c1")
    s.output(r1, "partial")
    r2 = s.run("c1")
    s.kernel_restart()
    s.edit("c1", "b2")
    d.feed()
    return b, 5


@history("branch_from_a_child_on_the_shared_parent_part", "a branch created from a child at a tick that belongs to its parent")
def child_of_child(d):
    s = d.s
    s.start(cells=[("c1", "m0")])
    s.edit("c1", "m1")
    s.edit("c1", "m2")
    x = s.branch_from(tick=2, name="X")
    s.edit("c1", "x1")
    s.on("main").edit("c1", "m3")
    z = s.branch_from(tick=2, name="Z", parent=x)   # tick 2 is on main's part of X's line
    s.edit("c1", "z1")
    d.feed()
    return z, 2


@history("refused_event_gap_then_snapshot", "an invalid event is refused (the service records a gap), then a snapshot closes it")
def refused_gap(d):
    s = d.s
    s.start(cells=[("c1", "m0")])
    s.edit("c1", "m1")
    d.feed()
    s.edit("c1", "never", revision=9)
    d.feed(ok=False)
    s.snapshot([("c1", "m-recovered")])
    d.feed()
    s.edit("c1", "m2")
    d.feed()
    return "main", 1


def build(name):
    source, fn = HISTORIES[name]
    d = Driver()
    branch, since = fn(d)
    branch = d.ids.get(branch, branch)
    rig = d.rig
    stored = rig.stored()
    resp = rig.service.events_since(TOKEN, EventsSinceRequest(explorationId=rig.exp, branchId=branch, afterTick=since))
    return {
        "name": name,
        "source": source,
        "events": [dump(BlaymEvent(root=e)) for e in stored],
        "notices": [dump(n) for n in d.notices],
        "outline": dump(build_outline(stored)),
        "initialOutline": dump(build_outline(stored[:1])),
        "eventsSince": {
            "request": {"explorationId": rig.exp, "branchId": branch, "afterTick": since},
            "response": dump(resp),
        },
    }


def render(fixture) -> str:
    return json.dumps(fixture, indent=2, sort_keys=True) + "\n"


def write_all():
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name in HISTORIES:
        (FIXTURE_DIR / f"{name}.json").write_text(render(build(name)))


# --- an independent model of the navigator's contract ---------------------------------


def apply_notice(outline, notice):
    """The rules in the navigator's contract, written without the service: only tick + 1 is
    applied (older ones are ignored), the notified branch gets the tick, a run start opens a
    span, run ends close the named open spans at that tick, a new branch is added."""
    o = copy.deepcopy(outline)
    if notice["tick"] <= o["highestTick"]:
        return o
    assert notice["tick"] == o["highestTick"] + 1, "a gap: not applicable"
    branch = next((b for b in o["branches"] if b["branchId"] == notice["branchId"]), None)
    if branch is None:
        branch = copy.deepcopy(notice["newBranch"])
        o["branches"].append(branch)
    if notice["tick"] not in branch["ownTicks"]:
        branch["ownTicks"].append(notice["tick"])
        branch["ownTicks"].sort()
    for span in branch["runSpans"]:
        if "endedTick" not in span and span["runId"] in notice.get("runsEnded", []):
            span["endedTick"] = notice["tick"]
    run = notice.get("runStarted")
    if run is not None and all(sp["runId"] != run for sp in branch["runSpans"]):
        branch["runSpans"].append({"runId": run, "startedTick": notice["tick"]})
    o["highestTick"] = notice["tick"]
    return o


def normal(outline):
    o = copy.deepcopy(outline)
    o["branches"] = sorted(o["branches"], key=lambda b: b["branchId"])
    return o


def load(name):
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def test_fixture_files_are_current():
    for name in HISTORIES:
        path = FIXTURE_DIR / f"{name}.json"
        assert path.exists(), f"missing {path}: run --write"
        assert path.read_text() == render(build(name)), f"{name}.json is stale: run --write"


def test_notices_applied_by_the_contract_rules_give_the_stored_outline():
    for name in HISTORIES:
        f = load(name)
        o = f["initialOutline"]
        for n in f["notices"]:
            o = apply_notice(o, n)
        assert normal(o) == normal(f["outline"]), name
        since = f["eventsSince"]["request"]["afterTick"]
        ticks = [e["tick"] for e in f["eventsSince"]["response"]["events"]]
        assert ticks == sorted(ticks) and all(t > since for t in ticks)


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_all()
        print(f"wrote {len(HISTORIES)} fixtures to {FIXTURE_DIR}")
    else:
        print("usage: python -m blaym_notebook.tests.service.test_notification_fixtures --write")

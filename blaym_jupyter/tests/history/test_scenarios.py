"""Named scenarios from the original Blaym's failures (S3a, Task 7).

Each scenario is a hand-written, readable history built with `Scenario`. A comment on
each names the original cases it comes from (the catalogues are in
`.ai/contexts/s3a-original-*.md`: Dup n, B n, S n, P n and the named final scenarios).
Every `expect_at` is checked against the reference rebuilder AND the real `Rebuilder`
(small snapshot interval, one warm instance per scenario, fed as events arrive), which
must agree exactly; the expectation is also recorded.

Fixtures: every scenario writes `fixtures/rebuild/<name>.json` with
`{name, source, events, expectations: [{branchId, tick, result}], outline}`, where `events`
are the events as delivered (duplicates included, dropped ones left out) and `result`
is the answer at that moment. `outline` is the history outline of everything delivered. Task 8 validates the files in both languages. The files
are generated, never edited by hand. Regenerate them after changing a scenario with

    python -m blaym_jupyter.tests.history.test_scenarios --write

`test_fixture_files_are_current` fails when a file is stale or missing, and
`test_fixture_events_reproduce_their_expectations` replays each file from scratch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blaym_jupyter.history import Rebuilder, outline, reference_rebuild
from blaym_jupyter.tests.history.scenario import Scenario

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rebuild"
INTERVAL = 3  # small, so snapshots exist and boundaries are crossed in short scenarios


def dump(result):
    # null fields are left out: the shared schema has optional fields, never nulls
    return result.model_dump(mode="json", exclude_none=True)


class Recorded(Scenario):
    """A Scenario whose answers are checked against both rebuilders and recorded."""

    def __init__(self, name: str, source: str):
        super().__init__()
        self.name, self.source = name, source
        self.arrivals: list = []  # events as delivered (duplicates in, dropped out)
        self.expectations: list[dict] = []
        self._rb: Rebuilder | None = None
        self._fed = 0

    def _emit(self, *args, **kwargs):
        event = super()._emit(*args, **kwargs)
        self.arrivals.append(event)
        return event

    def redeliver(self, *events):
        """The same events arrive again."""
        self.arrivals.extend(events)

    def drop(self, *ticks):
        """These events never arrive."""
        self.arrivals = [e for e in self.arrivals if e.tick not in ticks]
        self._rb, self._fed = None, 0

    def rebuilder(self) -> Rebuilder:
        if self._rb is None:
            self._rb, self._fed = Rebuilder(interval=INTERVAL), 0
        self._rb.add(self.arrivals[self._fed:])
        self._fed = len(self.arrivals)
        return self._rb

    def rebuild(self, branch, tick):
        want = reference_rebuild(self.arrivals, branch, tick)
        got = self.rebuilder().state_at(branch, tick)
        assert dump(got) == dump(want), f"{self.name}: {branch}@{tick}: real != reference"
        return want

    def expect_at(self, branch, tick, **kwargs):
        result = super().expect_at(branch, tick, **kwargs)
        self.expectations.append({"branchId": branch, "tick": tick, "result": dump(result)})
        return result

    def fixture(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "events": [e.model_dump(mode="json", exclude_none=True) for e in self.arrivals],
            "expectations": self.expectations,
            "outline": dump(outline(self.arrivals)),
        }


SCENARIOS: dict = {}


def scenario(name: str, source: str):
    def register(fn):
        SCENARIOS[name] = (source, fn)
        return fn

    return register


def build(name: str) -> Recorded:
    source, fn = SCENARIOS[name]
    s = Recorded(name, source)
    fn(s)
    return s


def _linear(s, n, cell="c1", prefix="m"):
    for i in range(1, n + 1):
        s.edit(cell, f"{prefix}{i}")


# --- branching and returning ------------------------------------------------------


@scenario("create_branch_work_jump_return",
          "slices S3b rule 5; Dup 1, Dup 2, Dup 5, B5, B8 (stale state after switch and return)")
def create_branch_work_jump_return(s):
    s.start(cells=[("c1", "m0")])                      # 1
    s.edit("c1", "m1")                                 # 2
    s.edit("c1", "m2")                                 # 3
    b = s.branch_from(tick=2, name="try")              # 4  branch from main@2
    _linear(s, 3, prefix="b")                          # 5,6,7
    s.on("main").edit("c1", "m3")                      # 8
    s.expect_at(b, 7, cells=[("c1", "b3")])
    s.expect_at(b, 5, cells=[("c1", "b1")])            # jump around on the branch
    s.expect_at(b, 6, cells=[("c1", "b2")])
    s.expect_at(b, 3, cells=[("c1", "m1")], position=(b, 3, 2, 2))   # before the branch existed
    s.expect_at(b, 7, cells=[("c1", "b3")])
    s.expect_at("main", 8, cells=[("c1", "m3")])       # return to the parent's latest: fully caught up
    s.expect_at("main", 3, cells=[("c1", "m2")])
    s.expect_at("main", 8, cells=[("c1", "m3")])


@scenario("child_shows_parent_at_the_branch_point",
          "B10, B11, S16, final scenario first-branch-switch-preselects-child-online")
def child_shows_parent_at_the_branch_point(s):
    s.start(cells=[("c1", "m0"), ("c2", "keep")])     # 1
    s.edit("c1", "m1")                                 # 2
    c = s.branch_from(tick=2, name="child")            # 3
    s.on("main")
    _linear(s, 3, prefix="later")                      # 4,5,6  parent keeps working
    s.on(c).edit("c1", "child1")                       # 7
    s.expect_at(c, 3, cells=[("c1", "m1"), ("c2", "keep")], position=(c, 3, 3, 3))
    s.expect_at(c, 7, cells=[("c1", "child1"), ("c2", "keep")])
    s.expect_at("main", 6, cells=[("c1", "later3"), ("c2", "keep")])
    for tick in (3, 7):  # the child never shows the parent's later edits
        assert "later" not in s.state_at(c, tick).notebook.cells[0].code


@scenario("grandchild_of_a_grandchild_with_cells_on_the_root_only",
          "B11 (multi-level lineage; files created on the root only)")
def grandchild_of_a_grandchild(s):
    s.start(cells=[("c1", "root")])                    # 1
    s.insert("c2", "made-on-root")                     # 2
    a = s.branch_from(tick=2, name="A")                # 3
    s.edit("c1", "a1")                                 # 4
    bp_b = s.branch_from(tick=4, name="B", parent=a)   # 5
    s.edit("c1", "b1")                                 # 6
    c = s.branch_from(tick=6, name="C", parent=bp_b)   # 7
    s.edit("c2", "c-edit")                             # 8
    s.on("main").insert("c3", "root-later")            # 9 on the root only, after everything
    s.expect_at(c, 8, cells=[("c1", "b1"), ("c2", "c-edit")])
    s.expect_at(c, 7, cells=[("c1", "b1"), ("c2", "made-on-root")])
    s.expect_at(bp_b, 6, cells=[("c1", "b1"), ("c2", "made-on-root")])
    s.expect_at(a, 4, cells=[("c1", "a1"), ("c2", "made-on-root")])
    s.expect_at("main", 9, cells=[("c1", "root"), ("c2", "made-on-root"), ("c3", "root-later")])
    for branch in (a, bp_b, c):
        assert "c3" not in [x.cellId for x in s.state_at(branch, 9).notebook.cells]


@scenario("two_children_of_one_parent_worked_alternately",
          "online-general-06 (isolation); Dup 26")
def two_children_alternately(s):
    s.start(cells=[("c1", "m0")])                      # 1
    s.edit("c1", "m1")                                 # 2
    x = s.branch_from(tick=2, name="X")                # 3
    y = s.branch_from(tick=2, name="Y", parent="main") # 4
    for i in range(3):
        s.on(x).edit("c1", f"x{i}")
        s.on(y).edit("c1", f"y{i}")
        s.on("main").edit("c1", f"m-after{i}")
    s.on(x).chat("q", "only on X")
    last = s.tick
    s.expect_at(x, last, cells=[("c1", "x2")], chat=[("q", "only on X")])
    s.expect_at(y, last, cells=[("c1", "y2")], chat=[])
    s.expect_at("main", last, cells=[("c1", "m-after2")], chat=[])
    s.expect_at(y, 4, cells=[("c1", "m1")])
    s.expect_at(x, 3, cells=[("c1", "m1")])


@scenario("jump_back_before_the_branch_existed_then_forward",
          "slices S3b rule 2; Dup 3 (seek back past a branch start)")
def jump_back_before_the_branch_existed(s):
    s.start(cells=[("c1", "m0")])                      # 1
    _linear(s, 3)                                      # 2,3,4
    b = s.branch_from(tick=3, name="B")                # 5
    s.edit("c1", "b1")                                 # 6
    s.edit("c1", "b2")                                 # 7
    s.expect_at(b, 7, cells=[("c1", "b2")])
    s.expect_at(b, 2, cells=[("c1", "m1")], position=(b, 2, 2, 2))   # still on B, shows shared history
    s.expect_at(b, 4, cells=[("c1", "m2")], position=(b, 4, 3, 3))
    s.expect_at(b, 6, cells=[("c1", "b1")])            # forward leads into B's changes, not the parent's
    s.expect_at(b, 7, cells=[("c1", "b2")])
    s.expect_at("main", 4, cells=[("c1", "m3")])


@scenario("jump_to_and_around_a_snapshot_boundary",
          "Dup 7, S29 (one stated boundary rule); snapshots every 3 own events")
def jump_around_a_snapshot_boundary(s):
    s.start(cells=[("c1", "m0")])                      # 1
    _linear(s, 9)                                      # 2..10
    b = s.branch_from(tick=5, name="B")                # 11 (B's own event 1)
    _linear(s, 6, prefix="b")                          # 12..17 (B's own events 2..7)
    for tick in (2, 3, 4, 5, 6, 7, 9, 10):             # main: snapshots at own events 3, 6, 9
        s.expect_at("main", tick, cells=[("c1", f"m{tick - 1}")])
    for tick in (13, 14, 15, 16, 17):                  # B: own events 3 and 6 are ticks 13 and 16
        s.expect_at(b, tick, cells=[("c1", f"b{tick - 11}")])
    rb = s.rebuilder()
    # the stated rule: a snapshot at tick k is the state after the event at tick k
    rb.state_at("main", 6)
    assert rb.last_base == ("main", 6)
    rb.state_at("main", 7)
    assert rb.last_base == ("main", 6)
    rb.state_at("main", 5)
    assert rb.last_base == ("main", 3)
    rb.state_at(b, 13)
    assert rb.last_base == ("B", 13)


@scenario("same_event_delivered_twice_after_jumping_back",
          "Dup 16, Dup 19 (a repeat after a seek must not rewind or duplicate)")
def same_event_twice_after_jumping_back(s):
    s.start(cells=[("c1", "m0")])                      # 1
    _linear(s, 5)                                      # 2..6
    s.chat("q", "hello")                               # 7
    s.expect_at("main", 7, cells=[("c1", "m5")], chat=[("q", "hello")])
    s.expect_at("main", 3, cells=[("c1", "m2")], chat=[])   # jump back ...
    s.redeliver(s.events[6], s.events[3], s.events[0])      # ... then repeats arrive
    s.expect_at("main", 3, cells=[("c1", "m2")], chat=[])
    s.expect_at("main", 7, cells=[("c1", "m5")], chat=[("q", "hello")])   # chat is not doubled
    assert len(s.state_at("main", 7).chat) == 1


@scenario("branch_point_at_the_very_first_event",
          "slices S3b rule 2; B10 (branch before any edit)")
def branch_point_at_first_event(s):
    s.start(cells=[("c1", "m0")])                      # 1
    s.edit("c1", "m1")                                 # 2
    b = s.branch_from(tick=1, name="from-the-start")   # 3
    s.edit("c1", "b1")                                 # 4
    s.expect_at(b, 3, cells=[("c1", "m0")], position=(b, 3, 3, 2))
    s.expect_at(b, 4, cells=[("c1", "b1")])
    s.expect_at("main", 4, cells=[("c1", "m1")])
    s.expect_at(b, 1, cells=[("c1", "m0")], position=(b, 1, 1, 1))


@scenario("branch_point_at_the_parents_latest_event",
          "B5, B8 (branch from the live head, then the parent carries on)")
def branch_point_at_parents_latest(s):
    s.start(cells=[("c1", "m0")])                      # 1
    _linear(s, 3)                                      # 2,3,4
    b = s.branch_from(tick=s.tick, name="from-latest") # 5, parentTick 4 = the parent's last event
    s.edit("c1", "b1")                                 # 6
    s.on("main").edit("c1", "m4")                      # 7
    s.expect_at(b, 5, cells=[("c1", "m3")], position=(b, 5, 5, 5))
    s.expect_at(b, 6, cells=[("c1", "b1")])
    s.expect_at("main", 7, cells=[("c1", "m4")])
    s.expect_at("main", 4, cells=[("c1", "m3")])


# --- runs, kernel, outside changes, chat, gaps ------------------------------------


@scenario("edit_a_cell_while_it_runs",
          "slices S3b rule 6; probe B question 6 (the kernel runs the code as it was sent)")
def edit_a_cell_while_it_runs(s):
    s.start(cells=[("c1", "slow(1)")])                 # 1
    run = s.run("c1")                                  # 2 sent revision 1
    s.edit("c1", "slow(2)")                            # 3 edited during the run: revision 2
    s.output(run, "result of slow(1)")                 # 4
    s.finish(run)                                      # 5
    s.expect_at("main", 3, cells=[("c1", "slow(2)")], runs={run: "running"})
    s.expect_at("main", 5, cells=[("c1", "slow(2)")], runs={run: "ok"},
                outputs={"c1": ["result of slow(1)"]})
    for tick in (2, 3, 5):
        st = s.state_at("main", tick)
        assert [r.cellRevision for r in st.runs] == [1]     # the run keeps the revision sent
    assert s.state_at("main", 5).notebook.cells[0].revision == 2
    s.expect_at("main", 2, cells=[("c1", "slow(1)")], runs={run: "running"})


@scenario("kernel_restart_with_a_run_in_progress",
          "ADR 0005 (a restart leaves a running cell visibly unfinished); P4")
def kernel_restart_with_run_in_progress(s):
    s.start(cells=[("c1", "loop()")])                  # 1
    s.kernel_start()                                   # 2
    run = s.run("c1")                                  # 3
    s.output(run, "partial")                           # 4
    s.kernel_restart(reason="unknown")                 # 5 (a crash looks the same)
    s.edit("c1", "loop(2)")                            # 6
    s.expect_at("main", 4, runs={run: "running"}, kernel_status="running")
    s.expect_at("main", 5, runs={run: "unfinished"}, outputs={"c1": ["partial"]})
    s.expect_at("main", 6, runs={run: "unfinished"})
    st = s.state_at("main", 6)
    assert st.kernel.lastRestart.tick == 5 and st.kernel.lastRestart.reason.value == "unknown"
    assert st.runs[0].endedTick is None


@scenario("outside_change_mid_history_then_more_edits",
          "product brief (changed outside Jupyter); S5 recorder")
def outside_change_mid_history(s):
    s.start(cells=[("c1", "a"), ("c2", "b")])          # 1
    s.edit("c1", "a2")                                 # 2
    s.outside_change(cells=[("c1", "from-claude-code"), ("c3", "new cell")])   # 3
    s.edit("c1", "after-outside")                      # 4
    s.expect_at("main", 2, cells=[("c1", "a2"), ("c2", "b")])
    s.expect_at("main", 3, cells=[("c1", "from-claude-code"), ("c3", "new cell")])
    s.expect_at("main", 4, cells=[("c1", "after-outside"), ("c3", "new cell")])
    assert s.state_at("main", 2).lastOutsideChange is None
    assert s.state_at("main", 4).lastOutsideChange.tick == 3


@scenario("streamed_chat_reply_jumping_between_updates",
          "probe A (a reply grows through updates of one message); Dup 21 style repeats")
def streamed_chat_reply(s):
    s.start(cells=[("c1", "x")])                       # 1
    s.chat("q1", "plot it", sender="user-1")           # 2
    s.chat("a1", "Sure", sender="claude", kind="agent")             # 3
    s.chat_update("a1", "Sure, here")                  # 4
    s.chat_update("a1", "Sure, here is the plot")      # 5
    s.chat("q2", "thanks", sender="user-1")            # 6
    for tick, reply in ((3, "Sure"), (4, "Sure, here"), (5, "Sure, here is the plot")):
        s.expect_at("main", tick, chat=[("q1", "plot it"), ("a1", reply)])
    s.expect_at("main", 6, chat=[("q1", "plot it"), ("a1", "Sure, here is the plot"), ("q2", "thanks")])
    s.expect_at("main", 4, chat=[("q1", "plot it"), ("a1", "Sure, here")])   # back to the middle
    s.expect_at("main", 2, chat=[("q1", "plot it")])


@scenario("recording_gap_then_recovery_snapshot",
          "ADR 0005 (a gap never claims nothing happened); P4")
def gap_then_recovery_snapshot(s):
    s.start(cells=[("c1", "a")])                       # 1
    run = s.run("c1")                                  # 2
    s.chat("q", "hi")                                  # 3
    s.gap_start("g1", source="browser", reason="no_client")      # 4
    s.gap_end("g1")                                    # 5
    s.snapshot([("c1", "a-after-gap"), ("c2", "typed during the gap")])   # 6 recovery
    s.expect_at("main", 4, cells=[("c1", "a")])
    s.expect_at("main", 6, cells=[("c1", "a-after-gap"), ("c2", "typed during the gap")],
                chat=[("q", "hi")], runs={run: "running"})
    open_gap = s.state_at("main", 4).gaps
    assert [(g.gapId, g.startedTick, g.endedTick) for g in open_gap] == [("g1", 4, None)]
    closed = s.state_at("main", 6).gaps
    assert [(g.gapId, g.startedTick, g.endedTick) for g in closed] == [("g1", 4, 5)]


@scenario("missing_event_in_the_middle_of_a_branch",
          "P7, Dup 21 (a hole in the sequence is reported, not skipped)")
def missing_event_in_a_branch(s):
    s.start(cells=[("c1", "m0")])                      # 1
    _linear(s, 2)                                      # 2,3
    b = s.branch_from(tick=3, name="B")                # 4
    _linear(s, 4, prefix="b")                          # 5,6,7,8
    s.drop(6)
    s.expect_at(b, 5, cells=[("c1", "b1")])            # earlier positions are fine
    s.expect_at("main", 3, cells=[("c1", "m2")])
    for branch, tick in ((b, 6), (b, 8), ("main", 6), ("main", 8)):
        r = s.expect_at(branch, tick, status="missing_events")
        assert r.firstMissingTick == 6                 # every branch reports the same first hole
    s.expect_at(b, 9, status="missing_events")         # past the end


@scenario("restore_like_branch_from_an_old_point_then_compare",
          "final scenario multi-file-restore-seek-live-desync-online; P6 (restore, then seek and live)")
def restore_like_sequence(s):
    s.start(cells=[("c1", "load()"), ("c2", "plot()")])    # 1
    run = s.run("c1")                                      # 2
    s.output(run, "1000 rows")                             # 3
    s.finish(run)                                          # 4
    s.edit("c1", "load(bad=True)")                         # 5 the experiment that went wrong
    s.edit("c2", "plot(broken)")                           # 6
    run2 = s.run("c1")                                     # 7
    s.output(run2, "0 rows")                               # 8
    s.finish(run2, status="error")                         # 9
    restored = s.branch_from(tick=4, name="restored", parent="main")   # 10
    s.edit("c2", "plot(fixed)")                            # 11
    s.on("main").edit("c1", "load(bad=True, more=1)")      # 12 the parent carries on
    s.expect_at(restored, 10, cells=[("c1", "load()"), ("c2", "plot()")],
                runs={run: "ok"}, outputs={"c1": ["1000 rows"]})
    s.expect_at(restored, 11, cells=[("c1", "load()"), ("c2", "plot(fixed)")])
    s.expect_at("main", 12, cells=[("c1", "load(bad=True, more=1)"), ("c2", "plot(broken)")],
                runs={run: "ok", run2: "error"}, outputs={"c1": ["0 rows"]})
    s.expect_at("main", 5, cells=[("c1", "load(bad=True)"), ("c2", "plot()")])
    s.expect_at(restored, 11, cells=[("c1", "load()"), ("c2", "plot(fixed)")])   # seek back: no desync
    assert s.state_at(restored, 11).runs[0].runId == run and len(s.state_at(restored, 11).runs) == 1


# --- tests ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(name):
    """Building a scenario runs its expectations against both rebuilders."""
    s = build(name)
    assert s.expectations, "a scenario must assert something"
    assert SCENARIOS[name][0], "a scenario must name its source cases"


def _render(s: Recorded) -> str:
    return json.dumps(s.fixture(), indent=1, sort_keys=True) + "\n"


def write_fixtures() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name in SCENARIOS:
        (FIXTURE_DIR / f"{name}.json").write_text(_render(build(name)))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_fixture_files_are_current(name):
    path = FIXTURE_DIR / f"{name}.json"
    assert path.exists(), f"missing {path}; run: python -m blaym_jupyter.tests.history.test_scenarios --write"
    assert path.read_text() == _render(build(name)), (
        f"{path.name} is stale; run: python -m blaym_jupyter.tests.history.test_scenarios --write")


def test_every_fixture_file_belongs_to_a_scenario():
    assert {p.stem for p in FIXTURE_DIR.glob("*.json")} == set(SCENARIOS)


# Replaying every fixture file and checking its recorded answers is `test_fixtures.py` (Task 8),
# which also validates every event and result against the contracts.


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write_fixtures()
        print(f"wrote {len(SCENARIOS)} fixtures to {FIXTURE_DIR}")
    else:
        sys.exit("usage: python -m blaym_jupyter.tests.history.test_scenarios --write")

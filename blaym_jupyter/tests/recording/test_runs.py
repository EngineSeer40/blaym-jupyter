"""The run recorder's rules (S5, Task 7), against a fake service read and a list instead of the sink.

The real service and the real sink are in `test_runs_service.py`.
"""

from __future__ import annotations

import heapq
import threading

import pytest

from blaym_jupyter.recording.runs import RunRecorder, RunRecorderSettings
from blaym_jupyter.testing.fakes import IdFactory
from blaym_jupyter_types import (
    ResolveCellRevisionCodeDiffers,
    ResolveCellRevisionOk,
    ResolveCellRevisionRequest,
    ResolveCellRevisionResponse,
    ResolveCellRevisionUnknownHistory,
)

EXP = "nb-1"


class FakeTime:
    """A clock and a scheduler in one: `advance` runs the due timers in order, each at its own time."""

    def __init__(self, start_ms: int = 1_770_000_000_000):
        self.ms = start_ms
        self._timers: list = []
        self._seq = 0

    def now_ms(self) -> int:
        return self.ms

    def sleep(self, seconds: float) -> None:
        self.advance(int(seconds * 1000))

    def call_later(self, ms, fn):
        self._seq += 1
        entry = [self.ms + ms, self._seq, fn, True]
        heapq.heappush(self._timers, entry)

        def cancel():
            entry[3] = False

        return cancel

    def advance(self, ms: int) -> None:
        end = self.ms + ms
        while self._timers and self._timers[0][0] <= end:
            at, _, fn, live = heapq.heappop(self._timers)
            if live:
                self.ms = max(self.ms, at)
                fn()
        self.ms = end

    def pending(self) -> int:
        return sum(1 for t in self._timers if t[3])


class UuidIds:
    def __init__(self, prefix="rec"):
        self._f = IdFactory(prefix)

    def new(self) -> str:
        return self._f()


class FakeIngestor:
    """Answers `resolve_cell_revision` from a plain table: (history exists, {cell: [(revision, code)]})."""

    def __init__(self):
        self.history: dict[str, list[tuple[int, str]]] = {}
        self.exists = True
        self.asked: list[tuple[str, str]] = []
        self.fail = False

    def set(self, cell, revision, code):
        self.history.setdefault(cell, []).append((revision, code))

    def resolve_cell_revision(self, request: ResolveCellRevisionRequest) -> ResolveCellRevisionResponse:
        self.asked.append((request.cellId, request.code))
        if self.fail:
            raise RuntimeError("boom")
        if not self.exists:
            return ResolveCellRevisionResponse(ResolveCellRevisionUnknownHistory(status="unknown_history"))
        entries = self.history.get(request.cellId, [])
        for revision, code in reversed(entries):
            if code == request.code:
                return ResolveCellRevisionResponse(ResolveCellRevisionOk(status="ok", cellRevision=revision))
        return ResolveCellRevisionResponse(ResolveCellRevisionCodeDiffers(
            status="code_differs", currentRevision=entries[-1][0] if entries else 1))


class ListSink:
    def __init__(self):
        self.writes = []

    def submit(self, write):
        self.writes.append(write)

    def events(self, *types):
        return [e for w in self.writes for e in w.events if not types or e["type"] in types]

    def types(self):
        return [e["type"] for w in self.writes for e in w.events]


class FakeRegistry:
    def __init__(self):
        self.alive = False
        self.aliases: dict[str, str] = {}

    def is_alive(self, notebook_id):
        return self.alive

    def alias_for(self, notebook_id, model_cell_id):
        return self.aliases.get(model_cell_id, model_cell_id)


class Rig:
    def __init__(self, **settings):
        self.t = FakeTime()
        self.ing, self.sink, self.reg = FakeIngestor(), ListSink(), FakeRegistry()
        self.idle: list[str] = []
        self.rec = RunRecorder(self.ing, self.sink, self.t, self.t, UuidIds(), registry=self.reg,
                               settings=RunRecorderSettings(**settings), on_idle=self.idle.append)

    def code(self, cell, revision, code):
        self.ing.set(cell, revision, code)

    def request(self, run_id, cell="c1", code="x = 1", kernel="k1"):
        self.rec.run_requested(kernel, EXP, run_id, cell, code)

    def msg(self, run_id, msg_type, content, kernel="k1"):
        self.rec.kernel_message(kernel, run_id, msg_type, content)

    def execute(self, run_id, count, *, text=None, reply="ok", kernel="k1", idle=True):
        """What a kernel sends for one cell."""
        self.msg(run_id, "execute_input", {"code": "", "execution_count": count}, kernel)
        if text is not None:
            self.msg(run_id, "stream", {"name": "stdout", "text": text}, kernel)
        if reply is not None:
            self.msg(run_id, "execute_reply", {"status": reply, "execution_count": count}, kernel)
        if idle:
            self.msg(run_id, "status", {"execution_state": "idle"}, kernel)

    def start_kernel(self, kernel="k1"):
        self.rec.kernel_started(kernel, EXP, "kid-" + kernel, "python3", "python")

    def types(self):
        return self.sink.types()


def finished(rig, run_id):
    return [e for e in rig.sink.events("run_finished") if e["payload"]["runId"] == run_id]


# -- the plain path ----------------------------------------------------------------------------

def test_a_run_writes_started_outputs_finished_in_order_with_the_kernels_count():
    r = Rig()
    r.code("c1", 4, "x = 1")
    r.start_kernel()
    r.request("r1")
    r.execute("r1", 7, text="hello\n")
    r.t.advance(300)
    assert r.types() == ["kernel_started", "run_started", "output_added", "run_finished"]
    started = r.sink.events("run_started")[0]
    assert started["payload"] == {"runId": "r1", "cellId": "c1", "cellRevision": 4, "runCount": 7}
    assert started["links"] == {"cellId": "c1", "runId": "r1", "cellRevision": 4}
    assert finished(r, "r1")[0]["payload"]["status"] == "ok"
    assert r.idle == [EXP]  # the kernel has no run left


@pytest.mark.parametrize("reply,extra,want", [
    ("ok", None, "ok"),
    ("error", {"ename": "ValueError", "evalue": "x", "traceback": ["ValueError: x"]}, "error"),
    ("error", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": ["KeyboardInterrupt"]}, "interrupted"),
    ("abort", None, "interrupted"),
    ("aborted", None, "interrupted"),
])
def test_status_comes_from_the_reply_and_interrupts_are_interrupted(reply, extra, want):
    r = Rig()
    r.code("c1", 1, "x = 1")
    r.request("r1")
    r.msg("r1", "execute_input", {"execution_count": 1})
    if extra:
        r.msg("r1", "error", extra)
    r.msg("r1", "execute_reply", {"status": reply})
    r.msg("r1", "status", {"execution_state": "idle"})
    assert finished(r, "r1")[0]["payload"]["status"] == want


def test_a_run_ends_when_only_one_of_reply_and_idle_arrives_after_the_grace_time():
    r = Rig(reply_grace_ms=1000)
    r.code("c1", 1, "x = 1")
    r.request("r1")
    r.execute("r1", 1, idle=False)
    assert not finished(r, "r1")
    r.t.advance(999)
    assert not finished(r, "r1")
    r.t.advance(2)
    assert finished(r, "r1")[0]["payload"]["status"] == "ok"
    r2 = Rig(reply_grace_ms=1000)
    r2.code("c1", 1, "x = 1")
    r2.request("r1")
    r2.execute("r1", 1, reply=None)  # an observer that never sees shell replies
    r2.t.advance(1001)
    assert finished(r2, "r1")[0]["payload"]["status"] == "ok"


def test_status_messages_other_than_idle_and_unrelated_types_are_ignored():
    r = Rig()
    r.code("c1", 1, "x = 1")
    r.request("r1")
    r.msg("r1", "status", {"execution_state": "busy"})
    r.msg("r1", "kernel_info_reply", {})
    r.msg("r1", "comm_msg", {})
    r.execute("r1", 1)
    assert r.types() == ["run_started", "run_finished"]
    assert not r.rec.stats["unknown_executions"]


def test_a_three_minute_run_with_silent_stretches_finishes_normally_and_records_everything():
    r = Rig()
    r.code("c1", 1, "slow()")
    r.start_kernel()
    r.request("r1", code="slow()")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "stream", {"name": "stdout", "text": "start\n"})
    for _ in range(18):  # three minutes of silence in ten second steps
        r.t.advance(10_000)
        assert not finished(r, "r1")
    r.msg("r1", "stream", {"name": "stdout", "text": "more\n"})
    r.msg("r1", "display_data", {"data": {"text/plain": "<Fig>", "image/png": "aaaa"}, "metadata": {}})
    r.t.advance(60_000)
    r.msg("r1", "execute_reply", {"status": "ok"})
    r.msg("r1", "status", {"execution_state": "idle"})
    assert finished(r, "r1")[0]["payload"]["status"] == "ok"
    assert r.types().count("run_started") == 1
    assert "recording_gap_started" not in r.types()
    assert not [k for k in r.rec.stats if k.startswith("runs_dropped")]
    assert r.t.pending() == 0  # no timer left behind


# -- finding the revision ----------------------------------------------------------------------

def test_the_run_waits_for_its_code_then_writes_everything_after_run_started():
    r = Rig()
    r.request("r1", code="y = 2")  # the browser's edit has not arrived
    r.execute("r1", 1, text="out\n")
    r.t.advance(3000)
    assert r.types() == []  # held, nothing guessed
    r.code("c1", 2, "y = 2")
    r.t.advance(100)
    assert r.types() == ["run_started", "output_added", "run_finished"]
    assert r.sink.events("run_started")[0]["payload"]["cellRevision"] == 2


def test_the_latest_revision_that_matches_the_code_is_used():
    r = Rig()
    r.code("c1", 3, "same")
    r.code("c1", 4, "other")
    r.code("c1", 5, "same")
    r.request("r1", code="same")
    r.execute("r1", 1)
    assert r.sink.events("run_started")[0]["payload"]["cellRevision"] == 5


def test_a_run_whose_code_never_arrives_writes_one_closed_point_gap_and_nothing_else():
    r = Rig()
    r.request("r1", code="never")
    r.execute("r1", 1, text="lost\n")
    r.t.advance(4900)
    assert r.types() == []
    r.t.advance(200)
    assert r.types() == ["recording_gap_started", "recording_gap_ended"]
    started, ended = r.sink.events()
    assert started["payload"]["source"] == "kernel" and started["payload"]["reason"] == "unknown"
    assert started["payload"]["gapId"] == ended["payload"]["gapId"]
    r.msg("r1", "stream", {"name": "stdout", "text": "later"})  # late messages of the dropped run vanish
    r.t.advance(10_000)
    assert r.types() == ["recording_gap_started", "recording_gap_ended"]
    assert r.rec.stats["runs_dropped_timeout"] == 1 and r.rec.stats["late_messages"] == 1
    assert r.t.pending() == 0
    assert r.idle == [EXP]


def test_the_wait_is_longer_while_the_recorder_is_alive_and_shrinks_when_it_is_not():
    r = Rig()
    r.reg.alive = True
    r.request("r1", code="slow edit")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.t.advance(20_000)
    assert r.types() == []  # still waiting past 5 s
    r.code("c1", 2, "slow edit")
    r.t.advance(100)
    assert r.types() == ["run_started"]
    r2 = Rig()
    r2.reg.alive = True
    r2.request("r1", code="never")
    r2.t.advance(29_000)
    assert r2.types() == []
    r2.t.advance(1_500)
    assert r2.types() == ["recording_gap_started", "recording_gap_ended"]
    r3 = Rig()
    r3.reg.alive = True
    r3.request("r1", code="never")
    r3.t.advance(6_000)
    r3.reg.alive = False  # the page went away: the short wait applies from then
    r3.t.advance(200)
    assert r3.types() == ["recording_gap_started", "recording_gap_ended"]


def test_an_unknown_history_and_a_failing_read_are_waited_out_like_a_missing_edit():
    r = Rig()
    r.ing.exists = False
    r.request("r1")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.t.advance(2000)
    assert r.types() == []
    r.ing.exists = True
    r.code("c1", 1, "x = 1")
    r.t.advance(100)
    assert r.types() == ["run_started"]
    r2 = Rig()
    r2.ing.fail = True
    r2.request("r1")
    r2.t.advance(6000)
    assert r2.types() == ["recording_gap_started", "recording_gap_ended"]
    assert r2.rec.stats["resolve_errors"] > 10


def test_the_cell_id_is_translated_through_the_registry_alias_before_matching():
    r = Rig()
    r.reg.aliases["tmp-44"] = "blaym-cell-1"
    r.code("blaym-cell-1", 2, "x = 1")
    r.request("r1", cell="tmp-44", code="x = 1")
    r.execute("r1", 1)
    assert r.ing.asked[0][0] == "blaym-cell-1"
    started = r.sink.events("run_started")[0]
    assert started["payload"]["cellId"] == "blaym-cell-1" and started["payload"]["cellRevision"] == 2


def test_the_alias_that_arrives_late_is_used_by_the_next_poll():
    r = Rig()
    r.code("blaym-cell-1", 2, "x = 1")
    r.request("r1", cell="tmp-44", code="x = 1")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.t.advance(500)
    assert r.types() == []
    r.reg.aliases["tmp-44"] = "blaym-cell-1"
    r.t.advance(100)
    assert r.types() == ["run_started"]


def test_a_run_with_no_cell_is_an_unlinked_point_gap():
    r = Rig()
    r.rec.run_requested("k1", EXP, "r1", None, "print(1)")
    r.execute("r1", 1, text="1\n")
    assert r.types() == ["recording_gap_started", "recording_gap_ended"]
    assert r.rec.stats["unlinked_runs"] == 1
    assert not r.rec.stats["unknown_executions"]  # its messages are its own, not an unknown parent


def test_a_duplicate_request_is_ignored():
    r = Rig()
    r.code("c1", 1, "x = 1")
    r.request("r1")
    r.request("r1")
    r.execute("r1", 1)
    assert r.types().count("run_started") == 1 and r.rec.stats["duplicate_requests"] == 1


# -- the lane ----------------------------------------------------------------------------------

def test_run_all_keeps_request_order_when_the_first_code_arrives_after_the_second():
    r = Rig()
    r.code("c2", 1, "b = 2")  # B's code is already there; A's is not
    r.start_kernel()
    r.request("A", cell="c1", code="a = 1")
    r.request("B", cell="c2", code="b = 2")
    r.t.advance(200)
    assert r.types() == ["kernel_started"]  # B does not jump the queue
    r.execute("A", 1, text="a\n")
    r.execute("B", 2, text="b\n")
    r.t.advance(1000)
    assert r.types() == ["kernel_started"]
    r.code("c1", 1, "a = 1")
    r.t.advance(100)
    order = [(e["type"], e["payload"].get("runId")) for e in r.sink.events()]
    assert order == [("kernel_started", None),
                     ("run_started", "A"), ("output_added", "A"), ("run_finished", "A"),
                     ("run_started", "B"), ("output_added", "B"), ("run_finished", "B")]
    assert [e["payload"]["runCount"] for e in r.sink.events("run_started")] == [1, 2]


def test_when_the_first_run_times_out_the_second_goes_on_after_its_gap():
    r = Rig()
    r.code("c2", 1, "b = 2")
    r.request("A", cell="c1", code="never")
    r.request("B", cell="c2", code="b = 2")
    r.execute("B", 1, text="b\n")
    r.t.advance(5100)
    assert r.types() == ["recording_gap_started", "recording_gap_ended", "run_started", "output_added", "run_finished"]


def test_a_later_run_starts_after_the_one_before_it_has_ended_not_while_it_runs():
    r = Rig()
    r.code("c1", 1, "x")
    r.code("c2", 1, "y")
    r.request("A", cell="c1", code="x")
    r.request("B", cell="c2", code="y")
    r.msg("A", "execute_input", {"execution_count": 1})
    r.t.advance(10_000)  # B has no count yet and A is still executing: B waits, however long
    assert r.types() == ["run_started"]
    r.msg("A", "execute_reply", {"status": "ok"})
    r.msg("A", "status", {"execution_state": "idle"})
    r.execute("B", 2)
    assert r.types() == ["run_started", "run_finished", "run_started", "run_finished"]
    assert [e["payload"]["runId"] for e in r.sink.events("run_finished")] == ["A", "B"]


def test_the_next_execute_input_ends_a_run_whose_end_was_never_seen():
    r = Rig()
    r.code("c1", 1, "x")
    r.code("c2", 1, "y")
    r.request("A", cell="c1", code="x")
    r.request("B", cell="c2", code="y")
    r.msg("A", "execute_input", {"execution_count": 1})
    r.msg("A", "stream", {"name": "stdout", "text": "a"})  # then nothing more from A
    r.execute("B", 2)
    r.t.advance(300)
    seq = [(e["type"], e["payload"].get("runId"), e["payload"].get("status")) for e in r.sink.events()]
    assert seq == [("run_started", "A", None), ("output_added", "A", None),
                   ("run_finished", "A", "interrupted"),
                   ("run_started", "B", None), ("run_finished", "B", "ok")]


def test_two_kernels_have_separate_lanes():
    r = Rig()
    r.code("c2", 1, "b")
    r.request("A", cell="c1", code="never", kernel="k1")  # k1's head is stuck
    r.request("B", cell="c2", code="b", kernel="k2")
    r.execute("B", 1, kernel="k2")
    assert r.types() == ["run_started", "run_finished"]
    assert r.sink.events("run_started")[0]["payload"]["runId"] == "B"


def test_the_count_falls_back_to_the_recorders_own_after_five_seconds_per_kernel_session():
    r = Rig()
    r.code("c1", 1, "x")
    r.start_kernel()
    r.request("r1", code="x")
    r.msg("r1", "stream", {"name": "stdout", "text": "."})  # it ran; only the count is missing
    r.t.advance(4900)
    assert r.types() == ["kernel_started"]
    r.t.advance(200)
    assert r.sink.events("run_started")[0]["payload"]["runCount"] == 1
    r.msg("r1", "execute_input", {"execution_count": 9})  # arrives too late: the count is written
    r.msg("r1", "execute_reply", {"status": "ok"})
    r.msg("r1", "status", {"execution_state": "idle"})
    r.request("r2", code="x")
    r.msg("r2", "stream", {"name": "stdout", "text": "."})  # it ran; only the count is missing
    r.t.advance(5100)
    assert r.sink.events("run_started")[1]["payload"]["runCount"] == 2  # never below what was written
    r.rec.kernel_restarted("k1")
    r.request("r3", code="x")
    r.msg("r3", "stream", {"name": "stdout", "text": "."})  # it ran; only the count is missing
    r.t.advance(5100)
    assert r.sink.events("run_started")[2]["payload"]["runCount"] == 1  # a new session starts again


# -- restart and kernel_gone --------------------------------------------------------------------

def test_a_restart_drops_a_waiting_run_before_kernel_restarted_is_written():
    r = Rig()
    r.start_kernel()
    r.request("r1", code="never")
    r.t.advance(1000)
    r.rec.kernel_restarted("k1")
    assert r.types() == ["kernel_started", "recording_gap_started", "recording_gap_ended", "kernel_restarted"]
    assert r.rec.stats["runs_dropped_restart"] == 1
    assert r.sink.events("kernel_restarted")[0]["payload"] == {"kernelId": "kid-k1", "reason": "unknown"}


def test_a_restart_resolves_a_run_whose_code_has_just_arrived_and_starts_it_first():
    r = Rig()
    r.start_kernel()
    r.request("r1", code="x")
    r.code("c1", 1, "x")  # arrived between two polls
    r.rec.kernel_restarted("k1")
    assert r.types() == ["kernel_started", "run_started", "kernel_restarted"]


def test_a_restart_leaves_an_executing_run_to_the_rebuilders_unfinished_rule():
    r = Rig()
    r.code("c1", 1, "x")
    r.start_kernel()
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "stream", {"name": "stdout", "text": "partial"})
    r.t.advance(10)
    r.rec.kernel_restarted("k1")
    types = r.types()
    assert types[:3] == ["kernel_started", "run_started", "output_added"]
    assert types[-1] == "kernel_restarted" and "run_finished" not in types
    r.msg("r1", "stream", {"name": "stdout", "text": "after"})  # what the dead kernel's run sends later is ignored
    assert r.rec.stats["late_messages"] == 1


def test_kernel_gone_finishes_started_runs_as_interrupted_and_drops_waiting_ones():
    r = Rig()
    r.code("c1", 1, "x")
    r.start_kernel()
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.request("r2", code="never")
    r.rec.kernel_gone("k1")
    assert r.types() == ["kernel_started", "run_started", "recording_gap_started", "recording_gap_ended",
                         "run_finished"] or r.types()[:2] == ["kernel_started", "run_started"]
    assert sorted(r.types()[2:]) == sorted(["recording_gap_started", "recording_gap_ended", "run_finished"])
    assert finished(r, "r1")[0]["payload"]["status"] == "interrupted"
    assert r.rec.stats["runs_dropped_restart"] == 1
    assert r.t.pending() == 0
    r.msg("r1", "stream", {"name": "stdout", "text": "x"}, kernel="k1")  # a fresh unknown kernel; nothing breaks
    assert r.idle == [EXP]


def test_close_treats_every_kernel_as_gone():
    r = Rig()
    r.code("c1", 1, "x")
    r.code("c2", 1, "y")
    r.request("a", cell="c1", code="x", kernel="k1")
    r.request("b", cell="c2", code="y", kernel="k2")
    r.msg("a", "execute_input", {"execution_count": 1}, kernel="k1")
    r.msg("b", "execute_input", {"execution_count": 1}, kernel="k2")
    r.rec.close()
    assert sorted(e["payload"]["runId"] for e in r.sink.events("run_finished")) == ["a", "b"]
    assert all(e["payload"]["status"] == "interrupted" for e in r.sink.events("run_finished"))


# -- unknown parents -----------------------------------------------------------------------------

def test_messages_that_arrive_before_run_requested_are_held_and_used():
    r = Rig()
    r.code("c1", 1, "x")
    r.start_kernel()
    r.execute("late", 1, text="early\n")  # the editor adapter told us about the run afterwards
    r.t.advance(500)
    assert r.types() == ["kernel_started"]
    r.request("late", code="x")
    r.t.advance(300)
    assert r.types() == ["kernel_started", "run_started", "output_added", "run_finished"]
    assert not r.rec.stats["unknown_executions"] and r.t.pending() == 0


def test_an_execute_input_that_never_gets_a_parent_counts_once_and_makes_one_gap_per_session():
    r = Rig()
    r.start_kernel()
    r.msg("x1", "execute_input", {"execution_count": 1})
    r.msg("x1", "stream", {"name": "stdout", "text": "who"})
    r.t.advance(1900)
    assert r.types() == ["kernel_started"]
    r.t.advance(200)
    assert r.types() == ["kernel_started", "recording_gap_started", "recording_gap_ended"]
    r.msg("x2", "execute_input", {"execution_count": 2})
    r.t.advance(2100)
    assert r.rec.stats["unknown_executions"] == 2
    assert r.types().count("recording_gap_started") == 1  # one per kernel session
    r.rec.kernel_restarted("k1")
    r.msg("x3", "execute_input", {"execution_count": 1})
    r.t.advance(2100)
    assert r.types().count("recording_gap_started") == 2


def test_completion_inspection_and_status_traffic_never_counts():
    r = Rig()
    r.start_kernel()
    for i in range(20):
        pid = f"complete-{i}"
        r.msg(pid, "status", {"execution_state": "idle"})  # the idle of a complete_request, say
        r.msg(pid, "complete_reply", {"status": "ok", "matches": []})
        r.msg(pid, "kernel_info_reply", {})
    r.t.advance(5000)
    assert r.types() == ["kernel_started"]
    assert not r.rec.stats["unknown_executions"]
    assert r.t.pending() == 0


def test_the_unknown_parent_buffer_is_bounded():
    r = Rig(max_unknown_parents=10)
    for i in range(100):
        r.msg(f"p{i}", "status", {"execution_state": "idle"})
    assert len(r.rec._kernels["k1"].unknown) <= 10


# -- bounds and cleanliness ---------------------------------------------------------------------

def test_a_waiting_run_holds_messages_under_the_bound_and_still_writes_start_and_finish():
    r = Rig(max_held_messages=50)
    r.request("r1", code="later")
    r.msg("r1", "execute_input", {"execution_count": 1})
    for i in range(500):
        r.msg("r1", "display_data", {"data": {"text/plain": f"item {i}"}, "metadata": {}})
    assert r.rec._held_messages <= 50
    r.msg("r1", "execute_reply", {"status": "ok"})
    r.msg("r1", "status", {"execution_state": "idle"})
    r.code("c1", 1, "later")
    r.t.advance(100)
    assert r.types().count("output_added") == 50
    assert r.types()[0] == "run_started" and r.types()[-1] == "run_finished"
    assert r.rec.stats["held_dropped"] == 450
    assert r.rec._held_messages == 0 and r.rec._held_bytes == 0


def test_the_recorder_starts_no_thread_and_leaves_no_timer_behind():
    before = threading.active_count()
    r = Rig()
    r.code("c1", 1, "x")
    r.start_kernel()
    for i in range(5):
        r.request(f"r{i}", code="x")
        r.execute(f"r{i}", i + 1, text="o\n")
    r.request("lost", code="never")
    r.t.advance(30_000)
    r.rec.close()
    assert threading.active_count() == before
    assert r.t.pending() == 0


def test_a_message_that_the_assembler_cannot_take_does_not_stop_the_run():
    r = Rig()
    r.code("c1", 1, "x")
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "display_data", {"data": {"image/png": object()}, "metadata": {}})  # not even JSON
    r.msg("r1", "stream", {"name": "stdout", "text": "after\n"})
    r.msg("r1", "execute_reply", {"status": "ok"})
    r.msg("r1", "status", {"execution_state": "idle"})
    assert finished(r, "r1")
    assert r.types().count("output_added") >= 1


def test_the_lane_writes_kernel_started_behind_a_waiting_run():
    r = Rig()
    r.request("r1", code="x")
    r.start_kernel()
    assert r.types() == []
    r.code("c1", 1, "x")
    r.t.advance(100)
    r.execute("r1", 1)
    assert r.types() == ["run_started", "kernel_started", "run_finished"]


# -- cells the kernel skipped (Run All after an error, live-testing LT-8) ------------------------

@pytest.mark.parametrize("reply", ["aborted", "abort"])
@pytest.mark.parametrize("code_known", [True, False])
@pytest.mark.parametrize("reply_before_request", [True, False])
def test_run_all_with_an_error_writes_nothing_for_skipped_cells_and_does_not_delay_the_lane(
        reply, code_known, reply_before_request):
    r = Rig()
    r.code("c1", 1, "boom")
    if code_known:
        r.code("c2", 1, "y")
    r.code("c3", 1, "z")

    def skipped():
        r.msg("r2", "status", {"execution_state": "busy"})
        r.msg("r2", "execute_reply", {"status": reply})
        r.msg("r2", "status", {"execution_state": "idle"})

    r.request("r1", "c1", "boom")
    if reply_before_request:
        skipped()
        r.request("r2", "c2", "y")
    else:
        r.request("r2", "c2", "y")
    r.execute("r1", 1, reply="error")
    if not reply_before_request:
        skipped()
    r.request("r3", "c3", "z")
    r.execute("r3", 2)
    # no time has passed: nothing waited for a count, a grace period or the missing code
    assert [(e["type"], e["payload"]["runId"]) for e in r.sink.events()] == [
        ("run_started", "r1"), ("run_finished", "r1"), ("run_started", "r3"), ("run_finished", "r3")]
    assert r.rec.stats["skipped_runs"] == 1 and r.rec.stats["count_fallbacks"] == 0
    assert r.rec.stats["point_gaps"] == 0 and r.idle and r.t.pending() == 0


def test_without_a_count_or_any_sign_of_running_no_run_is_invented():
    r = Rig(run_count_wait_ms=1000, reply_grace_ms=1000)
    r.code("c1", 1, "x = 1")
    r.request("r1")
    r.t.advance(5000)
    assert r.types() == []
    r.msg("r1", "stream", {"name": "stdout", "text": "here"})  # now it has evidence
    assert r.types()[0] == "run_started" and r.rec.stats["count_fallbacks"] == 1


def test_a_run_that_only_goes_idle_is_dropped_with_a_gap_after_the_grace_time():
    r = Rig(run_count_wait_ms=1000, reply_grace_ms=1000)
    r.code("c1", 1, "x = 1")
    r.request("r1")
    r.msg("r1", "status", {"execution_state": "idle"})
    r.t.advance(5000)
    assert r.types() == ["recording_gap_started", "recording_gap_ended"] and r.idle

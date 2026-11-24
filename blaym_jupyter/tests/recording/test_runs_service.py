"""The run recorder and the ordered sink against the real service over the memory ports (S5, Task 7).

Generated kernel message sequences (several runs, two kernels, restarts, the browser's code edit
delivered before the run request, after the kernel's messages, or never) end in a state that must
equal an independent oracle, and every committed tick must rebuild. Named cases follow.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import blaym_jupyter.tests.history.conftest as _profiles  # noqa: F401  (profiles)
from blaym_jupyter.recording.outputs import OutputLimits
from blaym_jupyter.recording.runs import RunRecorder, RunRecorderSettings
from blaym_jupyter.recording.sink import OrderedSink
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.service.core import Service
from blaym_jupyter.testing.fakes import IdFactory
from blaym_jupyter.testing.memory import MemoryBlobStore, MemoryEventStore
from blaym_jupyter.tests.recording.test_runs import FakeRegistry, FakeTime, UuidIds
from blaym_jupyter_types import GetBlobRequest, RebuildOk, StateAtRequest

TOKEN = "t0ken"
DEEP = _profiles.DEEP
NS = uuid.UUID("5c1f0a6e-0000-4000-8000-00000000d1d1")


class Bound:
    """The service with the credential bound: what `Ingestor` is in the Jupyter process."""

    def __init__(self, service):
        self.s = service
        self.gate: threading.Event | None = None  # when set, the worker blocks in `ingest` / `put_blob`

    def ingest(self, request):
        if self.gate is not None:
            self.gate.wait(30)
        return self.s.ingest(TOKEN, request)

    def put_blob(self, exploration_id, data):
        if self.gate is not None:
            self.gate.wait(30)
        return self.s.put_blob(TOKEN, exploration_id, data)

    def resolve_cell_revision(self, request):
        return self.s.resolve_cell_revision(TOKEN, request)

    def cell_revisions(self, exploration_id):
        return self.s.cell_revisions(TOKEN, exploration_id)


class Harness:
    def __init__(self, *, sink_kw=None, settings_kw=None, limits=None, registry=None):
        self.t = FakeTime()
        self.events, self.blobs = MemoryEventStore(), MemoryBlobStore()
        self.service = Service(self.events, self.blobs, FixedTokenAuthorizer(TOKEN), self.t, new_id=IdFactory("svc"))
        self.ing = Bound(self.service)
        self.sink = OrderedSink(self.ing, self.t, poll_s=0.002, **(sink_kw or {}))
        self.reg = registry or FakeRegistry()
        self.rec = RunRecorder(self.ing, self.sink, self.t, self.t, UuidIds("run"), registry=self.reg,
                               settings=RunRecorderSettings(**(settings_kw or {})),
                               limits=limits or OutputLimits(), on_idle=self.sink.on_idle)
        self.revs: dict[tuple[str, str], int] = {}
        self._n = 0

    def close(self):
        self.rec.close()
        self.sink.close(2.0)

    # -- the browser --------------------------------------------------------------------------

    def _ev(self, exp, type_, payload):
        self._n += 1
        return {"schemaVersion": 2, "eventId": str(uuid.uuid5(NS, f"{exp}:{self._n}")), "explorationId": exp,
                "wallTimeMs": self.t.now_ms(), "userId": "local", "workspaceId": "local", "originId": "tab-1",
                "editor": "jupyterlab", "type": type_, "payload": payload}

    def browser(self, exp, type_, payload):
        out = self.service.ingest(TOKEN, {"explorationId": exp, "editor": "jupyterlab", "instanceId": "tab-1",
                                          "events": [self._ev(exp, type_, payload)]}).outcomes[0]
        out = getattr(out, "root", out)
        assert out.status == "accepted", (type_, getattr(out, "reason", None))

    def snapshot(self, exp, cells):
        for cell, _ in cells:
            self.revs[(exp, cell)] = 1
        self.browser(exp, "notebook_snapshot", {
            "path": "nb.ipynb", "notebookMetadata": {}, "kernelName": "python3", "language": "python",
            "cells": [{"cellId": c, "cellType": "code", "code": code, "revision": 1, "metadata": {}, "outputs": []}
                      for c, code in cells]})

    def insert(self, exp, cell, code, index):
        self.revs[(exp, cell)] = 1
        self.browser(exp, "cell_inserted", {"cellId": cell, "cellType": "code", "code": code, "index": index,
                                            "revision": 1})

    def edit(self, exp, cell, code):
        self.revs[(exp, cell)] += 1
        self.browser(exp, "cell_source_changed", {"cellId": cell, "code": code, "revision": self.revs[(exp, cell)]})
        return self.revs[(exp, cell)]

    # -- kernel and reading ---------------------------------------------------------------------

    def kernel(self, kernel="k1", exp="nb-1"):
        self.rec.kernel_started(kernel, exp, "kid-" + kernel, "python3", "python")

    def request(self, run_id, exp="nb-1", cell="c1", code="x = 1", kernel="k1"):
        self.rec.run_requested(kernel, exp, run_id, cell, code)

    def msg(self, run_id, msg_type, content, kernel="k1"):
        self.rec.kernel_message(kernel, run_id, msg_type, content)

    def finish_msgs(self, run_id, count, reply="ok", kernel="k1"):
        self.msg(run_id, "execute_reply", {"status": reply, "execution_count": count}, kernel)
        self.msg(run_id, "status", {"execution_state": "idle"}, kernel)

    def settle(self, ms=500):
        self.t.advance(ms)
        assert self.sink.drain(10)

    def tip(self, exp):
        rec = self.events.exploration(exp)
        return rec.active_branch_id, self.events.tip_tick(exp)

    def state(self, exp, tick=None):
        branch, tip = self.tip(exp)
        result = self.service.state_at(TOKEN, StateAtRequest(explorationId=exp, branchId=branch, tick=tick or tip))
        assert isinstance(result, RebuildOk), result
        return result.state

    def types(self, exp="nb-1"):
        return [e.type for e in self.events.events_for(exp)]

    def stored(self, exp="nb-1", *types):
        return [e for e in self.events.events_for(exp) if not types or e.type in types]

    def assert_clean(self, *exps):
        """Nothing refused or dropped by the sink, and every committed tick rebuilds."""
        assert not self.sink.refused, dict(self.sink.refused)
        for key in ("dropped_overflow", "dropped_errors", "parked_dropped", "errors", "outcome_mismatch"):
            assert not self.sink.stats[key], (key, dict(self.sink.stats))
        for exp in exps:
            branch, tip = self.tip(exp)
            for tick in range(1, tip + 1):
                result = self.service.state_at(TOKEN, StateAtRequest(explorationId=exp, branchId=branch, tick=tick))
                assert isinstance(result, RebuildOk), (exp, tick, result)


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.close()


def run_of(state, run_id):
    return next(r for r in state.runs if r.runId == run_id)


def items_of(state, cell_id, run_id):
    cell = next(c for c in state.notebook.cells if c.cellId == cell_id)
    group = next((g for g in cell.outputs if g.runId == run_id), None)
    return [(i.outputType.value if hasattr(i.outputType, "value") else i.outputType, i.preview)
            for i in (group.items if group else [])]


# -- named cases ----------------------------------------------------------------------------------

def test_a_run_on_a_cell_inserted_a_moment_ago(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    h.insert("nb-1", "c2", "print('new')", 1)
    h.request("r1", cell="c2", code="print('new')")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "new\n"})
    h.finish_msgs("r1", 1)
    h.settle()
    s = h.state("nb-1")
    r = run_of(s, "r1")
    assert (r.cellId, r.cellRevision, r.runCount, r.status.root) == ("c2", 1, 1, "ok")
    assert items_of(s, "c2", "r1") == [("stream", "new\n")]
    h.assert_clean("nb-1")


def test_a_run_whose_code_arrives_after_the_run_is_requested_and_after_its_messages(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    h.request("r1", code="b = 2")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "hi\n"})
    h.finish_msgs("r1", 1)
    h.t.advance(2000)
    assert h.types() == ["notebook_snapshot"]  # nothing yet: the edit has not arrived
    h.edit("nb-1", "c1", "b = 2")
    h.settle()
    s = h.state("nb-1")
    assert (run_of(s, "r1").cellRevision, run_of(s, "r1").status.root) == (2, "ok")
    ticks = [e.type for e in h.stored()]
    assert ticks.index("cell_source_changed") < ticks.index("run_started")
    h.assert_clean("nb-1")


def test_an_edit_during_a_run_leaves_the_run_on_its_own_revision(h):
    h.snapshot("nb-1", [("c1", "v1")])
    h.edit("nb-1", "c1", "v2")
    h.kernel()
    h.request("r1", code="v2")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.settle(200)
    assert h.edit("nb-1", "c1", "v3") == 3  # the user types while the cell runs
    h.msg("r1", "stream", {"name": "stdout", "text": "late output\n"})
    h.finish_msgs("r1", 1)
    h.settle()
    s = h.state("nb-1")
    assert run_of(s, "r1").cellRevision == 2  # the code that was sent, not the tip
    cell = next(c for c in s.notebook.cells if c.cellId == "c1")
    assert cell.revision == 3 and cell.code == "v3"
    assert items_of(s, "c1", "r1") == [("stream", "late output\n")]
    h.assert_clean("nb-1")


def test_a_cell_edited_again_before_the_run_is_linked_still_resolves(h):
    h.snapshot("nb-1", [("c1", "v1")])
    h.edit("nb-1", "c1", "v2")
    h.edit("nb-1", "c1", "v3")  # the browser is already ahead of the run request
    h.request("r1", code="v2")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.finish_msgs("r1", 1)
    h.settle()
    assert run_of(h.state("nb-1"), "r1").cellRevision == 2


@pytest.mark.parametrize("code", ['api_key = "sk-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"', 'password = "hunter2-Plant3d-Pw"'])
def test_code_containing_a_fake_key_still_resolves(h, code):
    h.snapshot("nb-1", [("c1", "a")])
    h.edit("nb-1", "c1", code)
    h.kernel()
    h.request("r1", code=code)
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.finish_msgs("r1", 1)
    h.settle()
    s = h.state("nb-1")
    assert run_of(s, "r1").cellRevision == 2
    stored = next(c for c in s.notebook.cells if c.cellId == "c1").code
    assert "sk-A1b2" not in stored and "hunter2" not in stored  # and the history never held the key


def test_two_runs_overlapping_keep_their_own_outputs_and_order(h):
    h.snapshot("nb-1", [("c1", "a"), ("c2", "b")])
    h.kernel()
    h.request("A", cell="c1", code="a")
    h.request("B", cell="c2", code="b")  # requested while A runs
    h.msg("A", "execute_input", {"execution_count": 1})
    h.msg("A", "stream", {"name": "stdout", "text": "A1 "})
    h.msg("A", "stream", {"name": "stdout", "text": "A2"})
    h.settle(300)
    h.finish_msgs("A", 1)
    h.msg("B", "execute_input", {"execution_count": 2})
    h.msg("B", "stream", {"name": "stderr", "text": "B1"})
    h.finish_msgs("B", 2, reply="error")
    h.settle()
    s = h.state("nb-1")
    assert items_of(s, "c1", "A") == [("stream", "A1 A2")]
    assert items_of(s, "c2", "B") == [("stream", "B1")]
    assert [(r.runId, r.runCount, r.status.root) for r in s.runs] == [("A", 1, "ok"), ("B", 2, "error")]
    assert run_of(s, "A").startedTick < run_of(s, "A").endedTick < run_of(s, "B").startedTick
    h.assert_clean("nb-1")


def test_run_all_keeps_request_order_when_a_runs_code_is_not_there_yet(h):
    h.snapshot("nb-1", [("c1", "a"), ("c2", "b")])
    h.kernel()
    h.request("A", cell="c1", code="a2")  # the edit for A will land late
    h.request("B", cell="c2", code="b")  # B's code is already in the history
    for rid, n, text in (("A", 1, "a\n"), ("B", 2, "b\n")):
        h.msg(rid, "execute_input", {"execution_count": n})
        h.msg(rid, "stream", {"name": "stdout", "text": text})
        h.finish_msgs(rid, n)
    h.t.advance(1000)
    assert h.types() == ["notebook_snapshot"]
    h.edit("nb-1", "c1", "a2")
    h.settle()
    s = h.state("nb-1")
    a, b = run_of(s, "A"), run_of(s, "B")
    assert a.startedTick < a.endedTick < b.startedTick < b.endedTick
    assert (a.runCount, b.runCount) == (1, 2)
    h.assert_clean("nb-1")


def test_a_restart_racing_a_pending_run_resolves_it_first(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    h.request("r1", code="b")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "partial"})
    h.edit("nb-1", "c1", "b")  # the edit lands between two polls, then the kernel restarts
    h.rec.kernel_restarted("k1")
    h.settle()
    types = h.types()
    assert types.index("run_started") < types.index("kernel_restarted")
    s = h.state("nb-1")
    assert run_of(s, "r1").status.root == "unfinished" and s.kernel.lastRestart is not None
    assert items_of(s, "c1", "r1") == [("stream", "partial")]
    h.assert_clean("nb-1")


def test_a_restart_with_a_run_whose_code_never_came_drops_it_with_a_gap_before_the_restart(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    h.request("r1", code="never")
    h.rec.kernel_restarted("k1")
    h.settle()
    types = h.types()
    assert "run_started" not in types
    assert types.index("recording_gap_started") < types.index("kernel_restarted")
    s = h.state("nb-1")
    assert [g.endedTick is not None for g in s.gaps] == [True] and s.gaps[0].source.value == "kernel"
    h.assert_clean("nb-1")


def test_a_three_megabyte_chart_goes_through_put_blob_and_is_readable_with_its_secret_hidden(h):
    key = "sk-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    h.snapshot("nb-1", [("c1", "plot()")])
    h.kernel()
    h.request("r1", code="plot()")
    h.msg("r1", "execute_input", {"execution_count": 1})
    png = "iVBORw0KGgo" + "A" * (3 * 1024 * 1024)
    h.msg("r1", "display_data", {"data": {"image/png": png, "text/plain": f"<Figure key {key}>"}, "metadata": {}})
    h.finish_msgs("r1", 1)
    h.settle()
    s = h.state("nb-1")
    item = next(c for c in s.notebook.cells if c.cellId == "c1").outputs[0].items[0]
    assert item.sizeBytes > 3 * 1024 * 1024 and "image/png" in item.mimeTypes
    stored = h.service.get_blob(TOKEN, GetBlobRequest(explorationId="nb-1", contentHash=item.contentHash))
    assert b"iVBORw0KGgo" in stored and key.encode() not in stored
    h.assert_clean("nb-1")


def test_queue_overflow_keeps_run_started_and_finished_and_memory_bounded():
    h = Harness(sink_kw={"max_queued_events": 50, "max_pending_blob_bytes": 4 * 1024 * 1024})
    try:
        h.snapshot("nb-1", [("c1", "burst()")])
        h.kernel()
        h.settle(100)
        h.ing.gate = threading.Event()  # the store is slow: the worker is stuck
        h.request("r1", code="burst()")
        h.msg("r1", "execute_input", {"execution_count": 1})
        h.t.advance(10)
        time.sleep(0.05)
        for i in range(3000):
            h.msg("r1", "display_data", {"data": {"text/plain": f"row {i}", "image/png": "A" * 20000}, "metadata": {}})
        assert h.sink._queued <= 60 and h.sink._pending_bytes <= 5 * 1024 * 1024
        h.finish_msgs("r1", 1)
        h.ing.gate.set()
        h.settle()
        assert h.sink.stats["dropped_overflow"] > 2000 or h.sink.stats["reduced_blobs"] > 0
        s = h.state("nb-1")
        r = run_of(s, "r1")
        assert r.status.root == "ok"  # started and finished are never dropped
        assert h.sink.stats["errors"] == 0
        for tick in range(1, h.tip("nb-1")[1] + 1):
            assert isinstance(h.service.state_at(TOKEN, StateAtRequest(
                explorationId="nb-1", branchId=h.tip("nb-1")[0], tick=tick)), RebuildOk)
        assert not h.sink.refused
    finally:
        h.close()


def test_a_three_minute_run_with_silent_stretches_is_recorded_whole(h):
    h.snapshot("nb-1", [("c1", "fit()")])
    h.kernel()
    h.request("r1", code="fit()")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "epoch 0\n"})
    for epoch in range(1, 4):
        h.t.advance(60_000)  # a minute of silence
        assert h.sink.drain(5)
        assert run_of(h.state("nb-1"), "r1").status.root == "running"
        h.msg("r1", "stream", {"name": "stdout", "text": f"epoch {epoch}\n"})
    h.msg("r1", "display_data", {"data": {"text/plain": "<Fig>", "image/png": "AAAA"}, "metadata": {}})
    h.finish_msgs("r1", 1)
    h.settle()
    s = h.state("nb-1")
    assert run_of(s, "r1").status.root == "ok"
    assert items_of(s, "c1", "r1")[0] == ("stream", "epoch 0\nepoch 1\nepoch 2\nepoch 3\n")
    assert len(items_of(s, "c1", "r1")) == 2
    assert not s.gaps
    h.assert_clean("nb-1")


def test_the_kernel_starts_before_the_first_snapshot_and_everything_is_parked_then_applied_in_order(h):
    h.kernel()
    h.request("r1", cell="c1", code="a")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "early\n"})
    h.finish_msgs("r1", 1)
    h.t.advance(3000)  # no history yet: the kernel event is parked and the run waits for its code
    assert h.events.exploration("nb-1") is None
    h.snapshot("nb-1", [("c1", "a")])  # the browser's first snapshot arrives
    h.settle(2500)
    assert h.types() == ["notebook_snapshot", "kernel_started", "run_started", "output_added", "run_finished"]
    s = h.state("nb-1")
    assert s.kernel.status.value == "running" and run_of(s, "r1").status.root == "ok"
    h.assert_clean("nb-1")


def test_unknown_parent_messages_before_run_requested_are_buffered_and_used(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    h.msg("late", "execute_input", {"execution_count": 1})  # the VS Code way: messages first
    h.msg("late", "stream", {"name": "stdout", "text": "first\n"})
    h.finish_msgs("late", 1)
    h.t.advance(800)
    h.request("late", code="a")
    h.settle()
    s = h.state("nb-1")
    assert items_of(s, "c1", "late") == [("stream", "first\n")] and run_of(s, "late").status.root == "ok"
    assert "recording_gap_started" not in h.types()
    h.assert_clean("nb-1")


def test_an_execution_nobody_asked_about_is_one_gap_and_the_notebook_still_rebuilds(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    for i in range(3):  # a script run elsewhere
        h.msg(f"x{i}", "execute_input", {"execution_count": i + 1})
        h.msg(f"x{i}", "stream", {"name": "stdout", "text": "who\n"})
    h.settle(3000)
    assert h.types().count("recording_gap_started") == 1
    h.assert_clean("nb-1")


def test_a_run_whose_alias_maps_a_temporary_cell_id_to_a_blaym_id(h):
    h.snapshot("nb-1", [("blaym-7", "a")])
    h.reg.aliases["tmp-4.4-1"] = "blaym-7"
    h.kernel()
    h.request("r1", cell="tmp-4.4-1", code="a")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "ok\n"})
    h.finish_msgs("r1", 1)
    h.settle()
    s = h.state("nb-1")
    assert run_of(s, "r1").cellId == "blaym-7"
    assert items_of(s, "blaym-7", "r1") == [("stream", "ok\n")]
    h.assert_clean("nb-1")


def test_two_kernels_of_one_notebook_do_not_hold_each_other_up(h):
    h.snapshot("nb-1", [("c1", "a"), ("c2", "b")])
    h.kernel("k1")
    h.kernel("k2")
    h.request("A", cell="c1", code="never", kernel="k1")  # k1's head is stuck waiting for code
    h.request("B", cell="c2", code="b", kernel="k2")
    h.msg("B", "execute_input", {"execution_count": 1}, kernel="k2")
    h.finish_msgs("B", 1, kernel="k2")
    h.t.advance(1000)
    assert h.sink.drain(5)
    s = h.state("nb-1")
    assert [r.runId for r in s.runs] == ["B"]
    h.t.advance(6000)
    assert h.sink.drain(5)
    assert len(h.state("nb-1").gaps) == 1


def test_kernel_gone_closes_a_started_run_as_interrupted(h):
    h.snapshot("nb-1", [("c1", "a")])
    h.kernel()
    h.request("r1", code="a")
    h.msg("r1", "execute_input", {"execution_count": 1})
    h.msg("r1", "stream", {"name": "stdout", "text": "going\n"})
    h.rec.kernel_gone("k1")
    h.settle()
    s = h.state("nb-1")
    assert run_of(s, "r1").status.root == "interrupted"
    assert items_of(s, "c1", "r1") == [("stream", "going\n")]
    h.assert_clean("nb-1")


def test_a_snapshot_waits_for_the_run_to_end_and_the_idle_hook_releases_it():
    """S5-T end to end: the service refuses a snapshot while a run runs; the sink defers it and
    the run recorder's idle report puts it through."""
    h = Harness()
    try:
        h.snapshot("nb-1", [("c1", "a")])
        h.kernel()
        h.request("r1", code="a")
        h.msg("r1", "execute_input", {"execution_count": 1})
        h.settle(200)
        snap = h._ev("nb-1", "changed_outside_editor", {"path": "nb.ipynb", "notebookMetadata": {}, "kernelName": "python3", "language": "python", "cells": [
            {"cellId": "c1", "cellType": "code", "code": "changed", "revision": 2, "metadata": {}, "outputs": []}]})
        snap["authorHint"] = "outside"
        from blaym_jupyter.recording.sink import PendingWrite
        h.sink.submit(PendingWrite("nb-1", [snap]))
        assert h.sink.drain(5)
        assert "changed_outside_editor" not in h.types()  # refused while the run is running
        assert h.sink.stats["deferred"] == 1
        h.finish_msgs("r1", 1)
        h.settle()
        assert h.sink.drain(5)
        assert h.types()[-2:] == ["run_finished", "changed_outside_editor"]
    finally:
        h.close()


# -- generated sequences against an oracle -------------------------------------------------------

OPS = st.one_of(
    st.tuples(st.just("stream"), st.sampled_from(["stdout", "stderr"]), st.text("abc \n", min_size=1, max_size=6)),
    st.tuples(st.just("display"), st.one_of(st.none(), st.sampled_from(["d1", "d2"])), st.text("xyz", min_size=1, max_size=5)),
    st.tuples(st.just("update"), st.sampled_from(["d1", "d2"]), st.text("uvw", min_size=1, max_size=5)),
    st.tuples(st.just("clear"), st.booleans(), st.just("")),
    st.tuples(st.just("result"), st.just(None), st.text("123", min_size=1, max_size=4)),
)


@dataclass
class Step:
    kernel: int
    cell: int
    mode: str  # before | after | never
    ops: list
    end: str  # ok | error | interrupt | abort | restart
    restart_after: bool


STEPS = st.builds(
    Step, st.integers(0, 1), st.integers(0, 2), st.sampled_from(["before", "after", "never"]),
    st.lists(OPS, max_size=7), st.sampled_from(["ok", "error", "interrupt", "abort", "restart"]), st.booleans())


def oracle_outputs(ops, error=None):
    """Jupyter's output area as a plain list: the oracle for what a run leaves in its cell."""
    items: list[dict] = []
    pending = False
    seq = list(ops) + ([("error", None, error)] if error else [])
    for kind, a, b in seq:
        if kind == "stream":
            if items and items[-1]["k"] == "stream" and items[-1]["name"] == a and not pending:
                items[-1]["text"] += b
                continue
            if pending:
                items.clear()
                pending = False
            items.append({"k": "stream", "name": a, "text": b})
        elif kind in ("display", "result", "error"):
            if pending:
                items.clear()
                pending = False
            items.append({"k": {"display": "display_data", "result": "execute_result", "error": "error"}[kind],
                          "id": a if kind == "display" else None, "text": b})
        elif kind == "update":
            for it in items:
                if it.get("id") == a:
                    it["text"] = b
        elif kind == "clear":
            if a:
                pending = True
            else:
                items.clear()
                pending = False
    return [("stream" if i["k"] == "stream" else i["k"], i["text"]) for i in items]


def send_ops(h, run_id, ops, kernel):
    for kind, a, b in ops:
        if kind == "stream":
            h.msg(run_id, "stream", {"name": a, "text": b}, kernel)
        elif kind == "display":
            content = {"data": {"text/plain": b}, "metadata": {}}
            if a:
                content["transient"] = {"display_id": a}
            h.msg(run_id, "display_data", content, kernel)
        elif kind == "update":
            h.msg(run_id, "update_display_data", {"data": {"text/plain": b}, "metadata": {}, "transient": {"display_id": a}}, kernel)
        elif kind == "clear":
            h.msg(run_id, "clear_output", {"wait": a}, kernel)
        elif kind == "result":
            h.msg(run_id, "execute_result", {"data": {"text/plain": b}, "metadata": {}, "execution_count": 1}, kernel)
        h.t.advance(20)


@dataclass
class Expect:
    runs: list = field(default_factory=list)  # (run_id, cell, revision, count, status, outputs)
    gaps: int = 0
    restarts: int = 0
    code: dict = field(default_factory=dict)
    revision: dict = field(default_factory=dict)


def play(h, steps):
    exps = ["nb-0", "nb-1"]
    expect = {e: Expect() for e in exps}
    counts = [0, 0]
    for e in exps:
        h.snapshot(e, [(f"c{i}", "init") for i in range(3)])
        for i in range(3):
            expect[e].code[f"c{i}"], expect[e].revision[f"c{i}"] = "init", 1
    for k, e in enumerate(exps):
        h.kernel(f"k{k}", e)
    for n, st_ in enumerate(steps):
        k, e, cell = st_.kernel, exps[st_.kernel], f"c{st_.cell}"
        kn = f"k{k}"
        run_id, code = f"run-{n}", f"code-{n}"
        counts[k] += 1
        exp = expect[e]
        mid = st_.end == "restart"

        def deliver_edit():
            rev = h.edit(e, cell, code)
            exp.code[cell], exp.revision[cell] = code, rev

        if st_.mode == "before":
            deliver_edit()
            h.t.advance(10)
        h.rec.run_requested(kn, e, run_id, cell, code)
        h.msg(run_id, "execute_input", {"execution_count": counts[k]}, kn)
        send_ops(h, run_id, st_.ops, kn)
        error = None
        if st_.end == "error":
            error = f"boom {n}"
            h.msg(run_id, "error", {"ename": "ValueError", "evalue": error, "traceback": [f"ValueError: {error}"]}, kn)
        elif st_.end == "interrupt":
            error = "KeyboardInterrupt"
            h.msg(run_id, "error", {"ename": "KeyboardInterrupt", "evalue": "", "traceback": ["KeyboardInterrupt: "]}, kn)
        if not mid:
            h.finish_msgs(run_id, counts[k], {"ok": "ok", "error": "error", "interrupt": "error", "abort": "abort"}[st_.end], kn)
        if st_.mode == "after" and not mid:
            h.t.advance(250)
            deliver_edit()
        if mid:
            h.rec.kernel_restarted(kn)
            exp.restarts += 1
            counts[k] = 0
            if st_.mode == "after":
                deliver_edit()  # lands after the restart: the run was already dropped
        h.settle(6000 if st_.mode == "never" else 400)
        linked = st_.mode == "before" or (st_.mode == "after" and not mid)
        if linked:
            status = {"ok": "ok", "error": "error", "interrupt": "interrupted", "abort": "interrupted",
                      "restart": "unfinished"}[st_.end]
            err_ops = [("error", None, f"ValueError: {error}")] if st_.end == "error" else \
                [("error", None, "KeyboardInterrupt: ")] if st_.end == "interrupt" else []
            outputs = oracle_outputs(list(st_.ops) + err_ops)
            exp.runs.append((run_id, cell, exp.revision[cell], counts[k] if not mid else counts_before(steps, n, k),
                             status, outputs))
        else:
            exp.gaps += 1
        if st_.restart_after and not mid:
            h.rec.kernel_restarted(kn)
            exp.restarts += 1
            counts[k] = 0
            h.settle(100)
    return expect


def counts_before(steps, n, k):
    """The kernel's count of the run at step `n` that ended in a restart: its position since the last restart."""
    count = 0
    for i, s in enumerate(steps[: n + 1]):
        if s.kernel != k:
            continue
        count += 1
        if i < n and (s.end == "restart" or s.restart_after):
            count = 0
    return count


def check(h, expect):
    for e, exp in expect.items():
        state = h.state(e)
        assert [r.runId for r in state.runs] == [r[0] for r in exp.runs], e
        for run_id, cell, rev, count, status, outputs in exp.runs:
            r = run_of(state, run_id)
            assert (r.cellId, r.cellRevision, r.runCount, r.status.root) == (cell, rev, count, status), run_id
            assert items_of(state, cell, run_id) == outputs, run_id
        cells = {c.cellId: c for c in state.notebook.cells}
        for cell, code in exp.code.items():
            assert (cells[cell].code, cells[cell].revision) == (code, exp.revision[cell])
        assert len(state.gaps) == exp.gaps and all(g.endedTick is not None for g in state.gaps)
        assert (state.kernel.lastRestart is not None) == bool(exp.restarts)
        types = [ev.type for ev in h.stored(e)]
        assert types.count("kernel_restarted") == exp.restarts
        started = [run_of(state, r[0]).startedTick for r in exp.runs]
        assert started == sorted(started)  # ticks keep request order
    h.assert_clean(*expect)


@settings(max_examples=300 if DEEP else 60, deadline=None, print_blob=True,
          suppress_health_check=list(HealthCheck))
@given(st.lists(STEPS, min_size=1, max_size=8))
def test_generated_kernel_sequences_match_the_oracle(steps):
    h = Harness()
    try:
        check(h, play(h, steps))
    finally:
        h.close()


def test_oracle_itself_on_a_known_sequence():
    ops = [("stream", "stdout", "a"), ("stream", "stdout", "b"), ("display", "d1", "x"), ("stream", "stdout", "c"),
           ("update", "d1", "y"), ("clear", True, ""), ("stream", "stderr", "e")]
    assert oracle_outputs(ops) == [("stream", "e")]
    assert oracle_outputs(ops[:5]) == [("stream", "ab"), ("display_data", "y"), ("stream", "c")]

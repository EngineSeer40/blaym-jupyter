"""Reviewer additions (S5 Tasks 6 and 7): late IOPub, restart races, parked order, deferral at close,
log content, threads, and a reference model of the active-instance rule.

Tests marked `PROD-BUG` are xfail on purpose: they show a production problem (see the review report).
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from blaym_jupyter.recording.registry import DEFAULT_ALIVE_WINDOW_MS, Registry
from blaym_jupyter.recording.runs import RunRecorder, RunRecorderSettings
from blaym_jupyter.recording.sink import OrderedSink, PendingWrite
from blaym_jupyter.tests.recording.test_registry import Counter, registration
from blaym_jupyter.tests.recording.test_runs import EXP, FakeIngestor, FakeRegistry, FakeTime, ListSink, Rig, UuidIds, finished
from blaym_jupyter.tests.recording.test_sink import FakeIngestor as SinkIngestor, event, wait_for
from blaym_jupyter.testing.fakes import FakeClock

WINDOW = DEFAULT_ALIVE_WINDOW_MS


# -- Task 7 (a): IOPub output after the shell reply --------------------------------------------------

def test_output_and_idle_arriving_after_execute_reply_are_all_recorded_before_run_finished():
    r = Rig()
    r.code("c1", 1, "x")
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "execute_reply", {"status": "ok", "execution_count": 1})  # shell is faster than IOPub
    r.t.advance(400)
    r.msg("r1", "stream", {"name": "stdout", "text": "late a\n"})
    r.msg("r1", "display_data", {"data": {"text/plain": "<Fig>"}, "metadata": {}})
    r.t.advance(300)
    r.msg("r1", "stream", {"name": "stdout", "text": "late b\n"})
    r.msg("r1", "status", {"execution_state": "idle"})
    r.t.advance(300)
    types = r.types()
    assert types.index("run_finished") == len(types) - 1
    assert types.count("output_added") == 3 and not r.rec.stats["late_messages"]


def test_output_after_the_grace_time_is_marked_by_one_closed_gap_and_not_written():
    r = Rig(reply_grace_ms=1000)
    r.code("c1", 1, "x")
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "execute_reply", {"status": "ok"})
    r.t.advance(1500)  # IOPub is slow (a big image); idle has not come
    r.msg("r1", "stream", {"name": "stdout", "text": "late\n"})
    r.msg("r1", "display_data", {"data": {"text/plain": "more"}, "metadata": {}})
    r.msg("r1", "status", {"execution_state": "idle"})
    r.t.advance(300)
    assert "output_added" not in r.types()
    assert r.types().count("recording_gap_started") == 1 and r.types().count("recording_gap_ended") == 1
    assert r.rec.stats["late_output_gaps"] == 1


def test_output_within_the_default_grace_after_the_reply_is_recorded_normally():
    r = Rig()
    r.code("c1", 1, "x")
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "execute_reply", {"status": "ok"})
    r.t.advance(8000)
    r.msg("r1", "stream", {"name": "stdout", "text": "slow\n"})
    r.msg("r1", "status", {"execution_state": "idle"})
    r.t.advance(300)
    assert r.types() == ["run_started", "output_added", "run_finished"]


# -- Task 7 (d): restart racing a run --------------------------------------------------------------

def test_messages_of_an_executing_run_after_a_restart_do_not_raise_duplicate_or_reorder():
    r = Rig()
    r.code("c1", 1, "x")
    r.start_kernel()
    r.request("r1", code="x")
    r.msg("r1", "execute_input", {"execution_count": 1})
    r.msg("r1", "stream", {"name": "stdout", "text": "a"})
    r.rec.kernel_restarted("k1", reason="restart")
    r.msg("r1", "execute_reply", {"status": "ok"})  # a late echo from the dead kernel
    r.msg("r1", "status", {"execution_state": "idle"})
    r.t.advance(5000)
    types = r.types()
    assert types.count("run_started") == 1 and types.count("run_finished") <= 1
    assert types.index("kernel_restarted") == len(types) - 1 or types[-1] == "kernel_restarted"
    # a new run after the restart starts cleanly, counts restart from the kernel's own number
    r.code("c1", 2, "y")
    r.request("r2", code="y")
    r.execute("r2", 1)
    assert finished(r, "r2")[0]["payload"]["status"] == "ok"


# -- Task 7 (i): logs ids and reasons only ---------------------------------------------------------

def test_logs_of_a_dropped_run_and_a_bad_message_carry_no_code_or_output(caplog):
    caplog.set_level(logging.DEBUG, logger="blaym_jupyter.recording")
    r = Rig()
    r.request("r1", code="api_key = 'sk-SECRET-123456'")
    r.msg("r1", "stream", {"name": "stdout", "text": "token=sk-SECRET-123456"})
    r.t.advance(6000)
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "dropped" in text and "SECRET" not in text


# -- Task 7 (j): two threads and a real timer ---------------------------------------------------------

class _RealTime:
    def now_ms(self):
        return int(time.monotonic() * 1000)

    def call_later(self, ms, fn):
        t = threading.Timer(ms / 1000, fn)
        t.daemon = True
        t.start()
        return t.cancel


def test_requests_from_one_thread_kernel_messages_from_another_and_timer_callbacks_stay_consistent():
    ing, sink = FakeIngestor(), ListSink()
    clock = _RealTime()
    rec = RunRecorder(ing, sink, clock, clock, UuidIds(), registry=FakeRegistry(),
                      settings=RunRecorderSettings(flush_ms=5, reply_grace_ms=50, poll_ms=5))
    n = 25
    errors: list[BaseException] = []
    for i in range(n):
        ing.set(f"c{i}", 1, f"code{i}")

    def requests():
        try:
            for i in range(n):
                rec.run_requested("k", EXP, f"r{i}", f"c{i}", f"code{i}")
                time.sleep(0.001)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def kernel():
        try:
            for i in range(n):
                rec.kernel_message("k", f"r{i}", "execute_input", {"execution_count": i + 1})
                for j in range(5):
                    rec.kernel_message("k", f"r{i}", "stream", {"name": "stdout", "text": f"{i}-{j}\n"})
                    rec.kernel_message("k", f"r{i}", "display_data", {"data": {"text/plain": str(j)}, "metadata": {}})
                rec.kernel_message("k", f"r{i}", "execute_reply", {"status": "ok"})
                rec.kernel_message("k", f"r{i}", "status", {"execution_state": "idle"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    a, b = threading.Thread(target=requests), threading.Thread(target=kernel)
    a.start(); b.start(); a.join(); b.join()
    assert wait_for(lambda: len(sink.events("run_finished")) == n, 10)
    assert not errors
    seq = [(e["type"], e["payload"].get("runId")) for e in sink.events()]
    for i in range(n):
        mine = [t for t, rid in seq if rid == f"r{i}"]
        assert mine[0] == "run_started" and mine[-1] == "run_finished" and mine.count("run_finished") == 1
        assert mine.count("output_added") == 6 or mine.count("output_added") + mine.count("output_replaced") >= 6
    starts = [rid for t, rid in seq if t == "run_started"]
    assert starts == [f"r{i}" for i in range(n)]  # the lane keeps request order
    # a run is finished before the next one starts
    open_runs = 0
    for t, _ in seq:
        open_runs += t == "run_started"
        open_runs -= t == "run_finished"
        assert 0 <= open_runs <= 1
    rec.close()


# -- Task 7 (g), (h): the sink ---------------------------------------------------------------------

def test_parked_events_keep_submit_order_across_batches_and_late_submits_and_are_not_duplicated():
    ing, clock = SinkIngestor(), FakeTime()
    ready = {"on": False}
    ing.decide = lambda ev: "accepted" if ready["on"] else "bad_first_event"
    sink = OrderedSink(ing, clock, poll_s=0.002, batch_size=7)
    try:
        sent = []
        for i in range(30):
            e = event("kernel_started"); sent.append(e["eventId"])
            sink.submit(PendingWrite("e1", [e]))
        for _ in range(3):
            time.sleep(0.03); clock.advance(1000)
        for i in range(30):  # more arrive while parked
            e = event("kernel_started"); sent.append(e["eventId"])
            sink.submit(PendingWrite("e1", [e]))
        ready["on"] = True
        clock.advance(1000)
        assert wait_for(lambda: len(ing.accepted) == 60)
        assert [d["eventId"] for d in ing.accepted] == sent
    finally:
        sink.close(1.0)


def test_a_run_in_progress_event_still_deferred_at_close_is_reported_not_silently_lost():
    ing, clock = SinkIngestor(), FakeTime()
    ing.decide = lambda ev: "run_in_progress" if ev["type"] == "kernel_started" else "accepted"
    sink = OrderedSink(ing, clock, poll_s=0.002)
    sink.submit(PendingWrite("e1", [event("kernel_started")]))
    assert wait_for(lambda: sink.stats["deferred"] == 1)
    report = sink.close(1.0)
    assert report.unwritten == 1 and report.by_exploration == {"e1": 1}


def test_deferred_event_released_by_on_idle_goes_behind_run_finished():
    ing, clock = SinkIngestor(), FakeTime()
    once = {"n": 0}

    def decide(ev):
        if ev["type"] == "kernel_started" and once["n"] == 0:
            once["n"] = 1
            return "run_in_progress"
        return "accepted"

    ing.decide = decide
    sink = OrderedSink(ing, clock, poll_s=0.002)
    try:
        sink.submit(PendingWrite("e1", [event("kernel_started")]))
        assert wait_for(lambda: sink.stats["deferred"] == 1)
        sink.submit(PendingWrite("e1", [event("run_finished")]))
        assert wait_for(lambda: ing.accepted_types() == ["run_finished"])
        sink.on_idle("e1")
        assert wait_for(lambda: ing.accepted_types() == ["run_finished", "kernel_started"])
    finally:
        sink.close(1.0)


# -- Task 6: reference model of the active-instance rule ---------------------------------------------

MODEL_OPS = st.lists(st.one_of(
    st.tuples(st.just("register"), st.sampled_from(["i1", "i2", "i3"]), st.booleans()),
    st.tuples(st.just("heartbeat"), st.sampled_from(["i1", "i2", "i3"])),
    st.tuples(st.just("close"), st.sampled_from(["i1", "i2", "i3"])),
    st.tuples(st.just("wait"), st.sampled_from([1_000, WINDOW // 2, WINDOW + 1])),
), max_size=60)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(MODEL_OPS)
def test_active_instance_matches_a_simple_reference_model(tmp_path_factory, ops):
    from blaym_jupyter.recording.registry_store import FolderRegistryStore
    clock = FakeClock()
    reg = Registry(FolderRegistryStore(tmp_path_factory.mktemp("m")), clock, Counter(), path_exists=lambda p: True)
    nb = None
    holder, seen = None, {}
    for op in ops:
        if op[0] == "wait":
            clock._ms += op[1]
            continue
        inst = op[1]
        now = clock.now_ms()
        if op[0] == "register":
            s = reg.register(registration(instance=inst, takeover=op[2]))
            nb = s.notebookId
            seen[inst] = now
            if holder is None or op[2] or now - seen.get(holder, -10**12) > WINDOW or holder == inst:
                holder = inst
            # (the model: a holder that was heard within the window keeps the notebook)
        elif nb is None:
            continue
        elif op[0] == "heartbeat":
            if inst not in seen:
                assert reg.status(nb, inst).needsRegister
                continue
            prev_seen = dict(seen)
            reg.status(nb, inst)
            seen[inst] = now
            if holder is None or holder == inst or now - prev_seen.get(holder, -10**12) > WINDOW:
                holder = inst
        else:
            reg.close(nb, inst)
            seen.pop(inst, None)
            if holder == inst:
                holder = None
        if nb is not None:
            assert [i for i in ("i1", "i2", "i3") if reg.gate_ingest(i, nb) == "ok"] == ([holder] if holder else [])

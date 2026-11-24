"""The ordered sink (S5, Task 7): ordering, blobs before events, refusals, parking and bounds."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid

import pytest

from blaym_jupyter.recording.sink import OrderedSink, PendingWrite
from blaym_jupyter.tests.recording.test_runs import FakeTime
from blaym_jupyter_types import IngestAccepted, IngestRefused, IngestResponse, PutBlobResponse

NS = uuid.UUID("5c1f0a6e-0000-4000-8000-00000000c0c0")


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def hash_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_n = 0


def event(type_="kernel_started", exploration="e1", payload=None, **extra):
    global _n
    _n += 1
    payloads = {
        "kernel_started": {"kernelId": "k", "kernelName": "python3", "language": "python"},
        "run_started": {"runId": "r1", "cellId": "c1", "cellRevision": 1, "runCount": 1},
        "run_finished": {"runId": "r1", "status": "ok"},
        "output_added": {"runId": "r1", "cellId": "c1", "outputIndex": 0, "item": item("x")},
        "output_replaced": {"runId": "r1", "cellId": "c1", "outputIndex": 0, "item": item("x")},
    }
    return {
        "schemaVersion": 2, "eventId": str(uuid.uuid5(NS, str(_n))), "explorationId": exploration,
        "wallTimeMs": 1_770_000_000_000, "userId": "local", "workspaceId": "local", "originId": "o",
        "type": type_, "payload": payload or payloads[type_], **extra,
    }


def item(text, content_hash=None, truncated=False, size=None):
    raw = text.encode()
    return {"outputType": "stream", "mimeTypes": ["text/plain"], "contentHash": content_hash or hash_of(raw),
            "preview": text, "truncated": truncated, "sizeBytes": len(raw) if size is None else size}


def blob_event(data: bytes, preview="[image/png]", type_="output_added"):
    provisional = hash_of(data)
    it = {"outputType": "display_data", "mimeTypes": ["image/png"], "contentHash": provisional,
          "preview": preview, "truncated": True, "sizeBytes": len(data)}
    ev = event(type_, payload={"runId": "r1", "cellId": "c1", "outputIndex": 0, "item": it})
    return ev, {provisional: data}


class FakeIngestor:
    """Records every call in order. `decide(event) -> 'accepted' | refusal reason`."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.accepted: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.decide = lambda ev: "accepted"
        self.on_put = None  # hook(data) -> may raise or block
        self.fail_ingest = 0
        self.lock = threading.Lock()
        self.stored_prefix = "stored-"

    def put_blob(self, exploration_id, data):
        if self.on_put:
            self.on_put(data)
        h = self.stored_prefix + hash_of(data)[7:]  # the service's hash differs from the provisional one
        with self.lock:
            self.calls.append(("put_blob", exploration_id, h))
            self.blobs[h] = data
        return PutBlobResponse(contentHash=h, originalBytes=len(data), storedBytes=len(data), truncated=False)

    def ingest(self, request):
        with self.lock:
            if self.fail_ingest:
                self.fail_ingest -= 1
                raise RuntimeError("busy")
        outcomes = []
        for e in request.events:
            d = e.model_dump(mode="json", exclude_none=True)
            self.calls.append(("ingest", d["eventId"], d["type"]))
            verdict = self.decide(d)
            if verdict == "accepted":
                self.accepted.append(d)
                outcomes.append(IngestAccepted(status="accepted", eventId=d["eventId"], branchId="b", tick=len(self.accepted),
                                               duplicate=False))
            else:
                outcomes.append(IngestRefused(status="refused", eventId=d["eventId"], reason=verdict,
                                              detail="SECRET DETAIL print(api_key)"))
        return IngestResponse(apiVersion=1, outcomes=outcomes)

    def accepted_types(self):
        return [d["type"] for d in self.accepted]


@pytest.fixture
def rig():
    ing, clock = FakeIngestor(), FakeTime()
    sink = OrderedSink(ing, clock, poll_s=0.005)
    yield ing, clock, sink
    sink.close(1.0)


def test_events_of_one_exploration_are_written_in_submit_order(rig):
    ing, _, sink = rig
    evs = [event("kernel_started"), event("run_started"), event("run_finished")]
    for e in evs:
        sink.submit(PendingWrite("e1", [e]))
    assert sink.drain()
    assert [d["eventId"] for d in ing.accepted] == [e["eventId"] for e in evs]
    assert sink.stats["written"] == 3


def test_a_write_with_several_events_keeps_their_order(rig):
    ing, _, sink = rig
    evs = [event("kernel_started"), event("run_started"), event("run_finished")]
    sink.submit(PendingWrite("e1", evs))
    assert sink.drain()
    assert ing.accepted_types() == ["kernel_started", "run_started", "run_finished"]


def test_put_blob_comes_before_the_ingest_that_uses_it_and_the_hash_is_the_one_put_blob_answered(rig):
    ing, _, sink = rig
    ev, blobs = blob_event(b"\x89PNG" + b"x" * 100)
    sink.submit(PendingWrite("e1", [ev], blobs))
    assert sink.drain()
    kinds = [c[0] for c in ing.calls]
    assert kinds == ["put_blob", "ingest"]  # a fault that puts the blob after the event fails here
    sent = ing.accepted[0]["payload"]["item"]["contentHash"]
    assert sent == ing.calls[0][2] and sent != ev["payload"]["item"]["contentHash"]
    assert ev["payload"]["item"]["contentHash"] == hash_of(b"\x89PNG" + b"x" * 100)  # the caller's event is not changed


def test_an_event_without_a_blob_keeps_its_hash_and_makes_no_put_blob(rig):
    ing, _, sink = rig
    ev = event("output_added")
    sink.submit(PendingWrite("e1", [ev]))
    assert sink.drain()
    assert [c[0] for c in ing.calls] == ["ingest"]
    assert ing.accepted[0]["payload"]["item"]["contentHash"] == ev["payload"]["item"]["contentHash"]


def test_the_same_blob_in_one_batch_is_put_once(rig):
    ing, _, sink = rig
    ev1, b = blob_event(b"same")
    ev2, _ = blob_event(b"same", type_="output_replaced")
    sink.submit(PendingWrite("e1", [ev1, ev2], b))
    assert sink.drain()
    assert sum(1 for c in ing.calls if c[0] == "put_blob") <= 2  # one per batch of events with blobs
    assert all(d["payload"]["item"]["contentHash"].startswith("stored-") for d in ing.accepted)


def test_a_refused_output_event_is_counted_logged_without_content_and_never_retried_and_later_events_go_on(rig, caplog):
    ing, _, sink = rig
    bad = event("output_added")
    ing.decide = lambda d: "invalid_event" if d["eventId"] == bad["eventId"] else "accepted"
    caplog.set_level(logging.DEBUG, logger="blaym_jupyter.recording.sink")
    sink.submit(PendingWrite("e1", [event("run_started"), bad, event("run_finished")]))
    assert sink.drain()
    assert ing.accepted_types() == ["run_started", "run_finished"]
    assert sink.refused["invalid_event"] == 1
    assert [c for c in ing.calls if c[1] == bad["eventId"]] == [("ingest", bad["eventId"], "output_added")]  # once
    text = " ".join(r.getMessage() for r in caplog.records)
    assert bad["eventId"] in text and "invalid_event" in text
    assert "SECRET" not in text and "api_key" not in text


def test_missing_blob_puts_the_blob_again_once_then_gives_up(rig):
    ing, _, sink = rig
    ev, blobs = blob_event(b"chart" * 10)
    ing.decide = lambda d: "missing_blob" if d["eventId"] == ev["eventId"] else "accepted"
    sink.submit(PendingWrite("e1", [ev, event("run_finished")], blobs))
    assert sink.drain()
    puts = [c for c in ing.calls if c[0] == "put_blob"]
    ingests = [c for c in ing.calls if c[0] == "ingest" and c[1] == ev["eventId"]]
    assert len(puts) == 2 and len(ingests) == 2  # one retry, no more
    assert sink.refused["missing_blob"] == 1 and sink.stats["blob_retries"] == 1
    assert ing.accepted_types() == ["run_finished"]  # the event behind it is not lost


def test_missing_blob_that_succeeds_on_the_second_try_keeps_the_order(rig):
    ing, _, sink = rig
    ev, blobs = blob_event(b"chart" * 10)
    seen = {"n": 0}

    def decide(d):
        if d["eventId"] == ev["eventId"]:
            seen["n"] += 1
            return "missing_blob" if seen["n"] == 1 else "accepted"
        return "accepted"

    ing.decide = decide
    sink.submit(PendingWrite("e1", [event("run_started"), ev, event("run_finished")], blobs))
    assert sink.drain()
    assert ing.accepted_types() == ["run_started", "output_added", "run_finished"]


def test_bad_first_event_parks_the_events_in_order_and_retries_every_second_until_accepted(rig):
    ing, clock, sink = rig
    ing.decide = lambda d: "bad_first_event"
    first = [event("kernel_started"), event("run_started"), event("run_finished")]
    sink.submit(PendingWrite("e1", first))
    assert wait_for(lambda: sink.stats["park_retries"] >= 1)
    sink.submit(PendingWrite("e1", [event("kernel_started")]))  # arrives while parked: queued behind
    clock.advance(500)
    time.sleep(0.05)
    assert sink.stats["park_retries"] == 1  # not before a second
    ing.decide = lambda d: "accepted"  # the browser's snapshot arrived
    clock.advance(600)
    assert sink.drain()
    assert ing.accepted_types() == ["kernel_started", "run_started", "run_finished", "kernel_started"]
    assert [d["eventId"] for d in ing.accepted[:3]] == [e["eventId"] for e in first]


def test_a_parked_exploration_does_not_hold_up_another_one(rig):
    ing, _, sink = rig
    ing.decide = lambda d: "bad_first_event" if d["explorationId"] == "e1" else "accepted"
    sink.submit(PendingWrite("e1", [event("kernel_started", "e1")]))
    sink.submit(PendingWrite("e2", [event("kernel_started", "e2")]))
    assert wait_for(lambda: len(ing.accepted) == 1)
    assert ing.accepted[0]["explorationId"] == "e2"


def test_a_refused_first_event_never_loses_the_later_events(rig):
    ing, clock, sink = rig
    state = {"history": False}

    def decide(d):
        return "accepted" if state["history"] else "bad_first_event"

    ing.decide = decide
    evs = [event("kernel_started")] + [event("run_started")] + [event("run_finished")]
    for e in evs:
        sink.submit(PendingWrite("e1", [e]))
    time.sleep(0.05)
    assert not ing.accepted
    state["history"] = True
    clock.advance(1500)
    assert sink.drain()
    assert [d["eventId"] for d in ing.accepted] == [e["eventId"] for e in evs]


def test_the_park_is_bounded_by_500_events(rig):
    ing, clock, sink = rig
    ing.decide = lambda d: "bad_first_event"
    for i in range(1200):
        sink.submit(PendingWrite("e1", [event("kernel_started")]))
    assert wait_for(lambda: sink.stats["parked_dropped"] >= 400 and sink._queued <= 600, 10)


def test_the_park_is_bounded_by_ten_minutes(rig):
    ing, clock, sink = rig
    ing.decide = lambda d: "bad_first_event"
    sink.submit(PendingWrite("e1", [event("kernel_started")]))
    assert wait_for(lambda: sink.stats["park_retries"] >= 1)
    for _ in range(11):
        clock.advance(60_000)
        time.sleep(0.03)
    assert wait_for(lambda: sink.stats["parked_dropped"] == 1)
    assert sink.drain()


def test_run_in_progress_defers_only_that_event_and_on_idle_retries_it(rig):
    ing, _, sink = rig
    outside = event("kernel_started")
    ing.decide = lambda d: "run_in_progress" if d["eventId"] == outside["eventId"] else "accepted"
    sink.submit(PendingWrite("e1", [outside, event("run_started"), event("run_finished")]))
    assert sink.drain()  # the events behind it are not held up (they hold the run_finished it waits for)
    assert ing.accepted_types() == ["run_started", "run_finished"]
    assert sink.close(0.5).unwritten == 1  # the deferred event is reported if nobody retried it
    ing2, clock2 = FakeIngestor(), FakeTime()
    s2 = OrderedSink(ing2, clock2, poll_s=0.005)
    ing2.decide = lambda d: "run_in_progress" if d["eventId"] == outside["eventId"] else "accepted"
    s2.submit(PendingWrite("e1", [outside]))
    assert wait_for(lambda: s2.stats["deferred"] == 1)
    ing2.decide = lambda d: "accepted"
    s2.on_idle("e1")
    assert s2.drain()
    assert [d["eventId"] for d in ing2.accepted] == [outside["eventId"]]
    s2.close(0.5)


def test_an_ingest_error_backs_off_retries_and_does_not_lose_the_event(rig):
    ing, clock, sink = rig
    ing.fail_ingest = 2
    sink.submit(PendingWrite("e1", [event("run_started")]))
    assert wait_for(lambda: sink.stats["errors"] == 1)
    clock.advance(1100)
    assert wait_for(lambda: sink.stats["errors"] == 2)
    clock.advance(1100)
    assert sink.drain()
    assert ing.accepted_types() == ["run_started"]


def test_an_event_that_keeps_failing_is_dropped_after_the_retries_and_counted(rig):
    ing, clock, sink = rig
    ing.fail_ingest = 10_000
    sink.submit(PendingWrite("e1", [event("run_started")]))
    for _ in range(10):
        clock.advance(1100)
        time.sleep(0.03)
    assert wait_for(lambda: sink.stats["dropped_errors"] == 1)


def test_a_bad_event_in_a_batch_does_not_stop_its_neighbours(rig):
    ing, clock, sink = rig
    bad = {**event("run_started"), "payload": {"nonsense": True}}  # fails the contract's validation
    sink.submit(PendingWrite("e1", [event("kernel_started"), bad, event("run_finished")]))
    for _ in range(10):
        clock.advance(1100)
        time.sleep(0.03)
    assert wait_for(lambda: sink.stats["dropped_errors"] == 1)
    assert sink.drain()
    assert ing.accepted_types() == ["kernel_started", "run_finished"]


# -- bounds --------------------------------------------------------------------------------------

def test_past_the_event_bound_output_events_are_dropped_but_run_started_and_finished_stay():
    ing, clock = FakeIngestor(), FakeTime()
    gate = threading.Event()
    ing.on_put = lambda data: gate.wait(5)  # the worker is stuck: the queue fills
    sink = OrderedSink(ing, clock, max_queued_events=100, poll_s=0.005)
    ev0, b0 = blob_event(b"first")
    sink.submit(PendingWrite("e1", [event("kernel_started"), event("run_started"), ev0], b0))
    assert wait_for(lambda: sink._inflight > 0)
    for i in range(1000):
        sink.submit(PendingWrite("e1", [event("output_added")]))
    sink.submit(PendingWrite("e1", [event("run_finished")]))
    assert sink._queued <= 105
    assert sink.stats["dropped_overflow"] >= 900
    gate.set()
    assert sink.drain()
    types = ing.accepted_types()
    assert types[:2] == ["kernel_started", "run_started"] and types[-1] == "run_finished"
    assert types.count("output_added") <= 101
    sink.close(1.0)


def test_a_run_that_lost_output_to_the_bound_loses_its_later_output_too_so_indexes_stay_consistent():
    ing, clock = FakeIngestor(), FakeTime()
    gate = threading.Event()
    ing.on_put = lambda data: gate.wait(5)
    sink = OrderedSink(ing, clock, max_queued_events=5, poll_s=0.005)
    ev0, b0 = blob_event(b"first")
    sink.submit(PendingWrite("e1", [ev0], b0))
    assert wait_for(lambda: sink._inflight > 0)
    for _ in range(10):
        sink.submit(PendingWrite("e1", [event("kernel_started")]))
    sink.submit(PendingWrite("e1", [event("output_added")]))  # dropped: over the bound
    gate.set()
    assert sink.drain()
    sink.submit(PendingWrite("e1", [event("output_replaced")]))  # same run r1: still dropped
    assert sink.drain()
    assert "output_replaced" not in ing.accepted_types()
    sink.submit(PendingWrite("e1", [event("run_finished")]))  # ends the degraded state
    sink.submit(PendingWrite("e1", [event("output_added")]))
    assert sink.drain()
    assert ing.accepted_types()[-1] == "output_added"
    sink.close(1.0)


def test_pending_blob_bytes_are_bounded_by_reducing_blobs_to_previews():
    ing, clock = FakeIngestor(), FakeTime()
    gate = threading.Event()
    ing.on_put = lambda data: gate.wait(10)
    sink = OrderedSink(ing, clock, max_pending_blob_bytes=10 * 1024 * 1024, poll_s=0.005)
    big = b"B" * (3 * 1024 * 1024)
    for i in range(12):
        data = big + bytes([i])
        ev, blobs = blob_event(data, preview=f"[chart {i}]")
        sink.submit(PendingWrite("e1", [ev], blobs))
    assert sink._pending_bytes <= 10 * 1024 * 1024 + 4096
    assert sink.stats["reduced_blobs"] >= 8
    gate.set()
    assert sink.drain(10)
    assert len(ing.accepted) == 12  # reduced, not dropped
    reduced = [d for d in ing.accepted if d["payload"]["item"]["truncated"] and d["payload"]["item"]["sizeBytes"] > 3_000_000]
    assert len(reduced) == 12
    assert max(len(b) for b in ing.blobs.values() if len(b) < 1_000_000) < 1000
    assert sink._pending_bytes == 0 and sink._queued == 0
    sink.close(1.0)


def test_close_drains_and_reports_what_it_could_not_write():
    ing, clock = FakeIngestor(), FakeTime()
    ing.decide = lambda d: "bad_first_event"
    sink = OrderedSink(ing, clock, poll_s=0.005)
    sink.submit(PendingWrite("e1", [event("kernel_started"), event("run_finished")]))
    assert wait_for(lambda: sink.stats["park_retries"] >= 1)
    started = time.monotonic()
    report = sink.close(0.3)
    assert time.monotonic() - started < 3
    assert report.unwritten == 2 and report.by_exploration == {"e1": 2} and report.run_finished_unwritten == 1
    sink.submit(PendingWrite("e1", [event("kernel_started")]))  # after close: refused quietly, no thread revived
    assert sink.stats["rejected_after_close"] == 1


def test_the_sink_has_one_thread_and_it_stops_on_close():
    before = threading.active_count()
    ing, clock = FakeIngestor(), FakeTime()
    sink = OrderedSink(ing, clock, poll_s=0.005)
    for i in range(50):
        sink.submit(PendingWrite("e%d" % (i % 5), [event("kernel_started", "e%d" % (i % 5))]))
    assert threading.active_count() == before + 1
    assert sink.close(2.0).unwritten == 0
    assert wait_for(lambda: threading.active_count() == before)

"""The ordered sink: ordering, blobs before events, refusals, parking and bounds."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid

import pytest

from blaym_notebook.recording.sink import OrderedSink, PendingWrite
from blaym_notebook.tests.recording.helpers import FakeTime, wait_for
from blaym_notebook_types import IngestAccepted, IngestRefused, IngestResponse, PutBlobResponse

NS = uuid.UUID("5c1f0a6e-0000-4000-8000-00000000c0c0")


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

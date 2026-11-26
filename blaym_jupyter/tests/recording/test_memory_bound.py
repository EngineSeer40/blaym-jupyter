"""Task 21a: a 100 MB output and a thousand small outputs go through the run recorder and the real
ordered sink with bounded memory growth (Python allocations, tracemalloc peak).

Not covered here (they need a real server): the 300-cell notebook and the 200 ms event-loop check.
"""

from __future__ import annotations

import base64
import tracemalloc

from blaym_jupyter.recording.runs import RunRecorder
from blaym_jupyter.recording.sink import OrderedSink
from blaym_jupyter.tests.recording.test_runs import EXP, FakeIngestor, FakeRegistry, FakeTime, UuidIds
from blaym_jupyter.tests.recording.test_sink import FakeIngestor as StoreIngestor

LIMIT = 120 * 1024 * 1024


class DiscardingStore(StoreIngestor):
    """Takes events and blobs and keeps nothing, as a store on disk would."""

    def put_blob(self, exploration_id, data):
        from blaym_jupyter_types import PutBlobResponse
        return PutBlobResponse(contentHash="stored-" + str(len(data)), originalBytes=len(data),
                               storedBytes=len(data), truncated=False)

    def ingest(self, request):
        self.accepted.clear()
        self.calls.clear()
        return super().ingest(request)


def _rig():
    t = FakeTime()
    sink = OrderedSink(DiscardingStore(), t, poll_s=0.005)
    reader = FakeIngestor()
    reader.set("c1", 1, "x")
    rec = RunRecorder(reader, sink, t, t, UuidIds(), registry=FakeRegistry())
    rec.kernel_started("k1", EXP, "kid", "python3", "python")
    return rec, sink, t


def _run(rec, t, run_id, outputs):
    rec.run_requested("k1", EXP, run_id, "c1", "x")
    rec.kernel_message("k1", run_id, "execute_input", {"code": "", "execution_count": 1})
    for msg_type, content in outputs:
        rec.kernel_message("k1", run_id, msg_type, content)
    rec.kernel_message("k1", run_id, "execute_reply", {"status": "ok", "execution_count": 1})
    rec.kernel_message("k1", run_id, "status", {"execution_state": "idle"})
    t.advance(500)


def test_a_100_mb_output_keeps_memory_growth_under_120_mb():
    png = base64.b64encode(b"\x89PNG" + bytes(75 * 1024 * 1024)).decode()  # ~100 MB of text
    rec, sink, t = _rig()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        _run(rec, t, "r1", [("display_data", {"data": {"image/png": png}, "metadata": {}})])
        assert sink.drain(30)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
        sink.close(1.0)
    assert sink.stats["written"] >= 3  # started, the output, finished
    assert peak < LIMIT, peak // (1024 * 1024)


def test_a_thousand_small_outputs_keep_memory_growth_under_120_mb():
    rec, sink, t = _rig()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        outputs = [("display_data", {"data": {"text/plain": f"row {i} " + "x" * 200}, "metadata": {}})
                   for i in range(1000)]
        _run(rec, t, "r1", outputs)
        assert sink.drain(30)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
        sink.close(1.0)
    assert sink.stats["written"] >= 1000
    assert peak < LIMIT, peak // (1024 * 1024)

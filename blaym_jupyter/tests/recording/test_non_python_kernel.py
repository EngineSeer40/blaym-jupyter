"""Task 21a: the run recorder reads only messaging-protocol fields, so an R kernel's recorded
message stream gives the same result as the same stream labelled Python."""

from __future__ import annotations

import base64
import copy

from blaym_jupyter.tests.recording.test_runs import Rig

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


def _record(language: str):
    r = Rig()
    r.code("c1", 1, "plot(1:3)")
    r.rec.kernel_started("k1", "exp-1", "kid-1", language.lower() + "kernel", language)
    r.request("r1", code="plot(1:3)")
    r.msg("r1", "execute_input", {"code": "plot(1:3)", "execution_count": 3})
    r.msg("r1", "stream", {"name": "stdout", "text": "[1] 1 2 3\n"})
    r.msg("r1", "display_data", {"data": {"image/png": PNG, "text/plain": "<IRdisplay>"}, "metadata": {}})
    r.msg("r1", "error", {"ename": "simpleError", "evalue": "boom", "traceback": ["Error: boom"]})
    r.msg("r1", "execute_reply", {"status": "error", "execution_count": 3})
    r.msg("r1", "status", {"execution_state": "idle"})
    r.t.advance(300)
    return r


def _shape(r):
    events = copy.deepcopy([e for w in r.sink.writes for e in w.events])
    for e in events:
        for key in ("eventId", "wallTimeMs", "explorationId"):
            e.pop(key, None)
        if e["type"] == "kernel_started":
            e["payload"].pop("language"), e["payload"].pop("kernelName")
    blobs = sorted(len(b) for w in r.sink.writes for b in w.blobs.values())
    return events, blobs


def test_an_r_message_stream_records_the_same_as_the_same_stream_labelled_python():
    r_run, py_run = _record("R"), _record("python")
    assert r_run.types() == ["kernel_started", "run_started", "output_added", "output_added",
                             "output_added", "run_finished"]
    assert r_run.sink.events("kernel_started")[0]["payload"]["language"] == "R"
    assert any(e["payload"]["item"]["mimeTypes"][0] == "image/png" for e in r_run.sink.events("output_added"))
    assert r_run.sink.events("run_finished")[0]["payload"]["status"] == "error"
    assert _shape(r_run) == _shape(py_run)

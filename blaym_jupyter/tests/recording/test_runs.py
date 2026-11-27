"""The run recorder's rules, against a fake service read and a list instead of the sink.

The real service and the real sink are in `test_runs_service.py`.
"""

from __future__ import annotations

import base64
import copy
import json

import pytest

from blaym_jupyter.recording.outputs import item_from_output
from blaym_jupyter.tests.recording.helpers import EXP, Rig, finished
from blaym_jupyter.tests.service.helpers import Rig as ServiceRig, TOKEN
from blaym_jupyter_types import GetBlobRequest


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


def test_a_restart_resolves_a_run_whose_code_has_just_arrived_and_starts_it_first():
    r = Rig()
    r.start_kernel()
    r.request("r1", code="x")
    r.code("c1", 1, "x")  # arrived between two polls
    r.rec.kernel_restarted("k1")
    assert r.types() == ["kernel_started", "run_started", "kernel_restarted"]


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


@pytest.mark.parametrize("reply,code_known,reply_before_request", [("aborted", True, True), ("abort", False, False)])
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


KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # a fake OpenAI-style key
PASSWORD = "hunter2-Plant3d-Pw"
SECRETS = (KEY, PASSWORD)

PAD = ["    padding line of an ordinary traceback frame"] * 600  # over the inline limit: the error is a blob

# name -> (output, the planted values that must not be readable afterwards)
CASES = {
    "text/plain": ({"output_type": "display_data", "metadata": {},
                    "data": {"text/plain": f"config loaded, api key {KEY} ok", "image/png": "AAAA"}}, [KEY]),
    "text/plain quoted assignment": ({"output_type": "display_data", "metadata": {},
                                      "data": {"text/plain": f'password="{PASSWORD}"\napi_key = "{KEY}"', "image/png": "AAAA"}},
                                     [KEY, PASSWORD]),
    "text/html": ({"output_type": "display_data", "metadata": {},
                   "data": {"text/html": f"<table><tr><td>token</td><td>{KEY}</td></tr></table>"}}, [KEY]),
    "text/html script with a quoted assignment": ({"output_type": "display_data", "metadata": {},
                   "data": {"text/html": f'<script>var password = "{PASSWORD}"; var k = "{KEY}";</script>'}}, [KEY, PASSWORD]),
    "plotly json": ({"output_type": "display_data", "metadata": {},
                     "data": {"application/vnd.plotly.v1+json": {
                         "data": [{"name": f"series {KEY}", "x": [1]}],
                         "layout": {"title": {"text": f"auth {KEY}"}, "meta": {"password": PASSWORD}}}}}, [KEY, PASSWORD]),
    "error traceback": ({"output_type": "error", "ename": "ValueError", "evalue": f"bad key {KEY}",
                         "traceback": PAD + ['    client = Client(api_key="' + KEY + '", password="' + PASSWORD + '")',
                                             f"ValueError: bad key {KEY}"]}, [KEY, PASSWORD]),
    "stream": ({"output_type": "stream", "name": "stdout", "text": f"export OPENAI_API_KEY={KEY}\n" + "x" * 20000}, [KEY]),
}


def test_no_planted_secret_is_readable_from_a_stored_blob():
    for name, (output, planted) in CASES.items():
        prepared = item_from_output(output)
        assert prepared.blob is not None, "these cases are built to need a blob"
        for value in planted:  # the case really plants what it says
            assert value in prepared.blob.decode("utf-8") or value in json.dumps(output)
        rig = ServiceRig()
        put = rig.service.put_blob(TOKEN, rig.exp, prepared.blob)
        stored = rig.service.get_blob(TOKEN, GetBlobRequest(explorationId=rig.exp, contentHash=put.contentHash)).decode("utf-8")
        decoded = json.dumps(json.loads(stored), ensure_ascii=False)  # JSON escapes undone: the value itself
        leaked = [v for v in planted if v in stored or v in decoded]
        assert not leaked, f"{name}: {leaked} readable in the stored blob"


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

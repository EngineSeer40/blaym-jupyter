"""The kernel adapter with fakes: lazy binding, the restart gap, observer errors, both protocols."""

from __future__ import annotations

import asyncio
import json
import queue

import pytest

from blaym_jupyter.adapters.jupyter.binding import KernelBindings
from blaym_jupyter.adapters.jupyter.kernels import (
    V1_PROTOCOL,
    ExecuteRequest,
    KernelAdapter,
    observing_websocket_class,
    parse_execute_request,
)


def v1_frame(channel, header, parent, metadata, content) -> bytes:
    parts = [channel.encode()] + [json.dumps(p).encode() for p in (header, parent, metadata, content)]
    offsets, pos = [], 8 * (len(parts) + 2)
    for p in parts:
        offsets.append(pos)
        pos += len(p)
    offsets.append(pos)
    head = len(offsets).to_bytes(8, "little") + b"".join(o.to_bytes(8, "little") for o in offsets)
    return head + b"".join(parts)


def execute(msg_id="m1", cell_id="c1", code="1+1", **content):
    return ({"msg_id": msg_id, "msg_type": "execute_request"}, {}, {"cellId": cell_id} if cell_id else {},
            {"code": code, **content})


class FakeRecorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, *args))
        return call

    def names(self):
        return [c[0] for c in self.calls]


class FakeSink:
    def __init__(self):
        self.writes = []

    def submit(self, write):
        self.writes.append(write)

    def events(self):
        return [e for w in self.writes for e in w.events]


class FakeRegistry:
    def __init__(self, paths=None, recording=True):
        self.paths = dict(paths or {})
        self._recording = {nb: recording for nb in self.paths.values()}

    def notebook_id_for_path(self, path):
        return self.paths.get(path)

    def recording(self, nb):
        return self._recording.get(nb, False)

    def switch(self, nb, on):
        self._recording[nb] = on


class FakeClient:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.stopped = False

    async def get_iopub_msg(self):
        return await self.inbox.get()

    def stop_channels(self):
        self.stopped = True

    def put(self, msg_type, parent, content=None):
        self.inbox.put_nowait({"header": {"msg_type": msg_type}, "parent_header": {"msg_id": parent},
                               "content": content or {}})


class Clock:
    def __init__(self):
        self.t = 1000

    def now_ms(self):
        self.t += 1
        return self.t


class Ids:
    def __init__(self):
        self.n = 0

    def new(self):
        self.n += 1
        return f"id{self.n}"


class Rig:
    def __init__(self, sessions=None, registry=None):
        self.rec, self.sink = FakeRecorder(), FakeSink()
        self.sessions = {} if sessions is None else sessions  # kernel id -> notebook path
        self.registry = registry or FakeRegistry({"a.ipynb": "nb-a"})
        self.clients: list[FakeClient] = []

        async def session_path(kernel_id):
            return self.sessions[kernel_id]  # KeyError while there is no session record

        def make_client(kernel_id):
            self.clients.append(FakeClient())
            return self.clients[-1]

        self.adapter = KernelAdapter(
            recorder=self.rec, sink=self.sink, bindings=KernelBindings(self.registry, session_path),
            clock=Clock(), ids=Ids(), make_client=make_client,
            kernel_info=lambda k: ("python3", "python"))

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0.01)

    async def close(self):
        await self.adapter.close()


def req(msg_id="m1", cell_id="c1", code="1+1", silent=False):
    return ExecuteRequest(msg_id, cell_id, code, silent)


@pytest.mark.asyncio
async def test_a_session_created_after_the_start_still_gets_kernel_started_at_the_first_run():
    r = Rig()
    r.adapter.kernel_started("k1", at_ms=500)
    r.adapter.execute_requested("k1", req("m0"))  # no session record yet: not recorded, not an error
    await r.settle()
    assert r.rec.calls == [] and r.adapter.stats["unrecorded_requests"] == 1
    r.clients[0].put("stream", "m0", {"text": "x"})  # unbound: dropped
    await r.settle()
    assert r.rec.calls == []
    r.sessions["k1"] = "a.ipynb"  # the session record appears
    r.adapter.execute_requested("k1", req("m1", "c9", "x=1"))
    r.clients[0].put("stream", "m1", {"name": "stdout", "text": "hi"})
    r.clients[0].put("status", "m1", {"execution_state": "idle"})
    await r.settle()
    assert r.rec.calls[0] == ("kernel_started", "k1", "nb-a", "k1", "python3", "python", 500)  # the start action's time
    assert r.rec.calls[1] == ("run_requested", "k1", "nb-a", "m1", "c9", "x=1")
    assert [c[3] for c in r.rec.calls[2:]] == ["stream", "status"]
    assert r.rec.calls[2][2] == "m1"
    await r.close()


class ColdClient(FakeClient):
    """A client whose IOPub subscription never proves itself (the kernel does not answer)."""

    def kernel_info(self):
        return "ki"

    async def get_iopub_msg(self, timeout=None):
        await asyncio.sleep(0.05)
        raise queue.Empty


@pytest.mark.asyncio
async def test_a_run_requested_before_the_subscription_is_proven_leaves_a_closed_point_gap():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter._make_client = lambda k: ColdClient()
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    assert not r.adapter.is_warm("k1") and r.adapter.stats["runs_before_warm"] == 1
    assert [e["type"] for e in r.sink.events()] == ["recording_gap_started", "recording_gap_ended"]
    assert r.sink.events()[0]["payload"]["source"] == "kernel"
    assert "run_requested" in r.rec.names()  # still recorded as well as it can be
    await r.close()


@pytest.mark.asyncio
async def test_an_unregistered_notebook_or_recording_off_is_not_recorded():
    r = Rig(sessions={"k1": "other.ipynb", "k2": "a.ipynb"}, registry=FakeRegistry({"a.ipynb": "nb-a"}, False))
    r.adapter.execute_requested("k1", req())
    r.adapter.execute_requested("k2", req())
    await r.settle()
    assert r.rec.calls == []
    r.registry.switch("nb-a", True)
    r.adapter.execute_requested("k2", req("m2"))
    await r.settle()
    assert r.rec.names() == ["kernel_started", "run_requested"]
    await r.close()


@pytest.mark.asyncio
async def test_switching_recording_off_unbinds_and_finishes_the_kernel_then_on_again_restarts_it():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    r.registry.switch("nb-a", False)
    r.clients[0].put("stream", "m1", {"text": "late"})
    await r.settle()
    assert r.rec.names() == ["kernel_started", "run_requested", "kernel_gone"]
    r.registry.switch("nb-a", True)
    r.adapter.execute_requested("k1", req("m2"))
    await r.settle()
    assert r.rec.names()[-2:] == ["kernel_started", "run_requested"]
    await r.close()


@pytest.mark.asyncio
async def test_a_rename_keeps_the_binding_and_the_run_going():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    r.registry.paths["b.ipynb"] = r.registry.paths.pop("a.ipynb")
    r.sessions["k1"] = "b.ipynb"
    r.adapter.note_rename("a.ipynb", "b.ipynb")
    r.clients[0].put("stream", "m1", {"text": "after"})
    await r.settle()
    assert r.rec.names() == ["kernel_started", "run_requested", "kernel_message"]
    await r.close()


@pytest.mark.asyncio
async def test_a_restart_reattaches_opens_a_gap_and_the_first_message_closes_it():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter.kernel_started("k1", 1)
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    old = r.clients[0]
    r.adapter.kernel_restarted("k1")
    await r.settle()
    assert old.stopped and len(r.clients) == 2  # re-attached
    restarted = [c for c in r.rec.calls if c[0] == "kernel_restarted"]
    assert len(restarted) == 1 and restarted[0][1:4] == ("k1", "k1", "unknown")
    started = [e for e in r.sink.events() if e["type"] == "recording_gap_started"]
    assert len(started) == 1 and started[0]["payload"]["source"] == "kernel" \
        and started[0]["payload"]["reason"] == "kernel_restart"
    assert [e["type"] for e in r.sink.events()] == ["recording_gap_started"]  # still open
    old.put("stream", "m1", {"text": "from the dead process"})  # the old client is ignored
    await r.settle()
    assert [e["type"] for e in r.sink.events()] == ["recording_gap_started"]
    r.clients[1].put("status", "", {"execution_state": "starting"})
    r.clients[1].put("status", "", {"execution_state": "idle"})
    await r.settle()
    types = [e["type"] for e in r.sink.events()]
    assert types == ["recording_gap_started", "recording_gap_ended"]
    ended = r.sink.events()[1]["payload"]["gapId"]
    assert ended == started[0]["payload"]["gapId"]
    # the next run is recorded through the new listener
    r.adapter.execute_requested("k1", req("m2"))
    r.clients[1].put("stream", "m2", {"text": "again"})
    await r.settle()
    assert r.rec.names()[-2:] == ["run_requested", "kernel_message"]
    await r.close()


@pytest.mark.asyncio
async def test_a_restart_of_an_unrecorded_kernel_writes_nothing_and_a_shutdown_closes_an_open_gap():
    r = Rig(sessions={"k1": "other.ipynb", "k2": "a.ipynb"})
    r.adapter.kernel_started("k1")
    r.adapter.kernel_restarted("k1")
    await r.settle()
    assert r.rec.calls == [] and r.sink.events() == []
    r.adapter.execute_requested("k2", req())
    await r.settle()
    r.adapter.kernel_restarted("k2")
    await r.settle()
    r.adapter.kernel_shutdown("k2")
    await r.settle()
    assert [e["type"] for e in r.sink.events()] == ["recording_gap_started", "recording_gap_ended"]
    assert r.rec.names()[-1] == "kernel_gone"
    await r.close()


@pytest.mark.asyncio
async def test_close_treats_every_kernel_as_gone_and_a_recorder_error_does_not_stop_the_lane():
    r = Rig(sessions={"k1": "a.ipynb"})

    def boom(*a):
        raise RuntimeError("secret detail")

    r.rec.run_requested = boom
    r.adapter.execute_requested("k1", req("m1"))
    r.adapter.execute_requested("k1", req("m2"))
    await r.settle()
    assert r.adapter.stats["op_errors"] == 2
    r.clients[0].put("stream", "m1", {"text": "x"})
    await r.settle()
    assert r.rec.names()[-1] == "kernel_message"  # the lane lives
    await r.close()
    assert r.rec.names()[-1] == "kernel_gone"


@pytest.mark.asyncio
async def test_silent_requests_are_not_runs_and_a_missing_cell_id_is_passed_as_none():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter.execute_requested("k1", req("quiet", silent=True))
    r.adapter.execute_requested("k1", req("script", cell_id=None))
    await r.settle()
    assert [c[1:] for c in r.rec.calls if c[0] == "run_requested"] == [("k1", "nb-a", "script", None, "1+1")]
    await r.close()


def test_both_protocols_parse_an_execute_request():
    header, parent, metadata, content = execute("m7", "cell-7", "print(1)", silent=False)
    legacy = json.dumps({"channel": "shell", "header": header, "parent_header": parent, "metadata": metadata,
                         "content": content})
    expected = ExecuteRequest("m7", "cell-7", "print(1)", False)
    assert parse_execute_request(None, legacy) == expected
    assert parse_execute_request(V1_PROTOCOL, v1_frame("shell", header, parent, metadata, content)) == expected
    nocell = execute("m8", None)
    assert parse_execute_request(V1_PROTOCOL, v1_frame("shell", *nocell)).cell_id is None
    other = ({"msg_id": "x", "msg_type": "comm_msg"}, {}, {}, {})
    assert parse_execute_request(V1_PROTOCOL, v1_frame("shell", *other)) is None
    assert parse_execute_request(V1_PROTOCOL, v1_frame("control", *execute())) is None
    assert parse_execute_request(None, json.dumps({"channel": "shell", "header": other[0]})) is None


class Base:
    """Stands in for the configured connection class."""

    kernel_id = "k1"
    subprotocol = None

    def __init__(self):
        self.seen = []

    def handle_incoming_message(self, incoming_msg):
        self.seen.append(incoming_msg)
        return "base-in"

    def handle_outgoing_message(self, stream, outgoing_msg):
        self.seen.append(outgoing_msg)
        return "base-out"


def test_observer_errors_never_reach_the_connection_and_traffic_is_passed_on_unchanged():
    class Broken:
        stats: dict = {}

        def execute_requested(self, *a):
            raise RuntimeError("observer bug")

    adapter = Broken()
    adapter.stats = __import__("collections").Counter()
    cls = observing_websocket_class(Base, adapter)
    assert issubclass(cls, Base)  # the configured class is subclassed, not replaced
    conn = cls()
    legacy = json.dumps({"channel": "shell", "header": execute()[0], "metadata": {}, "content": {"code": "1"}})
    assert conn.handle_incoming_message(legacy) == "base-in"  # the observer raised; the base ran
    assert conn.handle_incoming_message("not json {") == "base-in"
    assert conn.handle_incoming_message(b"\x00garbage") == "base-in"
    conn.session = object()  # outgoing parsing fails on this: still passes
    assert conn.handle_outgoing_message("shell", [b"x"]) == "base-out"
    assert conn.seen[0] == legacy and adapter.stats["observer_errors"] >= 3


@pytest.mark.asyncio
async def test_a_flood_of_output_stays_bounded_keeps_lifecycle_items_and_writes_one_gap():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter._max_msgs = 50
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    lane, gen = r.adapter._lanes["k1"], r.adapter._lanes["k1"].gen
    r.rec.calls.clear()
    big = {"name": "stdout", "text": "x" * 1000}
    for _ in range(1000):  # no await: the lane cannot drain while the flood arrives
        r.adapter._put(lane, ("iopub", gen, "m1", "stream", big))
    assert lane.queue.qsize() == 51 and lane.queued_msgs == 50 and lane.queued_bytes == 50 * 1006  # 50 + the gap marker
    r.adapter._put(lane, ("iopub", gen, "m1", "status", {"execution_state": "idle"}))
    r.adapter.execute_replied("k1", "m1", {"status": "ok"})
    assert lane.queue.qsize() == 53  # lifecycle items still get through
    await r.settle()
    r.adapter.kernel_shutdown("k1")
    await r.settle()
    assert r.adapter.stats["output_dropped_overflow"] == 950
    assert [c[3] for c in r.rec.calls if c[0] == "kernel_message"] == ["stream"] * 50 + ["status", "execute_reply"]
    assert r.rec.names()[-1] == "kernel_gone"
    assert [e["type"] for e in r.sink.events()] == ["recording_gap_started", "recording_gap_ended"]
    assert lane.queued_msgs == 0 and lane.queued_bytes == 0
    await r.close()


@pytest.mark.asyncio
async def test_warm_up_probes_are_capped_and_the_client_stays_unproven():
    r = Rig(sessions={"k1": "a.ipynb"})
    asked = []

    class Busy(ColdClient):
        def kernel_info(self):
            asked.append(1)
            return f"ki{len(asked)}"

    r.adapter._max_probes = 5
    r.adapter._make_client = lambda k: Busy()
    r.adapter.kernel_started("k1")
    await asyncio.sleep(0.6)
    assert len(asked) == 5 and not r.adapter.is_warm("k1")
    await r.close()


def test_installing_twice_does_not_wrap_the_connection_class_twice(jp_serverapp):
    from blaym_jupyter.adapters.jupyter import KERNELS_KEY, SETTINGS_KEY
    from blaym_jupyter.adapters.jupyter.kernels import install_kernel_adapter

    host = jp_serverapp.web_app.settings[SETTINGS_KEY]
    first = jp_serverapp.web_app.settings[KERNELS_KEY]
    cls = jp_serverapp.web_app.settings["kernel_websocket_connection_class"]
    assert install_kernel_adapter(jp_serverapp, host) is first
    assert jp_serverapp.web_app.settings["kernel_websocket_connection_class"] is cls
    assert sum(1 for c in cls.__mro__ if c.__name__.startswith("BlaymZMQ")) == 1

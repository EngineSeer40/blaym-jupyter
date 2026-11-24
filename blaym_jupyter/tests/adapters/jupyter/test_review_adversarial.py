"""Reviewer's adversarial cases for the kernel adapter, the scheduler and the host shutdown."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from blaym_jupyter.adapters.jupyter import SETTINGS_KEY, KERNELS_KEY
from blaym_jupyter.adapters.jupyter.kernels import (
    V1_PROTOCOL,
    install_kernel_adapter,
    observing_websocket_class,
)
from blaym_jupyter.adapters.jupyter.scheduler import ThreadScheduler
from blaym_jupyter.tests.adapters.jupyter.test_kernels import Base, FakeClient, FakeRegistry, Rig, execute, req, v1_frame


# -- the websocket subclass --------------------------------------------------------------------------


def test_observer_errors_are_swallowed_and_counted_for_the_v1_protocol_too():
    class Broken:
        stats = Counter()

        def execute_requested(self, *a):
            raise RuntimeError("observer bug")

    class V1Base(Base):
        subprotocol = V1_PROTOCOL

    adapter = Broken()
    conn = observing_websocket_class(V1Base, adapter)()
    frame = v1_frame("shell", *execute("m1", "c1", "1"))
    assert conn.handle_incoming_message(frame) == "base-in"
    assert conn.handle_incoming_message(b"\x00\x01") == "base-in"  # a frame that does not parse
    assert conn.handle_incoming_message(b"") == "base-in"
    assert conn.seen[0] == frame
    assert adapter.stats["observer_errors"] >= 2


def test_a_custom_configured_class_keeps_its_own_behaviour_and_state():
    class Custom(Base):
        custom_attr = "mine"

        def handle_incoming_message(self, incoming_msg):
            return "custom:" + super().handle_incoming_message(incoming_msg)

    adapter = SimpleNamespace(stats=Counter(), execute_requested=lambda *a: None, execute_replied=lambda *a: None)
    cls = observing_websocket_class(Custom, adapter)
    assert cls is not Custom and issubclass(cls, Custom) and cls.custom_attr == "mine"
    assert cls().handle_incoming_message("x") == "custom:base-in"


async def test_install_wraps_the_class_that_was_configured_and_never_replaces_it():
    class Mine(Base):
        pass

    listeners = []
    mkm = SimpleNamespace(list_kernel_ids=lambda: [], get_kernel=lambda k: None)
    app = SimpleNamespace(
        kernel_manager=mkm, session_manager=SimpleNamespace(), kernel_websocket_connection_class="ignored-trait",
        web_app=SimpleNamespace(settings={"kernel_websocket_connection_class": Mine}),
        event_logger=SimpleNamespace(add_listener=lambda listener: listeners.append(listener)))
    host = SimpleNamespace(run_recorder=object(), sink=object(), registry=FakeRegistry())
    adapter = install_kernel_adapter(app, host)
    wrapped = app.web_app.settings["kernel_websocket_connection_class"]
    assert issubclass(wrapped, Mine) and wrapped is not Mine and app.kernel_websocket_connection_class is wrapped
    assert listeners == [adapter.on_event]
    await adapter.close()


# -- re-attach and warm-up -----------------------------------------------------------------------------


async def test_a_message_of_the_old_client_already_queued_at_a_restart_is_dropped_by_generation():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter.kernel_started("k1", 1)
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    lane = r.adapter._lanes["k1"]
    old_gen = lane.gen
    r.adapter.kernel_restarted("k1")
    await r.settle()
    assert lane.gen > old_gen
    r.adapter._put(lane, ("iopub", old_gen, "m1", "stream", {"text": "late, from the old client"}))
    await r.settle()
    assert "kernel_message" not in r.rec.names()
    r.adapter._put(lane, ("iopub", lane.gen, "m1", "stream", {"text": "new client"}))
    await r.settle()
    assert r.rec.names().count("kernel_message") == 1
    await r.close()


class AskingClient(FakeClient):
    """A client that can ask `kernel_info`, so the pump must warm up first."""

    def __init__(self):
        super().__init__()
        self.asked: list[str] = []

    def kernel_info(self):
        self.asked.append(f"ki{len(self.asked)}")
        return self.asked[-1]

    async def get_iopub_msg(self, timeout=None):
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except asyncio.TimeoutError:
            raise queue.Empty from None


async def test_warm_up_does_not_swallow_a_real_runs_output_and_ends_on_its_own_idle_only():
    r = Rig(sessions={"k1": "a.ipynb"})
    client = AskingClient()
    r.clients.append(client)
    r.adapter._make_client = lambda k: client
    r.adapter.kernel_started("k1", 1)
    r.adapter.execute_requested("k1", req("m1"))
    await asyncio.sleep(0.05)
    assert not r.adapter.is_warm("k1") and client.asked
    client.put("stream", "m1", {"text": "real output"})  # a real run's message before the warm-up ended
    client.put("status", "m1", {"execution_state": "idle"})  # a real run's idle must NOT count as warm
    await asyncio.sleep(0.05)
    assert r.rec.names().count("kernel_message") == 2 and not r.adapter.is_warm("k1")
    client.put("status", client.asked[0], {"execution_state": "idle"})  # our own probe answered
    await asyncio.sleep(0.05)
    assert r.adapter.is_warm("k1")
    assert r.rec.names().count("kernel_message") == 2  # the probe's idle was not forwarded
    await r.close()


async def test_a_pump_whose_client_dies_is_counted_and_does_not_hurt_the_lane():
    r = Rig(sessions={"k1": "a.ipynb"})

    class Dying(FakeClient):
        async def get_iopub_msg(self):
            raise OSError("socket closed")

    r.adapter._make_client = lambda k: Dying()
    r.adapter.kernel_started("k1", 1)
    await r.settle()
    assert r.adapter.stats["pump_errors"] == 1
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    assert "run_requested" in r.rec.names()
    await r.close()


# -- the scheduler ----------------------------------------------------------------------------------


def test_concurrent_call_later_and_cancel_fire_each_uncancelled_call_exactly_once():
    sched = ThreadScheduler()
    fired, lock = Counter(), threading.Lock()
    kept: list[int] = []

    def worker(base):
        for i in range(400):
            n = base * 1000 + i

            def fn(n=n):
                with lock:
                    fired[n] += 1

            cancel = sched.call_later(5, fn)
            if i % 3:
                cancel()
                cancel()
            else:
                with lock:
                    kept.append(n)

    threads = [threading.Thread(target=worker, args=(b,)) for b in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    deadline = threading.Event()
    for _ in range(200):
        with lock:
            if all(fired[n] == 1 for n in kept):
                break
        deadline.wait(0.02)
    with lock:
        assert all(fired[n] == 1 for n in kept)
        assert not (set(fired) - set(kept)) or all(fired[n] <= 1 for n in fired)
    assert sched.pending() == 0 and not sched.errors
    sched.close()


def test_closing_from_inside_a_callback_and_a_late_call_later_do_not_hang():
    sched = ThreadScheduler()
    done = threading.Event()
    sched.call_later(0, lambda: (sched.close(), done.set()))
    assert done.wait(2)
    assert sched.call_later(0, lambda: None)() is None and sched.pending() == 0


# -- the host ----------------------------------------------------------------------------------------


async def test_cleanup_runs_the_kernel_adapter_before_the_host_even_when_it_fails(jp_serverapp):
    host = jp_serverapp.web_app.settings[SETTINGS_KEY]
    kernels = jp_serverapp.web_app.settings[KERNELS_KEY]
    order = []
    real_close = host.close

    async def bad_kernels_close(*a, **k):
        order.append("kernels")
        raise RuntimeError("boom")

    kernels.close = bad_kernels_close
    host.close = lambda: (order.append("host"), real_close())
    with pytest.raises(RuntimeError):
        await jp_serverapp.cleanup_kernels()
    assert order == ["kernels", "host"]


def test_host_close_order_failure_isolation_and_double_close(tmp_path):
    from blaym_jupyter.adapters.jupyter.config import BlaymConfig
    from blaym_jupyter.adapters.jupyter.host import BlaymHost

    host = BlaymHost(BlaymConfig(root=str(tmp_path / "b")), root_dir=tmp_path)
    order = []
    for name, obj in (("recorder", host.run_recorder), ("sink", host.sink), ("scheduler", host.scheduler),
                      ("service", host.service)):
        real = obj.close

        def wrapped(real=real, name=name):
            order.append(name)
            if name == "recorder":
                raise RuntimeError("recorder failed")
            real()

        obj.close = wrapped
    threads = [threading.Thread(target=host.close) for _ in range(4)]  # racing closers
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert order == ["recorder", "sink", "scheduler", "service"]


# -- the handler -------------------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["get", "post", "put"])
def test_every_verb_of_the_handler_is_decorated_with_authenticated_and_never_unauthenticated_allowed(verb):
    # Jupyter's own prepare() refuses unsigned requests as well, so a missing decorator is not visible over
    # HTTP; this keeps the second layer (S5-H) from being dropped silently.
    import inspect

    from blaym_jupyter.adapters.jupyter.handlers import BlaymApiHandler

    method = getattr(BlaymApiHandler, verb)
    assert "@tornado.web.authenticated" in inspect.getsource(method)
    assert not getattr(method, "__allow_unauthenticated", False)
    for other in ("delete", "patch", "head", "options"):
        assert getattr(BlaymApiHandler, other) is getattr(type("P", (BlaymApiHandler,), {}), other)  # not overridden here


async def test_an_encoded_slash_or_dot_segment_over_http_is_never_a_server_error(jp_fetch):
    from tornado.httpclient import HTTPClientError

    for parts in (("blobs", "a%2fb"), ("blobs", "%2e%2e"), ("blobs", "e", "%2e%2e"), ("notebooks", "%2e%2e", "status")):
        try:
            r = await jp_fetch("blaym-jupyter", "api", *parts, method="PUT" if parts[0] == "blobs" and len(parts) == 2 else "GET",
                               body=b"x" if parts[0] == "blobs" and len(parts) == 2 else None,
                               params={"instanceId": "t"})
            assert r.code < 500
        except HTTPClientError as err:
            assert err.code < 500, parts

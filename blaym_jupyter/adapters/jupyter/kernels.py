"""The kernel adapter (S5, Task 10): turns what Jupyter's kernels do into the run recorder's inputs.

Three observations, none of which alters or delays traffic or lets an error reach the kernel
connection (ADR 0003, S1):

* **IOPub**: one extra ZMQ client per kernel, subscribed to IOPub only. It is attached when
  `kernel_actions` says `start` (and for kernels that exist when the adapter starts), detached at
  `shutdown`, and **attached again at every `restart`** (the old client belongs to the old kernel
  process). Output messages go to `RunRecorder.kernel_message`, with or without a browser.
* **The kernel websocket**: a subclass of whatever `kernel_websocket_connection_class` is
  configured (never a replacement) that sees `execute_request` (message id, `cellId` from its
  metadata, code) in both protocols (`v1.kernel.websocket.jupyter.org` and the JSON one), and the
  shell `execute_reply` going out to that client. IOPub clients cannot see replies, so a run whose
  browser is away ends on the idle status plus `reply_grace_ms`.
* **`jupyter_events` `kernel_actions`**: `start` (name and language from the kernelspec),
  `restart` (`kernel_restarted`, reason `unknown`, S5-N), `shutdown`.

Threads and tasks: none of our own besides, per kernel, one asyncio task pumping its IOPub client
and one asyncio task (the "lane") that applies items in order. Everything arrives in the event loop
and is put on the kernel's queue in order; the lane task resolves the binding (async, session
manager) and then runs the recorder calls of a whole batch in one executor call, so the loop never
waits for a service read. The recorder, sink and scheduler own the only real threads.

Only the kernels of registered notebooks with recording on are recorded (`binding.py`, bound lazily
at the first run request). A restart opens a gap (source `kernel`, reason `kernel_restart`) that
closes when the first message of the kernel is seen again, or when the kernel goes away (ADR 0005).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from collections import Counter
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Optional

from jupyter_server.services.kernels.connection.base import (
    deserialize_binary_message,
    deserialize_msg_from_ws_v1,
)

from blaym_jupyter.recording.ports import Clock, Ids
from blaym_jupyter.recording.sink import PendingWrite

from .binding import KernelBindings

log = logging.getLogger("blaym_jupyter.kernels")

V1_PROTOCOL = "v1.kernel.websocket.jupyter.org"
_KEEP_CONTENT = ("stream", "display_data", "update_display_data", "execute_result", "error", "clear_output",
                 "execute_input", "execute_reply", "status")


_OUTPUT_TYPES = ("stream", "display_data", "update_display_data", "execute_result", "error")


def _approx(value: Any) -> int:
    """A cheap size estimate of a message content (lengths of strings and bytes, 8 for the rest)."""
    total, stack = 0, [value]
    while stack:
        v = stack.pop()
        if isinstance(v, (str, bytes)):
            total += len(v)
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        else:
            total += 8
    return total


@dataclass(frozen=True)
class ExecuteRequest:
    msg_id: str
    cell_id: Optional[str]
    code: str
    silent: bool


def parse_execute_request(subprotocol: Optional[str], raw: Any) -> Optional[ExecuteRequest]:
    """An `execute_request` from a websocket frame of either protocol, else None. Never raises
    for a frame that is not one (returns None); cheap for the common frames."""
    if subprotocol == V1_PROTOCOL:
        if not isinstance(raw, (bytes, bytearray)):
            return None
        channel, parts = deserialize_msg_from_ws_v1(bytes(raw))
        if channel != "shell" or len(parts) < 4:
            return None
        header = json.loads(parts[0])
        if header.get("msg_type") != "execute_request":
            return None
        metadata, content = json.loads(parts[2]), json.loads(parts[3])
    else:
        if isinstance(raw, (bytes, bytearray)):
            msg = deserialize_binary_message(bytes(raw))
        else:
            if '"execute_request"' not in raw:
                return None
            msg = json.loads(raw)
        header = msg.get("header") or {}
        if header.get("msg_type") != "execute_request" or msg.get("channel", "shell") != "shell":
            return None
        metadata, content = msg.get("metadata") or {}, msg.get("content") or {}
    msg_id = header.get("msg_id")
    if not isinstance(msg_id, str) or not msg_id:
        return None
    cell_id = (metadata or {}).get("cellId")
    return ExecuteRequest(msg_id, cell_id if isinstance(cell_id, str) and cell_id else None,
                          str(content.get("code", "")), bool(content.get("silent")))


class _Lane:
    """One kernel: its queue, its tasks and what has been told to the recorder."""

    def __init__(self, kernel_id: str, start_ms: int, name: str, language: str):
        self.kernel_id, self.start_ms, self.name, self.language = kernel_id, start_ms, name, language
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: Optional[asyncio.Future] = None
        self.client: Any = None
        self.pump: Optional[asyncio.Future] = None
        self.gen = 0  # which IOPub client's messages count
        self.queued_msgs = 0  # output messages waiting (bounded), and their estimated bytes
        self.queued_bytes = 0
        self.flood_gap = False  # one point gap per kernel session for dropped output
        self.warm = False  # the IOPub subscription is proven (see `_pump`)
        self.started_nb: Optional[str] = None  # notebook `kernel_started` was written for
        self.gap_id: Optional[str] = None
        self.gap_nb: Optional[str] = None


class KernelAdapter:
    def __init__(
        self,
        *,
        recorder: Any,
        sink: Any,
        bindings: KernelBindings,
        clock: Clock,
        ids: Ids,
        make_client: Callable[[str], Any],
        kernel_info: Callable[[str], tuple[str, str]],
        editor: str = "jupyterlab",
        origin_id: str = "blaym-kernel",
        max_queued_messages: int = 10_000,
        max_queued_bytes: int = 64 * 1024 * 1024,
        max_warm_probes: int = 20,
        max_warm_seconds: float = 10.0,
    ):
        self._max_msgs, self._max_bytes = max_queued_messages, max_queued_bytes
        self._max_probes, self._max_probe_s = max_warm_probes, max_warm_seconds
        self._rec, self._sink, self._bindings, self._clock, self._ids = recorder, sink, bindings, clock, ids
        self._make_client, self._kernel_info = make_client, kernel_info
        self._editor, self._origin = editor, origin_id
        self._lanes: dict[str, _Lane] = {}
        self._closed = False
        self.stats: Counter[str] = Counter()

    # -- inputs, all called in the event loop ---------------------------------------------------

    def kernel_started(self, kernel_id: str, at_ms: Optional[int] = None) -> None:
        lane = self._lane(kernel_id, at_ms)
        if at_ms is not None:
            lane.start_ms = at_ms

    def kernel_restarted(self, kernel_id: str) -> None:
        lane = self._lane(kernel_id)
        lane.flood_gap = False  # a new kernel session
        self._attach(lane)  # a new client for the new process; older messages are ignored by `gen`
        self._put(lane, ("restart", self._clock.now_ms()))

    def kernel_shutdown(self, kernel_id: str) -> None:
        lane = self._lanes.pop(kernel_id, None)
        if lane is not None:
            self._detach(lane)
            self._put(lane, ("gone",))

    def execute_requested(self, kernel_id: str, request: ExecuteRequest) -> None:
        if request.silent:  # the editor's own probes (variable inspectors and the like), not a run
            self.stats["silent_requests_skipped"] += 1
            return
        self._put(self._lane(kernel_id), ("request", request))

    def execute_replied(self, kernel_id: str, parent_msg_id: str, content: dict) -> None:
        lane = self._lanes.get(kernel_id)
        if lane is not None and parent_msg_id:
            self._put(lane, ("iopub", None, parent_msg_id, "execute_reply", content))

    def is_warm(self, kernel_id: str) -> bool:
        """Whether the kernel's IOPub client is known to receive (after a start or restart, runs begun
        before this can lose their first messages)."""
        lane = self._lanes.get(kernel_id)
        return bool(lane and lane.warm)

    def attach_existing(self, kernel_ids) -> None:
        for kernel_id in kernel_ids:
            self._lane(kernel_id)

    def note_rename(self, old: str, new: str) -> None:
        """The notebook moved (Task 17 calls this): its kernels follow it."""
        self._bindings.rename_path(old, new)

    async def on_event(self, logger: Any, schema_id: str, data: dict) -> None:
        """The `jupyter_events` listener (any schema; only `kernel_actions` counts)."""
        try:
            if not schema_id.endswith("/kernel_actions/v1") or data.get("status") != "success":
                return
            kernel_id = data.get("kernel_id")
            if not kernel_id or self._closed:
                return
            action = data.get("action")
            if action == "start":
                self.kernel_started(kernel_id, self._clock.now_ms())
            elif action == "restart":
                self.kernel_restarted(kernel_id)
            elif action == "shutdown":
                self.kernel_shutdown(kernel_id)
        except Exception as exc:  # noqa: BLE001
            self.stats["event_errors"] += 1
            log.warning("a kernel action could not be handled: %s", type(exc).__name__)

    async def close(self, timeout_s: float = 5.0) -> None:
        """Every kernel still known is treated as gone (the recorder finishes its runs), then wait
        for the lanes to apply that. Call before the host closes."""
        self._closed = True
        lanes = list(self._lanes.values())
        self._lanes.clear()
        for lane in lanes:
            self._detach(lane)
            self._put(lane, ("gone",))
        tasks = [lane.task for lane in lanes if lane.task is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_s)
            for t in tasks:
                if not t.done():
                    t.cancel()

    # -- lanes ----------------------------------------------------------------------------------

    def _put(self, lane: _Lane, item: tuple) -> None:
        """Queue an item. Only output messages are bounded (count and estimated bytes): past the
        bound they are dropped and counted, with one closed point gap per kernel session. Lifecycle
        items, `status`, `execute_input` and replies are always queued (they are small)."""
        size = 0
        if item[0] == "iopub" and item[3] in _OUTPUT_TYPES and item[4] is not None:
            size = _approx(item[4])
            if lane.queued_msgs + 1 > self._max_msgs or lane.queued_bytes + size > self._max_bytes:
                self.stats["output_dropped_overflow"] += 1
                if not lane.flood_gap:
                    lane.flood_gap = True
                    lane.queue.put_nowait((("flood",), 0, False))
                return
            lane.queued_msgs += 1
            lane.queued_bytes += size
            lane.queue.put_nowait((item, size, True))
            return
        lane.queue.put_nowait((item, 0, False))

    def _lane(self, kernel_id: str, start_ms: Optional[int] = None) -> _Lane:
        lane = self._lanes.get(kernel_id)
        if lane is None:
            try:
                name, language = self._kernel_info(kernel_id)
            except Exception:  # noqa: BLE001
                name, language = "unknown", ""
            lane = self._lanes[kernel_id] = _Lane(
                kernel_id, start_ms if start_ms is not None else self._clock.now_ms(), name, language)
            lane.task = asyncio.ensure_future(self._consume(lane))
            self._attach(lane)
        return lane

    def _attach(self, lane: _Lane) -> None:
        self._detach(lane)
        lane.gen += 1
        lane.warm = False
        try:
            client = self._make_client(lane.kernel_id)
        except Exception as exc:  # noqa: BLE001 - the kernel is gone or not ours to attach to
            self.stats["attach_failed"] += 1
            log.warning("could not attach to kernel %s: %s", lane.kernel_id, type(exc).__name__)
            return
        lane.client = client
        lane.warm = not hasattr(client, "kernel_info")  # a client that cannot ask has nothing to prove
        lane.pump = asyncio.ensure_future(self._pump(lane, client, lane.gen))
        self.stats["attached"] += 1

    def _detach(self, lane: _Lane) -> None:
        lane.gen += 1
        if lane.pump is not None:
            lane.pump.cancel()
            lane.pump = None
        client, lane.client = lane.client, None
        if client is not None:
            try:
                client.stop_channels()
            except Exception:  # noqa: BLE001
                pass

    async def _pump(self, lane: _Lane, client: Any, gen: int) -> None:
        """Read the client's IOPub messages into the lane's queue, in order.

        A ZMQ subscriber that has just connected loses what the kernel publishes before the
        subscription is in place (found in the Task 10 tests: the first run after a kernel start,
        and every run after a restart, came through empty). So the pump first proves the
        subscription by asking the kernel for `kernel_info` on its own shell channel and waiting
        for the `idle` status of that request on IOPub, asking again every 250 ms until it comes.
        The warm-up traffic is not forwarded; everything else is, from the first message on.
        (`wait_for_ready` is not used: it flushes IOPub and could swallow a run that has just begun.)
        """
        ask = getattr(client, "kernel_info", None)
        warm_ids: set[str] = set()
        warm = ask is None
        probes, began = 0, time.monotonic()
        lane.warm = warm and gen == lane.gen
        try:
            while True:
                probing = not warm and probes < self._max_probes and time.monotonic() - began < self._max_probe_s
                if probing and not warm_ids:
                    warm_ids.add(ask())
                    probes += 1
                try:
                    msg = await (client.get_iopub_msg(timeout=0.25) if probing else client.get_iopub_msg())
                except queue.Empty:
                    if probing:  # nothing yet: the kernel may not be up, or the subscription not joined
                        warm_ids.add(ask())
                        probes += 1
                    continue
                header, parent = msg.get("header") or {}, (msg.get("parent_header") or {}).get("msg_id", "")
                msg_type = header.get("msg_type", "")
                if parent in warm_ids:
                    if msg_type == "status" and (msg.get("content") or {}).get("execution_state") == "idle":
                        warm = True
                        lane.warm = gen == lane.gen
                    continue
                content = msg.get("content") if msg_type in _KEEP_CONTENT else None
                self._put(lane, ("iopub", gen, parent, msg_type, content))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.stats["pump_errors"] += 1
            log.warning("the IOPub listener of kernel %s stopped: %s", lane.kernel_id, type(exc).__name__)

    # -- the lane task: apply items in order ------------------------------------------------------

    async def _consume(self, lane: _Lane) -> None:
        loop = asyncio.get_running_loop()
        done = False
        while not done:
            entries = [await lane.queue.get()]
            while True:
                try:
                    entries.append(lane.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            batch = []
            for item, size, counted in entries:
                if counted:
                    lane.queued_msgs -= 1
                    lane.queued_bytes -= size
                batch.append(item)
            ops: list[Callable[[], None]] = []
            for item in batch:
                try:
                    done = await self._plan(lane, item, ops)
                except Exception as exc:  # noqa: BLE001 - one item must not stop the kernel's lane
                    self.stats["plan_errors"] += 1
                    log.warning("a kernel item could not be handled: %s", type(exc).__name__)
                if done:
                    break
            if ops:
                await loop.run_in_executor(None, self._run, ops)

    def _run(self, ops: list[Callable[[], None]]) -> None:
        for op in ops:
            try:
                op()
            except Exception as exc:  # noqa: BLE001
                self.stats["op_errors"] += 1
                log.warning("a recorder call failed: %s", type(exc).__name__)

    async def _plan(self, lane: _Lane, item: tuple, ops: list) -> bool:
        """Turn one item into recorder calls (appended to `ops`). True when the lane is over."""
        kind, kid = item[0], lane.kernel_id
        if kind == "request":
            request: ExecuteRequest = item[1]
            binding = await self._bindings.resolve(kid)
            self._sync(lane, binding.notebook_id if binding else None, ops)
            if binding is None:
                self.stats["unrecorded_requests"] += 1
                return False
            self._close_gap(lane, ops)
            if not lane.warm and lane.client is not None:
                # the IOPub subscription is not proven yet (a kernel only just started or restarted):
                # this run's first messages may be lost, so say so (a closed point gap, S5-R)
                self.stats["runs_before_warm"] += 1
                self._point_gap(binding.notebook_id, ops)
            ops.append(partial(self._rec.run_requested, kid, binding.notebook_id, request.msg_id,
                               request.cell_id, request.code))
        elif kind == "iopub":
            _, gen, parent, msg_type, content = item
            if gen is not None and gen != lane.gen:
                return False
            binding = self._bindings.current(kid)
            self._sync(lane, binding.notebook_id if binding else None, ops)
            if binding is None:
                return False
            self._close_gap(lane, ops)  # any message of the kernel: it is alive again
            if content is not None and parent:
                ops.append(partial(self._rec.kernel_message, kid, parent, msg_type, content))
        elif kind == "restart":
            binding = self._bindings.current(kid)
            self._sync(lane, binding.notebook_id if binding else None, ops)
            if binding is not None and lane.started_nb is not None:
                at = item[1]
                ops.append(partial(self._rec.kernel_restarted, kid, kid, "unknown", at))
                if lane.gap_id is None:
                    lane.gap_id, lane.gap_nb = self._ids.new(), binding.notebook_id
                    ops.append(partial(self._write, lane.gap_nb, self._event(
                        "recording_gap_started", lane.gap_nb,
                        {"gapId": lane.gap_id, "source": "kernel", "reason": "kernel_restart"}, at)))
        elif kind == "flood":
            binding = self._bindings.current(kid)
            if binding is not None:
                self._point_gap(binding.notebook_id, ops)
        elif kind == "gone":
            self._close_gap(lane, ops)
            if lane.started_nb is not None:
                ops.append(partial(self._rec.kernel_gone, kid))
                lane.started_nb = None
            self._bindings.unbind(kid)
            return True
        return False

    def _sync(self, lane: _Lane, notebook_id: Optional[str], ops: list) -> None:
        """Make the recorder's idea of the kernel match the binding: `kernel_started` when the
        kernel becomes recorded (with the time of the start action), `kernel_gone` when it stops
        being (recording switched off, another notebook)."""
        if lane.started_nb is not None and lane.started_nb != notebook_id:
            self._close_gap(lane, ops)
            ops.append(partial(self._rec.kernel_gone, lane.kernel_id))
            lane.started_nb = None
        if notebook_id is not None and lane.started_nb is None:
            ops.append(partial(self._rec.kernel_started, lane.kernel_id, notebook_id, lane.kernel_id,
                               lane.name, lane.language, lane.start_ms))
            lane.started_nb = notebook_id

    def _point_gap(self, nb: str, ops: list) -> None:
        """A closed gap (started and ended together): source `kernel`, reason `unknown`."""
        gap_id, at = self._ids.new(), self._clock.now_ms()
        ops.append(partial(self._write_all, nb, [
            self._event("recording_gap_started", nb, {"gapId": gap_id, "source": "kernel", "reason": "unknown"}, at),
            self._event("recording_gap_ended", nb, {"gapId": gap_id}, at)]))

    def _close_gap(self, lane: _Lane, ops: list) -> None:
        if lane.gap_id is None:
            return
        gap_id, nb = lane.gap_id, lane.gap_nb
        lane.gap_id = lane.gap_nb = None
        ops.append(partial(self._write, nb, self._event("recording_gap_ended", nb, {"gapId": gap_id},
                                                        self._clock.now_ms())))

    def _event(self, type_: str, exploration_id: str, payload: dict, at_ms: int) -> dict:
        return {"schemaVersion": 2, "eventId": self._ids.new(), "explorationId": exploration_id,
                "wallTimeMs": at_ms, "userId": "local", "workspaceId": "local", "originId": self._origin,
                "editor": self._editor, "type": type_, "payload": payload}

    def _write(self, exploration_id: str, event: dict) -> None:
        self._write_all(exploration_id, [event])

    def _write_all(self, exploration_id: str, events: list[dict]) -> None:
        self._sink.submit(PendingWrite(exploration_id, events, {}))


# -- the websocket hook ---------------------------------------------------------------------------

def observing_websocket_class(base: type, adapter: KernelAdapter) -> type:
    """A subclass of `base` (the configured connection class) that reports to `adapter`. Observer
    errors are counted and never reach the connection; the base class always runs, unchanged."""

    class BlaymObservingConnection(base):  # type: ignore[valid-type, misc]
        def handle_incoming_message(self, incoming_msg):
            try:
                request = parse_execute_request(self.subprotocol, incoming_msg)
                if request is not None:
                    adapter.execute_requested(self.kernel_id, request)
            except Exception:  # noqa: BLE001
                adapter.stats["observer_errors"] += 1
            return super().handle_incoming_message(incoming_msg)

        def handle_outgoing_message(self, stream, outgoing_msg):
            try:
                if (stream if isinstance(stream, str) else getattr(stream, "channel", None)) == "shell":
                    _, fed = self.session.feed_identities(outgoing_msg)
                    header = self.session.unpack(fed[1])
                    if header.get("msg_type") == "execute_reply":
                        parent = self.session.unpack(fed[2]).get("msg_id", "")
                        adapter.execute_replied(self.kernel_id, parent, self.session.unpack(fed[4]))
            except Exception:  # noqa: BLE001
                adapter.stats["observer_errors"] += 1
            return super().handle_outgoing_message(stream, outgoing_msg)

    BlaymObservingConnection.__name__ = f"Blaym{base.__name__}"
    return BlaymObservingConnection


# -- wiring into a Jupyter server -------------------------------------------------------------------

def install_kernel_adapter(server_app: Any, host: Any) -> KernelAdapter:
    """Build the adapter over the host's recording parts and hook it into the server. The caller
    runs `await adapter.close()` when the kernels have stopped and before `host.close()`."""
    from tornado.ioloop import IOLoop

    from blaym_jupyter.local import SystemClock

    from .host import RandomIds

    existing = server_app.web_app.settings.get("blaym_kernels")
    if existing is not None:  # a second call must not wrap the class or add a listener again
        return existing
    mkm = server_app.kernel_manager

    async def session_path(kernel_id: str) -> Optional[str]:
        session = await server_app.session_manager.get_session(kernel_id=kernel_id)
        return session.get("path")

    def make_client(kernel_id: str) -> Any:
        client = mkm.get_kernel(kernel_id).client()
        client.start_channels(shell=True, iopub=True, stdin=False, hb=False, control=False)
        return client

    def kernel_info(kernel_id: str) -> tuple[str, str]:
        name = mkm.get_kernel(kernel_id).kernel_name
        try:
            return name, mkm.kernel_spec_manager.get_kernel_spec(name).language or ""
        except Exception:  # noqa: BLE001
            return name, ""

    adapter = KernelAdapter(
        recorder=host.run_recorder, sink=host.sink, bindings=KernelBindings(host.registry, session_path),
        clock=SystemClock(), ids=RandomIds(), make_client=make_client, kernel_info=kernel_info)

    base = server_app.web_app.settings.get("kernel_websocket_connection_class") \
        or server_app.kernel_websocket_connection_class
    observing = observing_websocket_class(base, adapter)
    server_app.web_app.settings["kernel_websocket_connection_class"] = observing
    server_app.kernel_websocket_connection_class = observing

    server_app.web_app.settings["blaym_kernels"] = adapter
    server_app.event_logger.add_listener(listener=adapter.on_event)
    IOLoop.current().add_callback(lambda: adapter.attach_existing(list(mkm.list_kernel_ids())))
    return adapter

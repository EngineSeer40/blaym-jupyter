"""The run recorder (S5, decisions C, D, F, N, T): kernel messages become run events.

Neutral: it knows kernel messaging (Jupyter's message content, which every editor's kernel path
sees) and nothing of Jupyter itself. Time, ids, timers and the service come in through the
interfaces of `ports.py`; it starts no thread (the sink has the only one). Inputs:

    run_requested(kernel_ref, exploration_id, run_id, cell_id, code)
    kernel_started(kernel_ref, exploration_id, kernel_id, kernel_name, language)
    kernel_message(kernel_ref, parent_msg_id, msg_type, content)
    kernel_restarted(kernel_ref)            kernel_gone(kernel_ref)

`run_id` is also the join key: a kernel message belongs to the run whose id is its
`parent_msg_id` (in Jupyter the `msg_id` of the `execute_request`).

Rules in one place:

* The cell revision of a run is found by matching its code against the committed history
  (`resolve_cell_revision`), polled every 100 ms. The wait is `run_link_wait_ms`, extended to
  `run_link_max_wait_ms` while the registry says the notebook's recorder is alive. On timeout one
  point gap (started and ended together, source `kernel`) is written and the run's events are dropped.
* One ordered lane per kernel carries `kernel_started`, `kernel_restarted` and the starts of runs.
  A run starts (writes `run_started`) when its revision is known and so is its `runCount`: from
  `execute_input`, or after `run_count_wait_ms` the recorder's own count for that kernel session.
  A later run waits behind an earlier one that is still waiting for its code (Run All keeps request
  order) and behind a run that is still executing (a kernel runs one cell at a time). A restart
  resolves or drops the waiting runs first, then writes `kernel_restarted`.
* Output messages of a run that has not started are held raw in memory under a bound; the output
  assembler (`outputs.py`) is created at start. `run_started` and `run_finished` are never dropped
  by a bound. A started run that cannot be finished normally (`kernel_gone`, `close`, the next
  `execute_input` of the kernel arriving first) gets `run_finished`. A run has no time limit.
* A run ends when both `execute_reply` and the `idle` status are seen, or `reply_grace_ms` (10 s) after
  the first of them. Status: `error` or `ok` from the reply, `interrupted` on an abort or a
  `KeyboardInterrupt`.
* Messages with a parent nobody asked about are held `unknown_parent_buffer_ms` (a late
  `run_requested` takes them). After that only an `execute_input` that never got a parent is
  counted, with at most one gap per kernel session. Anything else does not count.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

from blaym_jupyter_types import ResolveCellRevisionRequest

from .outputs import AssembledEvent, OutputAssembler, OutputLimits
from .ports import Cancel, Clock, Ids, Ingestor, Scheduler
from .sink import PendingWrite

log = logging.getLogger("blaym_jupyter.recording.runs")

_OUTPUT_TYPES = ("stream", "display_data", "update_display_data", "execute_result", "error", "clear_output")
_RELEVANT = _OUTPUT_TYPES + ("execute_input", "execute_reply", "status")


class WriteSink(Protocol):
    def submit(self, write: PendingWrite) -> None: ...


class NotebookView(Protocol):
    """What the run recorder asks of the notebook registry (notebook id = exploration id)."""

    def alias_for(self, notebook_id: str, model_cell_id: str) -> str: ...

    def is_alive(self, notebook_id: str) -> bool: ...


@dataclass(frozen=True)
class RunRecorderSettings:
    run_link_wait_ms: int = 5_000
    run_link_max_wait_ms: int = 30_000
    poll_ms: int = 100
    unknown_parent_buffer_ms: int = 2_000
    run_count_wait_ms: int = 5_000
    reply_grace_ms: int = 10_000
    flush_ms: int = 250
    max_held_messages: int = 1_000
    max_held_bytes: int = 64 * 1024 * 1024
    max_unknown_parents: int = 256
    max_closed_runs: int = 1_024
    origin_id: str = "blaym-kernel"
    editor: str = "jupyterlab"


@dataclass
class _Mark:
    kind: str  # kernel_started | kernel_restarted
    payload: dict[str, Any]
    exploration_id: str
    at_ms: int


@dataclass
class _Run:
    run_id: str
    kernel: "_Kernel"
    exploration_id: str
    raw_cell_id: str
    code: str
    requested_ms: int
    state: str = "waiting"  # waiting | ready | started | done | dropped
    cell_id: str = ""
    revision: Optional[int] = None
    exec_count: Optional[int] = None
    count_expired: bool = False
    count_timer: Optional[Cancel] = None
    poll_timer: Optional[Cancel] = None
    grace_timer: Optional[Cancel] = None
    grace_expired: bool = False
    held: list = field(default_factory=list)  # raw output messages not yet fed to the assembler
    assembler: Optional[OutputAssembler] = None
    draining: bool = False
    finishing: bool = False
    reply_status: Optional[str] = None
    idle_seen: bool = False
    had_error: bool = False
    keyboard_interrupt: bool = False
    superseded: bool = False
    degraded: bool = False


@dataclass
class _Unknown:
    msgs: list = field(default_factory=list)
    timer: Optional[Cancel] = None
    has_input: bool = False


class _Kernel:
    def __init__(self, ref: str):
        self.ref = ref
        self.exploration_id: Optional[str] = None
        self.kernel_id: str = ref
        self.lane: deque = deque()
        self.runs: dict[str, _Run] = {}
        self.closed: "OrderedDict[str, str]" = OrderedDict()
        self.unknown: "OrderedDict[str, _Unknown]" = OrderedDict()
        self.session_count = 0
        self.session_gap = False
        self.busy = False


def _approx(value: Any) -> int:
    """A cheap size estimate of a message content (text and bytes lengths, shallow for the rest)."""
    total, stack = 0, [value]
    while stack:
        v = stack.pop()
        if isinstance(v, (str, bytes)):
            total += len(v)
        elif isinstance(v, Mapping):
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        else:
            total += 8
    return total


class RunRecorder:
    def __init__(
        self,
        ingestor: Ingestor,
        sink: WriteSink,
        clock: Clock,
        scheduler: Scheduler,
        ids: Ids,
        *,
        registry: Optional[NotebookView] = None,
        settings: RunRecorderSettings = RunRecorderSettings(),
        limits: OutputLimits = OutputLimits(),
        on_idle: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._ing, self._sink, self._clock, self._sched, self._ids = ingestor, sink, clock, scheduler, ids
        self._registry, self._s, self._limits, self._on_idle = registry, settings, limits, on_idle
        self._lock = threading.RLock()
        self._kernels: dict[str, _Kernel] = {}
        self._held_messages = 0
        self._held_bytes = 0
        self.stats: Counter[str] = Counter()

    # -- inputs ---------------------------------------------------------------------------------

    def kernel_started(self, kernel_ref: str, exploration_id: str, kernel_id: str, kernel_name: str,
                       language: str, at_ms: Optional[int] = None) -> None:
        with self._lock:
            k = self._kernel(kernel_ref)
            k.exploration_id, k.kernel_id = exploration_id, kernel_id
            k.session_count, k.session_gap = 0, False
            k.lane.append(_Mark("kernel_started", {"kernelId": kernel_id, "kernelName": kernel_name,
                                                   "language": language},
                                exploration_id, at_ms if at_ms is not None else self._clock.now_ms()))
        self._progress(k)

    def run_requested(self, kernel_ref: str, exploration_id: str, run_id: str, cell_id: Optional[str],
                      code: str) -> None:
        with self._lock:
            k = self._kernel(kernel_ref)
            k.exploration_id = exploration_id
            if run_id in k.runs or run_id in k.closed:
                self.stats["duplicate_requests"] += 1
                return
            self.stats["runs_requested"] += 1
            if cell_id is None:  # no cell to link to (a script, a headless run): a quiet edge
                self.stats["unlinked_runs"] += 1
                self._point_gap(exploration_id)
                self._close_id(k, run_id, "dropped")
                self._discard_unknown(k, run_id)
                return
            run = _Run(run_id, k, exploration_id, cell_id, code, self._clock.now_ms())
            k.runs[run_id] = run
            k.lane.append(run)
            k.busy = True
            replay = self._discard_unknown(k, run_id)
            for msg_type, content in replay:
                self._deliver(k, run, msg_type, content)
        self._poll(run)

    def kernel_message(self, kernel_ref: str, parent_msg_id: str, msg_type: str, content: Mapping[str, Any]) -> None:
        if msg_type not in _RELEVANT:
            return
        if msg_type == "status" and content.get("execution_state") != "idle":
            return
        with self._lock:
            k = self._kernel(kernel_ref)
            run = k.runs.get(parent_msg_id)
            if run is None:
                if parent_msg_id in k.closed:
                    if k.closed[parent_msg_id] == "grace" and msg_type in _OUTPUT_TYPES:
                        # the run ended by grace expiry and output still came: one closed gap marks the loss
                        k.closed[parent_msg_id] = "grace_gapped"
                        if k.exploration_id is not None:
                            self._point_gap(k.exploration_id)
                        self.stats["late_output_gaps"] += 1
                    self.stats["late_messages"] += 1
                else:
                    self._buffer_unknown(k, parent_msg_id, msg_type, content)
                return
            self._deliver(k, run, msg_type, content)
        self._progress(k)

    def kernel_restarted(self, kernel_ref: str, kernel_id: Optional[str] = None, reason: str = "unknown",
                         at_ms: Optional[int] = None) -> None:
        """The kernel restarted. Runs still waiting for their code are resolved or dropped first, so
        that `kernel_restarted` comes after them in the history; runs that were executing are left to
        the rebuilder's rule (a restart marks them unfinished)."""
        with self._lock:
            k = self._kernel(kernel_ref)
        self._wind_down(k, finish_started=False)
        with self._lock:
            if kernel_id:
                k.kernel_id = kernel_id
            if k.exploration_id is None:
                self.stats["restart_unbound"] += 1
            else:
                k.lane.append(_Mark("kernel_restarted", {"kernelId": k.kernel_id, "reason": reason},
                                    k.exploration_id, at_ms if at_ms is not None else self._clock.now_ms()))
            k.session_count, k.session_gap = 0, False
            self._advance(k, force=True)
        self._progress(k)

    def kernel_gone(self, kernel_ref: str) -> None:
        """The kernel is gone (killed, shut down): waiting runs are resolved or dropped, started
        runs are finished as interrupted, and the kernel is forgotten."""
        with self._lock:
            k = self._kernels.get(kernel_ref)
        if k is None:
            return
        self._wind_down(k, finish_started=True)
        with self._lock:
            for u in list(k.unknown):
                self._discard_unknown(k, u)
            self._kernels.pop(kernel_ref, None)
        self._notify_idle(k)

    def close(self) -> None:
        """Shutdown: every kernel is treated as gone."""
        with self._lock:
            refs = list(self._kernels)
        for ref in refs:
            self.kernel_gone(ref)

    # -- kernels, the lane and starting runs ---------------------------------------------------

    def _kernel(self, ref: str) -> _Kernel:
        with self._lock:
            k = self._kernels.get(ref)
            if k is None:
                k = self._kernels[ref] = _Kernel(ref)
            return k

    def _advance(self, k: _Kernel, force: bool = False) -> list[_Run]:
        """Write what the lane lets through, in order. Under the lock."""
        started: list[_Run] = []
        while k.lane:
            head = k.lane[0]
            if isinstance(head, _Mark):
                k.lane.popleft()
                self._emit_mark(head)
                continue
            if head.state == "waiting":
                break
            count = head.exec_count
            if not force and any(r.state == "started" and r.superseded for r in k.runs.values()):
                break  # an earlier run is over (this one began) but not written as finished yet
            if count is None:
                if not force and any(r.state == "started" for r in k.runs.values()):
                    break  # the kernel runs one cell at a time: this one has not begun
                if force or head.count_expired:
                    count = k.session_count + 1
                    self.stats["count_fallbacks"] += 1
                else:
                    if head.count_timer is None:
                        head.count_timer = self._sched.call_later(self._s.run_count_wait_ms,
                                                                  lambda r=head: self._count_expired(r))
                    break
            k.lane.popleft()
            self._start(k, head, count)
            started.append(head)
        return started

    def _count_expired(self, run: _Run) -> None:
        with self._lock:
            run.count_timer = None
            run.count_expired = True
        self._progress(run.kernel)

    def _start(self, k: _Kernel, run: _Run, count: int) -> None:
        self._cancel(run, "count_timer")
        self._cancel(run, "poll_timer")
        k.session_count = max(k.session_count, count)
        links = {"cellId": run.cell_id, "runId": run.run_id, "cellRevision": run.revision}
        self._write(run.exploration_id, [self._event("run_started", run.exploration_id, {
            "runId": run.run_id, "cellId": run.cell_id, "cellRevision": run.revision, "runCount": count},
            links)])
        run.state = "started"
        later = False
        for other in k.runs.values():  # a run that began after this one means this one is over
            if other is run:
                later = True
            elif later and other.exec_count is not None:
                run.superseded = True
                self.stats["superseded"] += 1
                break
        run.assembler = OutputAssembler(run.run_id, run.cell_id, lambda evs, r=run: self._emit_outputs(r, evs),
                                        self._sched, self._limits, self._s.flush_ms)
        self.stats["runs_started"] += 1

    def _emit_mark(self, mark: _Mark) -> None:
        self._write(mark.exploration_id, [self._event(mark.kind, mark.exploration_id, mark.payload, None, mark.at_ms)])

    def _emit_outputs(self, run: _Run, events: list[AssembledEvent]) -> None:
        """The assembler's callback (under its lock): hand a run's output events to the sink."""
        evs: list[dict] = []
        blobs: dict[str, bytes] = {}
        for e in events:
            evs.append(self._event(e.type, run.exploration_id, e.payload(),
                                   {"cellId": run.cell_id, "runId": run.run_id}))
            if e.prepared is not None and e.prepared.blob is not None:
                blobs[e.prepared.item.contentHash] = e.prepared.blob
        with self._lock:
            if run.state == "started":
                self._sink.submit(PendingWrite(run.exploration_id, evs, blobs))

    # -- revision matching ----------------------------------------------------------------------

    def _alive(self, exploration_id: str) -> bool:
        try:
            return bool(self._registry and self._registry.is_alive(exploration_id))
        except Exception:  # noqa: BLE001
            return False

    def _alias(self, exploration_id: str, cell_id: str) -> str:
        try:
            return self._registry.alias_for(exploration_id, cell_id) if self._registry else cell_id
        except Exception:  # noqa: BLE001
            return cell_id

    def _poll(self, run: _Run, final: bool = False) -> None:
        with self._lock:
            if run.state != "waiting":
                return
            cell = self._alias(run.exploration_id, run.raw_cell_id)
        answer = None
        try:
            response = self._ing.resolve_cell_revision(ResolveCellRevisionRequest(
                explorationId=run.exploration_id, cellId=cell, code=run.code))
            answer = getattr(response, "root", response)
        except Exception as exc:  # noqa: BLE001 - a read failed: keep waiting
            self.stats["resolve_errors"] += 1
            log.warning("resolving a run's revision failed: %s", type(exc).__name__)
        with self._lock:
            if run.state != "waiting":
                return
            if answer is not None and answer.status == "ok":
                run.cell_id, run.revision, run.state = cell, answer.cellRevision, "ready"
                self._cancel(run, "poll_timer")
            else:
                limit = self._s.run_link_max_wait_ms if self._alive(run.exploration_id) else self._s.run_link_wait_ms
                if final or self._clock.now_ms() - run.requested_ms >= limit:
                    self._drop(run, "timeout" if not final else "restart")
                else:
                    run.poll_timer = self._sched.call_later(self._s.poll_ms, lambda r=run: self._poll(r))
        self._progress(run.kernel)

    def _drop(self, run: _Run, why: str) -> None:
        """Forget a run that never started: one point gap, its held messages freed. Under the lock."""
        k = run.kernel
        self._release_held(len(run.held), sum(_approx(c) for _, c in run.held))
        run.held.clear()
        run.state = "dropped"
        for name in ("poll_timer", "count_timer", "grace_timer"):
            self._cancel(run, name)
        try:
            k.lane.remove(run)
        except ValueError:
            pass
        k.runs.pop(run.run_id, None)
        self._close_id(k, run.run_id, "dropped")
        self._point_gap(run.exploration_id)
        self.stats["runs_dropped_" + why] += 1
        log.warning("run %s dropped (%s): its code never appeared in the history", run.run_id, why)

    def _point_gap(self, exploration_id: str) -> None:
        gap_id = self._ids.new()
        self._write(exploration_id, [
            self._event("recording_gap_started", exploration_id,
                        {"gapId": gap_id, "source": "kernel", "reason": "unknown"}),
            self._event("recording_gap_ended", exploration_id, {"gapId": gap_id}),
        ])
        self.stats["point_gaps"] += 1

    # -- messages -------------------------------------------------------------------------------

    def _deliver(self, k: _Kernel, run: _Run, msg_type: str, content: Mapping[str, Any]) -> None:
        """Take one message of a known run. Under the lock."""
        if msg_type == "execute_input":
            count = content.get("execution_count")
            if run.exec_count is None and isinstance(count, int) and count >= 1:
                run.exec_count = count
            for other in k.runs.values():  # a kernel runs one cell at a time: the earlier ones are over
                if other is not run and other.state == "started" and not other.superseded:
                    other.superseded = True
                    self.stats["superseded"] += 1
                if other is run:
                    break
        elif msg_type == "execute_reply":
            status = content.get("status")
            run.reply_status = status if isinstance(status, str) else "ok"
            count = content.get("execution_count")
            if run.exec_count is None and isinstance(count, int) and count >= 1:
                run.exec_count = count
        elif msg_type == "status":
            run.idle_seen = True
        else:
            if msg_type == "error":
                if content.get("ename") == "KeyboardInterrupt":
                    run.keyboard_interrupt = True
                run.had_error = True
            self._hold(run, msg_type, content)

    def _hold(self, run: _Run, msg_type: str, content: Mapping[str, Any]) -> None:
        size = _approx(content)
        if (self._held_messages + 1 > self._s.max_held_messages
                or self._held_bytes + size > self._s.max_held_bytes):
            run.degraded = True
            self.stats["held_dropped"] += 1
            return
        self._held_messages += 1
        self._held_bytes += size
        run.held.append((msg_type, content))

    def _release_held(self, count: int, size: int) -> None:
        self._held_messages -= count
        self._held_bytes -= size

    def _buffer_unknown(self, k: _Kernel, parent: str, msg_type: str, content: Mapping[str, Any]) -> None:
        u = k.unknown.get(parent)
        if u is None:
            if len(k.unknown) >= self._s.max_unknown_parents:
                oldest = next(iter(k.unknown))
                self._expire_unknown(k, oldest)
            u = k.unknown[parent] = _Unknown()
            u.timer = self._sched.call_later(self._s.unknown_parent_buffer_ms,
                                             lambda: self._unknown_timer(k, parent))
        if msg_type == "execute_input":
            u.has_input = True
        size = _approx(content)
        if msg_type in _OUTPUT_TYPES and (self._held_messages + 1 > self._s.max_held_messages
                                          or self._held_bytes + size > self._s.max_held_bytes):
            self.stats["held_dropped"] += 1
            return
        self._held_messages += 1
        self._held_bytes += size
        u.msgs.append((msg_type, content))

    def _discard_unknown(self, k: _Kernel, parent: str) -> list:
        """Take a parent's buffered messages out (freeing their budget) and stop its timer."""
        u = k.unknown.pop(parent, None)
        if u is None:
            return []
        if u.timer is not None:
            u.timer()
        self._release_held(len(u.msgs), sum(_approx(c) for _, c in u.msgs))
        return u.msgs

    def _unknown_timer(self, k: _Kernel, parent: str) -> None:
        with self._lock:
            if parent in k.unknown:
                self._expire_unknown(k, parent)

    def _expire_unknown(self, k: _Kernel, parent: str) -> None:
        u = k.unknown.get(parent)
        has_input = bool(u and u.has_input)
        self._discard_unknown(k, parent)
        if not has_input:
            return  # status, completion and other traffic nobody asked us to record never counts
        self.stats["unknown_executions"] += 1
        if not k.session_gap and k.exploration_id is not None:
            k.session_gap = True
            self._point_gap(k.exploration_id)

    # -- ending runs ----------------------------------------------------------------------------

    def _terminal_status(self, run: _Run, forced: bool) -> str:
        if run.reply_status in ("abort", "aborted") or run.keyboard_interrupt:
            return "interrupted"
        if run.reply_status == "error" or (run.reply_status is None and run.had_error):
            return "error"
        if forced and run.reply_status is None and not run.idle_seen:
            return "interrupted"
        return "ok"

    def _drain(self, run: _Run) -> bool:
        """Feed the held output messages of a started run to its assembler, outside the lock."""
        did = False
        while True:
            with self._lock:
                if run.state != "started" or run.draining or not run.held:
                    return did
                batch, run.held = run.held, []
                self._release_held(len(batch), sum(_approx(c) for _, c in batch))
                run.draining = True
            try:
                for msg_type, content in batch:
                    try:
                        run.assembler.message(msg_type, content)
                    except Exception as exc:  # noqa: BLE001 - one odd message must not stop the run
                        self.stats["message_errors"] += 1
                        log.warning("a %s message of run %s could not be recorded: %s", msg_type, run.run_id,
                                    type(exc).__name__)
            finally:
                with self._lock:
                    run.draining = False
            did = True

    def _maybe_finish(self, run: _Run) -> bool:
        with self._lock:
            if run.state != "started" or run.finishing or run.held or run.draining:
                return False
            both = (run.reply_status is not None and run.idle_seen) or run.grace_expired or run.superseded
            if not both:
                if (run.reply_status is not None or run.idle_seen) and run.grace_timer is None:
                    run.grace_timer = self._sched.call_later(self._s.reply_grace_ms,
                                                             lambda r=run: self._grace(r))
                return False
            run.finishing = True
        self._end_run(run, self._terminal_status(run, forced=True))
        return True

    def _grace(self, run: _Run) -> None:
        with self._lock:
            run.grace_timer = None
            run.grace_expired = True
        self._progress(run.kernel)

    def _end_run(self, run: _Run, status: Optional[str]) -> None:
        """Flush the assembler, then write `run_finished` (when `status` is given) and forget the run."""
        run.assembler.finish()  # outside the lock: its callback takes it
        with self._lock:
            if run.state != "started":
                return
            if status is not None:
                self._write(run.exploration_id, [self._event(
                    "run_finished", run.exploration_id, {"runId": run.run_id, "status": status},
                    {"cellId": run.cell_id, "runId": run.run_id})])
                self.stats["runs_finished"] += 1
            run.state = "done"
            for name in ("poll_timer", "count_timer", "grace_timer"):
                self._cancel(run, name)
            run.kernel.runs.pop(run.run_id, None)
            by_grace = run.grace_expired and not (run.reply_status is not None and run.idle_seen)
            self._close_id(run.kernel, run.run_id, "grace" if by_grace else "done")

    def _wind_down(self, k: _Kernel, finish_started: bool) -> None:
        """For a restart or a vanished kernel: settle every run before the kernel event is written."""
        with self._lock:
            waiting = [r for r in k.lane if isinstance(r, _Run) and r.state == "waiting"]
        for run in waiting:
            self._poll(run, final=True)
        with self._lock:
            started = self._advance(k, force=True)
            live = [r for r in k.runs.values() if r.state == "started"]
        for run in live:
            self._drain(run)
        for run in live:
            with self._lock:
                if run.state != "started" or run.finishing:
                    continue
                run.finishing = True
                known = run.reply_status is not None or run.idle_seen
            if finish_started:
                self._end_run(run, self._terminal_status(run, forced=True))
            else:  # the restart marks it unfinished; a run that had really ended keeps its own end
                self._end_run(run, self._terminal_status(run, forced=False) if known else None)

    def _progress(self, k: _Kernel) -> None:
        """Move everything that can move: the lane, held messages, finishes. Repeats until stable."""
        while True:
            with self._lock:
                started = self._advance(k)
                live = [r for r in k.runs.values() if r.state == "started"]
            moved = bool(started)
            for run in live:
                moved = self._drain(run) or moved
                moved = self._maybe_finish(run) or moved
            if not moved:
                break
        self._notify_idle(k)

    def _notify_idle(self, k: _Kernel) -> None:
        with self._lock:
            idle = k.busy and not k.runs
            if idle:
                k.busy = False
            exploration = k.exploration_id
        if idle and exploration is not None and self._on_idle is not None:
            try:
                self._on_idle(exploration)
            except Exception:  # noqa: BLE001 - a hook must not stop the recorder
                log.exception("the idle hook failed")

    # -- small helpers --------------------------------------------------------------------------

    def _cancel(self, run: _Run, name: str) -> None:
        timer = getattr(run, name)
        if timer is not None:
            setattr(run, name, None)
            timer()

    def _close_id(self, k: _Kernel, run_id: str, how: str) -> None:
        k.closed[run_id] = how
        while len(k.closed) > self._s.max_closed_runs:
            k.closed.popitem(last=False)

    def _event(self, type_: str, exploration_id: str, payload: dict, links: Optional[dict] = None,
               at_ms: Optional[int] = None) -> dict:
        event = {
            "schemaVersion": 2, "eventId": self._ids.new(), "explorationId": exploration_id,
            "wallTimeMs": at_ms if at_ms is not None else self._clock.now_ms(),
            "userId": "local", "workspaceId": "local", "originId": self._s.origin_id,
            "editor": self._s.editor, "type": type_, "payload": payload,
        }
        if links:
            event["links"] = links
        return event

    def _write(self, exploration_id: str, events: list[dict]) -> None:
        self._sink.submit(PendingWrite(exploration_id, events, {}))


__all__ = ["NotebookView", "RunRecorder", "RunRecorderSettings", "WriteSink"]

"""The ordered sink (S5, decision F): the one place server-side recorders write from.

A recorder hands a `PendingWrite` (events plus the blobs their output items point to) to
`OrderedSink.submit`, which returns at once. One worker thread (the only thread of this layer)
writes per exploration in first-in first-out order:

* `put_blob` before `ingest`, and the event's `contentHash` is the hash `put_blob` answered (the
  service hides secrets before hashing, so the recorder's own hash is only provisional);
* the whole outcome list of an `ingest` is read, not only the first answer. A refused output
  event is logged (ids and reasons only, never code, previews or details) and counted, never
  retried blindly. `missing_blob` puts the blob again once. `bad_first_event` (the browser has
  not sent the first snapshot yet) parks that exploration's events, in order, and retries every
  second until one is accepted, up to 500 events or 10 minutes (then they are dropped, counted).
  `run_in_progress` defers that one event until `on_idle(exploration_id)` (the run recorder calls
  it when a kernel has no runs left); it does not hold up the events behind it, because those
  include the `run_finished` the refusal is waiting for;
* bounds per process: 1,000 queued events and 64 MB of pending blob bytes. Beyond them only
  output content is reduced: a blob becomes a preview-only blob, and past the event bound the
  output events of a run are dropped (counted) and the run's later output events with them, but
  `run_started`, `run_finished` and every other event stays.

`close()` drains with a timeout and reports what it could not write.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from blaym_jupyter_types import IngestRequest

from .edges import point_gap
from .ports import Clock, Ingestor

log = logging.getLogger("blaym_jupyter.recording.sink")

OUTPUT_TYPES = ("output_added", "output_replaced", "outputs_cleared")


@dataclass
class PendingWrite:
    """Events of one exploration, in order, and the blobs their items refer to (keyed by the
    provisional `contentHash` the item carries)."""

    exploration_id: str
    events: list[dict[str, Any]]
    blobs: dict[str, bytes] = field(default_factory=dict)


@dataclass
class CloseReport:
    unwritten: int = 0
    by_exploration: dict[str, int] = field(default_factory=dict)
    run_finished_unwritten: int = 0


@dataclass
class _Item:
    event: dict[str, Any]
    blobs: dict[str, bytes]
    size: int
    blob_retried: bool = False
    attempts: int = 0
    solo: bool = False  # failed in a batch once: written alone from now on


class OrderedSink:
    def __init__(
        self,
        ingestor: Ingestor,
        clock: Clock,
        *,
        editor: str = "jupyterlab",
        instance_id: str = "blaym-server",
        max_queued_events: int = 1000,
        max_pending_blob_bytes: int = 64 * 1024 * 1024,
        park_retry_ms: int = 1000,
        park_max_events: int = 500,
        park_max_ms: int = 600_000,
        error_retries: int = 5,
        batch_size: int = 20,
        max_deferred: int = 100,
        poll_s: float = 0.02,
    ) -> None:
        self._ing, self._clock = ingestor, clock
        self._editor, self._instance = editor, instance_id
        self._max_events, self._max_bytes = max_queued_events, max_pending_blob_bytes
        self._retry_ms, self._park_events, self._park_ms = park_retry_ms, park_max_events, park_max_ms
        self._error_retries, self._batch, self._max_deferred = error_retries, batch_size, max_deferred
        self._poll = poll_s
        self._cv = threading.Condition()
        self._queues: dict[str, deque[_Item]] = {}
        self._deferred: dict[str, list[_Item]] = {}
        self._blocked_until: dict[str, int] = {}
        self._park_since: dict[str, int] = {}
        self._degraded: set[str] = set()  # run ids whose output events are being dropped
        self._queued = 0
        self._pending_bytes = 0
        self._inflight = 0
        self._order: deque[str] = deque()  # round robin over explorations
        self._thread: Optional[threading.Thread] = None
        self._closing = False
        self._stopped = False
        self.stats: Counter[str] = Counter()
        self.refused: Counter[str] = Counter()  # by reason

    # -- submit ---------------------------------------------------------------------------------

    def submit(self, write: PendingWrite) -> None:
        with self._cv:
            if self._closing:
                self.stats["rejected_after_close"] += len(write.events)
                return
            for event in write.events:
                self._submit_one(write.exploration_id, event, write.blobs)
            self._start_thread()
            self._cv.notify_all()

    def _submit_one(self, exploration_id: str, event: dict, blobs: dict[str, bytes]) -> None:
        etype = event.get("type")
        run_id = (event.get("payload") or {}).get("runId")
        if etype in OUTPUT_TYPES and run_id in self._degraded:
            self.stats["dropped_overflow"] += 1
            return
        if etype in OUTPUT_TYPES and self._queued >= self._max_events:
            self._degraded.add(run_id)
            self.stats["dropped_overflow"] += 1
            # S5-R: say so once per run, as a closed point gap (source `kernel`), outside the bound
            for gap in point_gap(exploration_id, source="kernel", reason="unknown", at_ms=self._clock.now_ms(),
                                 editor=self._editor, origin_id=self._instance):
                self._queues.setdefault(exploration_id, deque()).append(_Item(gap, {}, 0))
                if exploration_id not in self._order:
                    self._order.append(exploration_id)
                self._queued += 1
            return
        if etype == "run_finished":
            self._degraded.discard(run_id)
        mine = {h: blobs[h] for h in _hashes_in(event) if h in blobs}
        size = sum(len(b) for b in mine.values())
        if size and self._pending_bytes + size > self._max_bytes:
            event, mine = _reduced(event, mine)
            size = sum(len(b) for b in mine.values())
            self.stats["reduced_blobs"] += 1
        queue = self._queues.setdefault(exploration_id, deque())
        if exploration_id not in self._order:
            self._order.append(exploration_id)
        if exploration_id in self._park_since and len(queue) >= self._park_events:
            self._drop_parked(exploration_id)
            queue = self._queues[exploration_id]
        queue.append(_Item(event, mine, size))
        self._queued += 1
        self._pending_bytes += size

    def _drop_parked(self, exploration_id: str) -> None:
        queue = self._queues.get(exploration_id)
        if queue:
            self.stats["parked_dropped"] += len(queue)
            log.warning("dropping %d parked events of %s: its history never started", len(queue), exploration_id)
            for item in queue:
                self._release(item)
            queue.clear()
        self._park_since.pop(exploration_id, None)
        self._blocked_until.pop(exploration_id, None)

    def _release(self, item: _Item) -> None:
        self._queued -= 1
        self._pending_bytes -= item.size

    def on_idle(self, exploration_id: str) -> None:
        """The run recorder reports that no run of a kernel of this exploration is left: events
        deferred for `run_in_progress` go to the back of the queue, behind the `run_finished` the
        recorder has just submitted."""
        with self._cv:
            items = self._deferred.pop(exploration_id, [])
            if items:
                queue = self._queues.setdefault(exploration_id, deque())
                if exploration_id not in self._order:
                    self._order.append(exploration_id)
                queue.extend(items)  # behind what is already queued: the run's `run_finished` goes first
                self._cv.notify_all()

    # -- lifecycle ------------------------------------------------------------------------------

    def _start_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="blaym-sink", daemon=True)
            self._thread.start()

    def drain(self, timeout_s: float = 5.0) -> bool:
        """Wait until nothing is queued or in flight. Parked events count as queued; events
        deferred for `run_in_progress` do not (they wait for `on_idle`)."""
        end = time.monotonic() + timeout_s
        with self._cv:
            while self._busy() and time.monotonic() < end:
                self._cv.wait(0.01)
            return not self._busy()

    def _busy(self) -> bool:
        deferred = sum(len(d) for d in self._deferred.values())
        return bool(self._queued - deferred or self._inflight)

    def close(self, timeout_s: float = 5.0) -> CloseReport:
        self.drain(timeout_s)
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
        with self._cv:
            report = CloseReport()
            for exploration_id, queue in self._queues.items():
                if queue:
                    report.by_exploration[exploration_id] = len(queue)
                    report.unwritten += len(queue)
                    report.run_finished_unwritten += sum(1 for i in queue if i.event.get("type") == "run_finished")
            for exploration_id, items in self._deferred.items():
                if items:
                    report.by_exploration[exploration_id] = report.by_exploration.get(exploration_id, 0) + len(items)
                    report.unwritten += len(items)
            self.stats["unwritten"] += report.unwritten
            if report.unwritten:
                log.warning("sink closed with %d events not written", report.unwritten)
            self._stopped = True
            return report

    # -- the worker -----------------------------------------------------------------------------

    def _ready(self, exploration_id: str, now: int) -> bool:
        return bool(self._queues.get(exploration_id)) and now >= self._blocked_until.get(exploration_id, 0)

    def _take(self) -> Optional[tuple[str, list[_Item]]]:
        now = self._clock.now_ms()
        for _ in range(len(self._order)):
            exploration_id = self._order[0]
            self._order.rotate(-1)
            if not self._ready(exploration_id, now):
                continue
            queue = self._queues[exploration_id]
            batch = [queue.popleft()]
            while queue and not batch[-1].blobs and not batch[-1].solo and not queue[0].solo and len(batch) < self._batch:
                batch.append(queue.popleft())
            return exploration_id, batch
        return None

    def _work(self) -> None:
        while True:
            with self._cv:
                taken = self._take()
                while taken is None:
                    if self._closing:
                        return  # `close` drained first: what is left could not be written
                    self._cv.wait(self._poll)
                    taken = self._take()
                self._inflight += len(taken[1])
            try:
                self._write(*taken)
            except BaseException:  # noqa: BLE001 - the worker must never die
                self.stats["worker_errors"] += 1
                log.exception("the sink failed on a batch of %s", taken[0])
            finally:
                with self._cv:
                    self._inflight -= len(taken[1])
                    self._cv.notify_all()

    def _write(self, exploration_id: str, batch: list[_Item]) -> None:
        real: dict[str, str] = {}
        events: list[dict] = []
        try:
            for item in batch:
                for provisional, data in item.blobs.items():
                    if provisional not in real:
                        real[provisional] = self._ing.put_blob(exploration_id, data).contentHash
                events.append(_with_hashes(item.event, real) if item.blobs else item.event)
            request = IngestRequest.model_validate({
                "explorationId": exploration_id, "editor": self._editor, "instanceId": self._instance,
                "events": events})
            response = self._ing.ingest(request)
        except Exception as exc:  # noqa: BLE001 - a store or service error: back off and retry the batch
            self.stats["errors"] += 1
            log.warning("sink write failed for %s: %s", exploration_id, type(exc).__name__)
            with self._cv:
                self._requeue(exploration_id, batch, 0, error=True)
            return
        outcomes = list(response.outcomes)
        with self._cv:
            self._apply(exploration_id, batch, outcomes)

    def _requeue(self, exploration_id: str, items: list[_Item], at: int, *, error: bool = False) -> None:
        """Put `items[at:]` back at the head, in order. After an error: back off, and drop an item
        that has failed too often."""
        rest = items[at:]
        queue = self._queues.setdefault(exploration_id, deque())
        if error:
            for item in rest:
                item.attempts += 1
                item.solo = True
            head = rest[0] if rest else None
            if head is not None and head.attempts > self._error_retries:
                self.stats["dropped_errors"] += 1
                log.warning("dropping an event of %s after repeated errors: %s", exploration_id,
                            head.event.get("eventId"))
                self._release(head)
                rest = rest[1:]
            self._blocked_until[exploration_id] = self._clock.now_ms() + self._retry_ms
        for item in reversed(rest):
            queue.appendleft(item)

    def _apply(self, exploration_id: str, batch: list[_Item], outcomes: list) -> None:
        if len(outcomes) != len(batch):
            log.error("ingest answered %d outcomes for %d events of %s", len(outcomes), len(batch), exploration_id)
            self.stats["outcome_mismatch"] += 1
        now = self._clock.now_ms()
        for i, item in enumerate(batch):
            out = outcomes[i] if i < len(outcomes) else None
            out = getattr(out, "root", out)
            if out is not None and out.status == "accepted":
                self._release(item)
                self.stats["written"] += 1
                if i == 0:
                    self._park_since.pop(exploration_id, None)
                    self._blocked_until.pop(exploration_id, None)
                continue
            reason = getattr(out, "reason", None)
            reason = getattr(reason, "value", reason) if out is not None else "no_outcome"
            if reason == "bad_first_event":
                self._park(exploration_id, batch, i, now)
                return
            if reason == "missing_blob" and not item.blob_retried and item.blobs:
                item.blob_retried = True
                self.stats["blob_retries"] += 1
                self._blocked_until.pop(exploration_id, None)
                self._requeue(exploration_id, batch, i)
                return
            if reason == "run_in_progress":
                self._defer(exploration_id, item)
                continue
            self._release(item)
            self.refused[str(reason)] += 1
            log.warning("event refused: id=%s type=%s reason=%s", item.event.get("eventId"),
                        item.event.get("type"), reason)

    def _park(self, exploration_id: str, batch: list[_Item], at: int, now: int) -> None:
        since = self._park_since.setdefault(exploration_id, now)
        self._requeue(exploration_id, batch, at)
        self._blocked_until[exploration_id] = now + self._retry_ms
        self.stats["park_retries"] += 1
        queue = self._queues[exploration_id]
        if len(queue) >= self._park_events or now - since >= self._park_ms:
            self._drop_parked(exploration_id)

    def _defer(self, exploration_id: str, item: _Item) -> None:
        items = self._deferred.setdefault(exploration_id, [])
        if len(items) >= self._max_deferred:
            self._release(item)
            self.stats["deferred_dropped"] += 1
            return
        items.append(item)
        self.stats["deferred"] += 1


def _hashes_in(value: Any) -> list[str]:
    """Every `contentHash` string inside an event (items of outputs, wherever they sit)."""
    found: list[str] = []
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            h = v.get("contentHash")
            if isinstance(h, str):
                found.append(h)
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return found


def _with_hashes(event: dict, real: dict[str, str]) -> dict:
    """A copy of `event` whose provisional content hashes are replaced by the stored ones."""

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: (real.get(x, x) if k == "contentHash" and isinstance(x, str) else walk(x))
                    for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(event)


def _reduced(event: dict, blobs: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    """The event with each blob replaced by a tiny preview-only blob (the item is marked
    truncated; its recorded size stays the original size)."""
    event = copy.deepcopy(event)
    new: dict[str, bytes] = {}

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            h = v.get("contentHash")
            if isinstance(h, str) and h in blobs:
                tiny = json.dumps({"output_type": v.get("outputType", "display_data"),
                                   "data": {"text/plain": v.get("preview", "")}, "metadata": {},
                                   "reduced": True}, sort_keys=True).encode("utf-8")
                fresh = "sha256:" + hashlib.sha256(tiny).hexdigest()
                new[fresh] = tiny
                v["contentHash"], v["truncated"] = fresh, True
            for x in list(v.values()):
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(event)
    return event, new


__all__ = ["CloseReport", "OUTPUT_TYPES", "OrderedSink", "PendingWrite"]

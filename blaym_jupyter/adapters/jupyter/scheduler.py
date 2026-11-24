"""A real `Scheduler` (S5): one daemon thread and a heap of due times.

The run recorder arms a timer for every message whose run it does not know yet, so a call must
be cheap: a heap push and a notify. Callbacks run on the one scheduler thread, one after the
other, so they must return quickly (the recorders hand real work to the ordered sink). A callback
that raises is counted in `errors` (by exception type, never the message) and does not stop the
thread.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections import Counter
from typing import Callable

log = logging.getLogger("blaym_jupyter.scheduler")


class _Entry:
    __slots__ = ("due", "seq", "fn", "cancelled")

    def __init__(self, due: float, seq: int, fn: Callable[[], None]):
        self.due, self.seq, self.fn, self.cancelled = due, seq, fn, False

    def __lt__(self, other: "_Entry") -> bool:
        return (self.due, self.seq) < (other.due, other.seq)


class ThreadScheduler:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._cv = threading.Condition()
        self._heap: list[_Entry] = []
        self._seq = itertools.count()
        self._live = 0  # entries in the heap that are not cancelled
        self._thread: threading.Thread | None = None
        self._closed = False
        self.errors: Counter[str] = Counter()

    def call_later(self, ms: int, fn: Callable[[], None]) -> Callable[[], None]:
        entry = _Entry(self._now() + max(ms, 0) / 1000.0, next(self._seq), fn)
        with self._cv:
            if self._closed:
                return lambda: None
            heapq.heappush(self._heap, entry)
            self._live += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="blaym-scheduler", daemon=True)
                self._thread.start()
            if self._heap[0] is entry:
                self._cv.notify()

        def cancel() -> None:
            with self._cv:
                if not entry.cancelled:
                    entry.cancelled = True
                    self._live -= 1
                    if len(self._heap) > 64 and self._live * 2 < len(self._heap):  # drop dead entries
                        self._heap = [e for e in self._heap if not e.cancelled]
                        heapq.heapify(self._heap)

        return cancel

    def pending(self) -> int:
        with self._cv:
            return self._live

    def close(self) -> None:
        """Stop the thread; timers that have not fired never will."""
        with self._cv:
            self._closed = True
            self._heap.clear()
            self._live = 0
            self._cv.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            with self._cv:
                while True:
                    if self._closed:
                        return
                    while self._heap and self._heap[0].cancelled:
                        heapq.heappop(self._heap)
                    if not self._heap:
                        self._cv.wait()
                        continue
                    wait = self._heap[0].due - self._now()
                    if wait <= 0:
                        entry = heapq.heappop(self._heap)
                        self._live -= 1
                        entry.cancelled = True  # a late cancel() of a fired timer is a no-op
                        break
                    self._cv.wait(wait)
            try:
                entry.fn()
            except Exception as exc:  # noqa: BLE001 - a callback must not stop the scheduler
                self.errors[type(exc).__name__] += 1
                log.warning("scheduled call failed: %s", type(exc).__name__)

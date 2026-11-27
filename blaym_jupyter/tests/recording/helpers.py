"""Fakes shared by the recording tests: a fake clock and scheduler, a fake service read, a recorder rig."""

from __future__ import annotations

import heapq
import time

from blaym_jupyter.recording.runs import RunRecorder, RunRecorderSettings
from blaym_jupyter.testing.fakes import IdFactory
from blaym_jupyter_types import (
    ResolveCellRevisionCodeDiffers,
    ResolveCellRevisionOk,
    ResolveCellRevisionRequest,
    ResolveCellRevisionResponse,
    ResolveCellRevisionUnknownHistory,
)

EXP = "nb-1"


class FakeTime:
    """A clock and a scheduler in one: `advance` runs the due timers in order, each at its own time."""

    def __init__(self, start_ms: int = 1_770_000_000_000):
        self.ms = start_ms
        self._timers: list = []
        self._seq = 0

    def now_ms(self) -> int:
        return self.ms

    def sleep(self, seconds: float) -> None:
        self.advance(int(seconds * 1000))

    def call_later(self, ms, fn):
        self._seq += 1
        entry = [self.ms + ms, self._seq, fn, True]
        heapq.heappush(self._timers, entry)

        def cancel():
            entry[3] = False

        return cancel

    def advance(self, ms: int) -> None:
        end = self.ms + ms
        while self._timers and self._timers[0][0] <= end:
            at, _, fn, live = heapq.heappop(self._timers)
            if live:
                self.ms = max(self.ms, at)
                fn()
        self.ms = end

    def pending(self) -> int:
        return sum(1 for t in self._timers if t[3])


class UuidIds:
    def __init__(self, prefix="rec"):
        self._f = IdFactory(prefix)

    def new(self) -> str:
        return self._f()


class FakeIngestor:
    """Answers `resolve_cell_revision` from a plain table: (history exists, {cell: [(revision, code)]})."""

    def __init__(self):
        self.history: dict[str, list[tuple[int, str]]] = {}
        self.exists = True
        self.asked: list[tuple[str, str]] = []
        self.fail = False

    def set(self, cell, revision, code):
        self.history.setdefault(cell, []).append((revision, code))

    def resolve_cell_revision(self, request: ResolveCellRevisionRequest) -> ResolveCellRevisionResponse:
        self.asked.append((request.cellId, request.code))
        if self.fail:
            raise RuntimeError("boom")
        if not self.exists:
            return ResolveCellRevisionResponse(ResolveCellRevisionUnknownHistory(status="unknown_history"))
        entries = self.history.get(request.cellId, [])
        for revision, code in reversed(entries):
            if code == request.code:
                return ResolveCellRevisionResponse(ResolveCellRevisionOk(status="ok", cellRevision=revision))
        return ResolveCellRevisionResponse(ResolveCellRevisionCodeDiffers(
            status="code_differs", currentRevision=entries[-1][0] if entries else 1))


class ListSink:
    def __init__(self):
        self.writes = []

    def submit(self, write):
        self.writes.append(write)

    def events(self, *types):
        return [e for w in self.writes for e in w.events if not types or e["type"] in types]

    def types(self):
        return [e["type"] for w in self.writes for e in w.events]


class FakeRegistry:
    def __init__(self):
        self.alive = False
        self.aliases: dict[str, str] = {}

    def is_alive(self, notebook_id):
        return self.alive

    def alias_for(self, notebook_id, model_cell_id):
        return self.aliases.get(model_cell_id, model_cell_id)


class Rig:
    def __init__(self, **settings):
        self.t = FakeTime()
        self.ing, self.sink, self.reg = FakeIngestor(), ListSink(), FakeRegistry()
        self.rec = RunRecorder(self.ing, self.sink, self.t, self.t, UuidIds(), registry=self.reg,
                               settings=RunRecorderSettings(**settings))

    def code(self, cell, revision, code):
        self.ing.set(cell, revision, code)

    def request(self, run_id, cell="c1", code="x = 1", kernel="k1"):
        self.rec.run_requested(kernel, EXP, run_id, cell, code)

    def msg(self, run_id, msg_type, content, kernel="k1"):
        self.rec.kernel_message(kernel, run_id, msg_type, content)

    def execute(self, run_id, count, *, text=None, reply="ok", kernel="k1", idle=True):
        """What a kernel sends for one cell."""
        self.msg(run_id, "execute_input", {"code": "", "execution_count": count}, kernel)
        if text is not None:
            self.msg(run_id, "stream", {"name": "stdout", "text": text}, kernel)
        if reply is not None:
            self.msg(run_id, "execute_reply", {"status": reply, "execution_count": count}, kernel)
        if idle:
            self.msg(run_id, "status", {"execution_state": "idle"}, kernel)

    def start_kernel(self, kernel="k1"):
        self.rec.kernel_started(kernel, EXP, "kid-" + kernel, "python3", "python")

    def types(self):
        return self.sink.types()


def finished(rig, run_id):
    return [e for e in rig.sink.events("run_finished") if e["payload"]["runId"] == run_id]


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()

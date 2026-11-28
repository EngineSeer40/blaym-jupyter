"""R9: the measured drag test. One `slow` test: the real service on a generated month, a recorder
thread still writing, and a 5 s sweep of the main line asking only for the newest position."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest
from blaym_notebook_types import StateAtRequest

from blaym_notebook.tests.store.bench_store import EXP, TOKEN, Month, make_service

# Measured 2026-11-28 (first run, WSL2): final state 0.8 ms after the last position, 0 dropped
# positions, slowest answer 19.2 ms, slowest write 182.5 ms. The pass marks are twice those values,
# except where doubling a tiny value is no margin: the final state (a second run gave 1.6 ms) is
# allowed 5 ms, and 0 dropped positions is allowed 5.
MAX_FINAL_STATE_S = 0.005
MAX_DROPPED = 5
MAX_SLOWEST_ANSWER_S = 0.0384
MAX_SLOWEST_WRITE_S = 0.365

RATE = 60  # positions a second
SECONDS = 5
WRITES_PER_SECOND = 20


def sweep_ticks(top: int) -> list[int]:
    frames = RATE * SECONDS
    half = frames // 2
    up = [1 + (top - 1) * i // (half - 1) for i in range(half)]
    down = [1 + (top - 1) * (frames - 1 - i) // (frames - half - 1) for i in range(half, frames)]
    return up + down


@pytest.mark.slow
def test_drag_over_the_month_while_recording():
    root = Path(tempfile.mkdtemp(prefix="drag-"))
    try:
        svc = make_service(root, recover=True)
        month = Month(svc, 7, events=7600, runs=500, chats=270, charts=50, big=3, children=4, restarts=5,
                      refusals=True)
        month.build()
        branch = month.root
        top = month.tip[branch]
        built = len(month.lat_plain)

        stop = threading.Event()

        def record():
            period = 1 / WRITES_PER_SECOND
            nxt = time.perf_counter()
            while not stop.is_set():
                month.edit(branch)
                nxt += period
                time.sleep(max(0.0, nxt - time.perf_counter()))

        def ask(tick):
            return svc.state_at(TOKEN, StateAtRequest(explorationId=EXP, branchId=branch, tick=tick))

        ticks = sweep_ticks(top)
        recorder = threading.Thread(target=record, daemon=True)
        recorder.start()
        dropped = 0
        slowest_answer = 0.0
        last = -1
        start = time.perf_counter()
        answer = None
        final_s = 0.0
        while True:
            index = min(len(ticks) - 1, int((time.perf_counter() - start) * RATE))
            if index == last:
                time.sleep(0.001)
                continue
            dropped += index - last - 1  # positions passed while the previous request was in flight
            last = index
            t0 = time.perf_counter()
            answer = ask(ticks[index])
            slowest_answer = max(slowest_answer, time.perf_counter() - t0)
            if index == len(ticks) - 1:
                # the last position is reached at (len - 1) / RATE; it is asked as soon as it is reached
                final_s = time.perf_counter() - (start + index / RATE)
                break
        stop.set()
        recorder.join()
        writes = month.lat_plain[built:] + month.lat_blob[len(month.lat_blob):]
        slowest_write = max(writes)
        fresh = ask(ticks[-1])
        print(f"\ndrag: final state {final_s * 1000:.1f} ms after the last position; dropped {dropped} of "
              f"{len(ticks)}; slowest answer {slowest_answer * 1000:.1f} ms; slowest write "
              f"{slowest_write * 1000:.1f} ms ({len(writes)} writes, {top} ticks)")
        svc.close()
        assert answer == fresh
        assert final_s <= MAX_FINAL_STATE_S
        assert dropped <= MAX_DROPPED
        assert slowest_answer <= MAX_SLOWEST_ANSWER_S
        assert slowest_write <= MAX_SLOWEST_WRITE_S
    finally:
        shutil.rmtree(root, ignore_errors=True)

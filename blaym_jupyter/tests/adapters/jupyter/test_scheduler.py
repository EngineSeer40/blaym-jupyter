"""The thread scheduler: one daemon thread, a heap, cheap calls, cancel, errors survived."""

from __future__ import annotations

import threading

from blaym_jupyter.adapters.jupyter.scheduler import ThreadScheduler

from ...recording.helpers import wait_for


def test_calls_fire_in_due_order_on_one_thread():
    s = ThreadScheduler()
    fired, threads = [], set()

    def mark(name):
        def run():
            fired.append(name)
            threads.add(threading.current_thread().name)
        return run

    s.call_later(60, mark("c"))
    s.call_later(10, mark("a"))
    s.call_later(30, mark("b"))
    assert wait_for(lambda: len(fired) == 3)
    s.close()
    assert fired == ["a", "b", "c"] and threads == {"blaym-scheduler"}


def test_a_cancelled_call_never_fires_and_cancel_after_firing_is_harmless():
    s = ThreadScheduler()
    fired, done = [], []
    s.call_later(30, lambda: fired.append("x"))()
    late = s.call_later(5, lambda: done.append(1))
    assert wait_for(lambda: done)
    late()  # already fired
    s.close()
    assert fired == [] and s.pending() == 0

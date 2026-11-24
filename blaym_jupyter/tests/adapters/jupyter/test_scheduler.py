"""The thread scheduler: one daemon thread, a heap, cheap calls, cancel, errors survived."""

from __future__ import annotations

import threading
import time

from blaym_jupyter.adapters.jupyter.scheduler import ThreadScheduler


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


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
    fired = []
    cancel = s.call_later(30, lambda: fired.append("x"))
    cancel()
    done = []
    late = s.call_later(5, lambda: done.append(1))
    assert wait_for(lambda: done)
    late()  # already fired
    time.sleep(0.06)
    s.close()
    assert fired == [] and s.pending() == 0


def test_a_failing_callback_is_counted_and_the_thread_keeps_going():
    s = ThreadScheduler()
    ok = []

    def boom():
        raise RuntimeError("secret-looking message")

    s.call_later(1, boom)
    s.call_later(5, lambda: ok.append(1))
    assert wait_for(lambda: ok)
    s.close()
    assert dict(s.errors) == {"RuntimeError": 1}


def test_an_earlier_call_added_while_waiting_wakes_the_thread():
    s = ThreadScheduler()
    fired = []
    s.call_later(5_000, lambda: fired.append("late"))
    s.call_later(10, lambda: fired.append("soon"))
    assert wait_for(lambda: fired == ["soon"], 1.0)
    s.close()


def test_many_cheap_calls_use_one_thread_and_cancelled_ones_are_dropped():
    s = ThreadScheduler()
    before = threading.active_count()
    cancels = [s.call_later(60_000, lambda: None) for _ in range(20_000)]
    assert threading.active_count() <= before + 1
    for c in cancels:
        c()
    assert s.pending() == 0 and len(s._heap) < 200
    s.close()


def test_close_stops_the_thread_and_later_calls_do_nothing():
    s = ThreadScheduler()
    s.call_later(10_000, lambda: None)
    thread = s._thread
    s.close()
    assert not thread.is_alive()
    fired = []
    s.call_later(0, lambda: fired.append(1))()
    time.sleep(0.02)
    assert fired == []

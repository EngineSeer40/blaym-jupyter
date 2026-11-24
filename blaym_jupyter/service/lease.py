"""The one-recorder lease (S4, Task 7, decision H).

One editor at a time records a notebook; its tabs (instances) share the lease. The lease is
in memory only: it is lost when the process stops, and the next request from an editor
claims it again. It is given up when no instance of the holder has been heard for longer than
the timeout, and another editor may then take over.
"""

from __future__ import annotations

import threading

from blaym_jupyter.ports.clock import Clock


class RecorderLease:
    def __init__(self, clock: Clock, timeout_ms: int = 60_000):
        if timeout_ms < 0:
            raise ValueError("the lease timeout cannot be negative")
        self._clock = clock
        self._timeout_ms = timeout_ms
        self._holders: dict[str, tuple[str, dict[str, int]]] = {}  # exploration -> (editor, {instance: last seen ms})
        self._lock = threading.Lock()

    def claim(self, exploration_id: str, editor: str, instance_id: str) -> tuple[bool, str]:
        """(granted, holder editor). Granted for no live holder, the holder's own editor
        (any instance) or a holder silent for longer than the timeout; it refreshes the lease."""
        now = self._clock.now_ms()
        with self._lock:
            held = self._holders.get(exploration_id)
            if held is not None:
                holder, instances = held
                live = now - max(instances.values()) <= self._timeout_ms
                if holder != editor and live:
                    return False, holder
                if holder == editor:
                    instances[instance_id] = now
                    return True, editor
            self._holders[exploration_id] = (editor, {instance_id: now})
            return True, editor

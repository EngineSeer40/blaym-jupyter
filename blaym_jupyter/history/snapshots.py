"""In-memory snapshot cache (S3a, Task 5; plan 8.2).

A snapshot at `(branch, tick)` is the state after applying the event at `tick`
on the line that ends in that branch. States are copied when stored and copied again
when a walk starts from one, so a stored state is never changed. Snapshots are only a
shortcut (rule 8): any change to the log that could alter a state drops it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort


class SnapshotCache:
    def __init__(self, interval: int | None = 200):
        if interval is not None and interval < 1:
            raise ValueError("interval must be a positive whole number or None")
        self.interval = interval
        self._ticks: dict[str, list[int]] = {}
        self._states: dict[tuple[str, int], object] = {}

    def wants(self, own_index: int) -> bool:
        """True for every Nth event of a branch's own events (1-based)."""
        return self.interval is not None and own_index % self.interval == 0

    def has(self, branch: str, tick: int) -> bool:
        return (branch, tick) in self._states

    def put(self, branch: str, tick: int, state) -> None:
        if (branch, tick) not in self._states:
            insort(self._ticks.setdefault(branch, []), tick)
        self._states[(branch, tick)] = state

    def latest(self, branch: str, max_tick: int):
        """The newest snapshot on `branch` at or before `max_tick`: (tick, state) or None."""
        ticks = self._ticks.get(branch)
        if not ticks:
            return None
        i = bisect_right(ticks, max_tick)
        if i == 0:
            return None
        return ticks[i - 1], self._states[(branch, ticks[i - 1])]

    def drop_from(self, tick: int) -> None:
        """Forget every snapshot at or after `tick`."""
        for branch, ticks in self._ticks.items():
            i = bisect_left(ticks, tick)
            for t in ticks[i:]:
                del self._states[(branch, t)]
            del ticks[i:]

    def clear(self) -> None:
        self._ticks.clear()
        self._states.clear()

    def __len__(self) -> int:
        return len(self._states)

    def keys(self) -> list[tuple[str, int]]:
        return sorted(self._states, key=lambda k: (k[1], k[0]))

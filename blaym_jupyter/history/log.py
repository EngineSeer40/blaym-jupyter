"""Append-only event log.

Accepts events in any order and any number of times; keeps its own copy of each. Indexes them by tick and by
event id as they arrive and records, incrementally, what the answers depend on:

* the first conflict (same event id, different content), in arrival order;
* the first tick clash (a different event id at a tick already taken), in arrival
  order of the event that clashed;
* the first missing tick.

Readers get read-only views; nothing here is ever removed or changed once added.
"""

from __future__ import annotations

from types import MappingProxyType

from blaym_jupyter_types import BlaymEvent, RebuildConflict, RebuildInvalidEvent

from .apply import unwrap


class EventLog:
    def __init__(self) -> None:
        self._by_id: dict[str, object] = {}
        self._by_tick: dict[int, object] = {}
        self._conflict: RebuildConflict | None = None
        self._clash: RebuildInvalidEvent | None = None
        self._first_missing = 1
        self._version = 0

    def add(self, events) -> int | None:
        """Add events in arrival order. Returns the lowest tick among newly accepted
        events, or None if nothing changed the log (all duplicates)."""
        lowest: int | None = None
        for raw in events:
            ev = unwrap(raw)
            key = str(ev.eventId)
            seen = self._by_id.get(key)
            if seen is not None:
                if (
                    self._conflict is None
                    and seen.model_dump(mode="json") != ev.model_dump(mode="json")
                ):
                    self._conflict = RebuildConflict(
                        status="conflict",
                        eventId=key,
                        first=BlaymEvent(seen),
                        second=BlaymEvent(ev.model_copy(deep=True)),
                    )
                    self._version += 1
                    lowest = ev.tick if lowest is None else min(lowest, ev.tick)
                continue
            # Kept events are private copies: a caller changing its event object later
            # cannot change any answer.
            ev = ev.model_copy(deep=True)
            self._by_id[key] = ev
            if ev.tick in self._by_tick:
                if self._clash is None:
                    self._clash = RebuildInvalidEvent(
                        status="invalid_event",
                        eventId=key,
                        tick=ev.tick,
                        reason="tick_clash",
                        detail=f"two different events at tick {ev.tick}",
                    )
            else:
                self._by_tick[ev.tick] = ev
                while self._first_missing in self._by_tick:
                    self._first_missing += 1
            self._version += 1
            lowest = ev.tick if lowest is None else min(lowest, ev.tick)
        return lowest

    @property
    def version(self) -> int:
        """Changes whenever anything that could change an answer is added."""
        return self._version

    @property
    def by_tick(self):
        return MappingProxyType(self._by_tick)

    @property
    def first_missing(self) -> int:
        return self._first_missing

    @property
    def conflict(self) -> RebuildConflict | None:
        return self._conflict.model_copy(deep=True) if self._conflict else None

    @property
    def clash(self) -> RebuildInvalidEvent | None:
        return self._clash.model_copy(deep=True) if self._clash else None

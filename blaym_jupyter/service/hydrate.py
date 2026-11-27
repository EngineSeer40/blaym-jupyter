"""Hydrate a `Rebuilder` and an outline from any `EventStore`.

No snapshots are persisted: the service reads a history's events once, in tick order,
exactly as stored, and keeps one `Rebuilder` per open history, appending to it as events
commit. Nothing here alters, repairs or skips a stored event, so a hole in the store is a
`missing_events` answer, a clash or a conflict the rebuilder's typed answer.

Depends only on the `EventStore` port, `history` and the contracts.
"""

from __future__ import annotations

from blaym_jupyter.history import OutlineResult, Rebuilder, build_outline
from blaym_jupyter.ports.events import EventStore


class HydratedRebuilder(Rebuilder):
    """A `Rebuilder` that also remembers the events it was given, so the outline can be
    made from them. `add` is the incremental path: pass newly committed events (in any
    order, duplicates allowed, as for `Rebuilder`); there is no reload.

    The outline is recomputed with `history.build_outline` over all events on each call
    (the service caches it, under its lock). It is not incremental: `build_outline` walks
    every branch tip to find the first invalid event.
    """

    def __init__(self, interval: int | None = 200):
        super().__init__(interval)
        self._events: list = []

    def add(self, events) -> None:
        events = list(events)
        if not events:
            return
        self._events.extend(events)
        super().add(events)

    def has_tick(self, tick: int) -> bool:
        """Whether an event is stored at `tick` (False at a hole or beyond the last tick)."""
        return tick in self._log.by_tick

    def line_events(self, branch_id: str) -> list | None:
        """The events on the line of `branch_id` in tick order: its own events and, for each
        ancestor, its events up to the cut where the child began. None when the branch is
        unknown or its ancestry cannot be resolved."""
        lin = self._index()
        own = lin.own_ticks.get(branch_id)
        if not own:
            return None
        line = lin.resolve(branch_id, own[-1])
        if not hasattr(line, "chain"):
            return None
        events = [ev for name in line.chain for ev in lin.segment(name, line.cutoff[name])[1]]
        return sorted(events, key=lambda ev: ev.tick)

    def outline_result(self) -> OutlineResult:
        """The outline and the first problem found (see `history.OutlineResult`)."""
        return build_outline(self._events)

    def outline(self):
        """The `HistoryOutline`, or None when not even tick 1 is usable."""
        return self.outline_result().outline


def hydrate(event_store: EventStore, exploration_id: str, interval: int | None = 200) -> HydratedRebuilder:
    """A rebuilder whose log equals the stored events of `exploration_id`."""
    rebuilder = HydratedRebuilder(interval)
    rebuilder.add(event_store.events_for(exploration_id))
    return rebuilder

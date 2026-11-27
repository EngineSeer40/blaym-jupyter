"""The event store port.

Add-only. The store allocates ticks (`MAX(tick) + 1` per exploration, inside the write
transaction), keeps exactly what was written, and returns it in tick order with no
skipping, repair or renumbering. It holds no product rules: it does not know what a
rebuilder, a branch rule or a secret is. Any store (SQLite, in memory, a hosted one) is
accepted by passing the shared contract suite in `tests/ports/`.

Events go in as plain JSON (a mapping without `tick`) and come out as the contract
models. They are stored as `model_dump(mode="json", exclude_none=True)`, so an optional
field that was `null` and one that was absent are the same event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from blaym_jupyter_types import BlaymEvent


class EventRejected(Exception):
    """A write was refused and nothing was written."""


class StoreBusy(EventRejected):
    """The database stayed locked past its timeout; nothing was written. Safe to retry."""


class InvalidEvent(EventRejected, ValueError):
    """The event does not validate against the contract (or names another exploration)."""


class DuplicateEventId(EventRejected):
    """An event id is already stored (or appears twice in the batch)."""

    def __init__(self, event_id: str):
        super().__init__(f"event id already stored: {event_id}")
        self.event_id = event_id


@dataclass(frozen=True)
class ExplorationRecord:
    exploration_id: str
    user_id: str
    workspace_id: str
    active_branch_id: str | None = None


@dataclass(frozen=True)
class BranchRecord:
    branch_id: str
    parent_branch_id: str | None = None
    parent_tick: int | None = None
    name: str | None = None


def canonical_event_json(event: Any) -> str:
    """The stored form of an event (a model or a mapping), for comparing content.

    `null` optionals and absent ones give the same text; the tick is part of it when present.
    """
    if isinstance(event, Mapping):
        event = BlaymEvent.model_validate(dict(event))
    root = event.root if isinstance(event, BlaymEvent) else event
    return json.dumps(root.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":"))


class EventStore(Protocol):
    def append(
        self,
        exploration_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        branches: Sequence[BranchRecord] = (),
        active_branch_id: str | None = None,
    ) -> list[Any]:
        """Write `events` (mappings without `tick`) in one transaction, giving them the
        next ticks in order; return the stored events (contract models, with ticks).
        The exploration is created from the first event's ids if it is new. `branches`
        are branch records written in the same transaction, and `active_branch_id`
        sets the active branch in it. Raises `InvalidEvent` or `DuplicateEventId`
        and then writes nothing and uses no tick."""
        ...

    def events_for(self, exploration_id: str) -> list[Any]:
        """Every stored event in tick order, exactly as written (holes stay holes)."""
        ...

    def find_event(self, exploration_id: str, event_id: str) -> Any | None:
        """The stored event with this id, or None."""
        ...

    def tip_tick(self, exploration_id: str) -> int:
        """The highest stored tick, 0 for an empty or unknown exploration."""
        ...

    def exploration(self, exploration_id: str) -> ExplorationRecord | None: ...

    def explorations(self) -> list[ExplorationRecord]: ...

    def branches(self, exploration_id: str) -> list[BranchRecord]:
        """Branch records in the order they were written."""
        ...

    def set_active_branch(self, exploration_id: str, branch_id: str) -> None:
        """Durably set the active branch (the branch must have a record)."""
        ...

    def close(self) -> None: ...

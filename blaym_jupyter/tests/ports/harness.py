"""Test harness giving the shared suites one way to open, reopen and plant events in any
event store implementation (S4, Task 2).

`load_exact` is test-only: it writes events with the ticks and branches they carry,
bypassing every check, so the S3a fixtures and delivery mutations (hole, clash,
conflict) can be reproduced in either store. It is not part of the port.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from blaym_jupyter_types import BlaymEvent

from blaym_jupyter.ports.events import ExplorationRecord, canonical_event_json
from blaym_jupyter.store.events import SqliteEventStore
from blaym_jupyter.testing.memory import MemoryEventStore

KINDS = ("memory", "sqlite")


class Harness:
    def __init__(self, kind: str):
        self.kind = kind
        self._dir = Path(tempfile.mkdtemp(prefix="blaym-store-"))
        self.store = self._open()

    def _open(self):
        if self.kind == "memory":
            return MemoryEventStore()
        return SqliteEventStore(self._dir)

    @property
    def can_reopen(self) -> bool:
        return self.kind == "sqlite"

    def reopen(self):
        """Close and open the same store again (a restart). Memory has nothing to reopen."""
        if self.can_reopen:
            self.store.close()
            self.store = self._open()
        return self.store

    def close(self) -> None:
        self.store.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    @property
    def can_hold_clash(self) -> bool:
        """Two different events at one tick: the in-memory store is keyed by tick and cannot."""
        return self.kind == "sqlite"

    def load_exact(self, exploration_id: str, events) -> None:
        """Write the events as they are (tick and branch included). A store holds one row per
        event id, so a repeated id is an error here (drop duplicates first). A clash (a second
        event at a used tick) is planted in SQLite by lifting `events_tick_uq`; the in-memory
        store cannot hold one and raises ValueError."""
        events = [e.root if isinstance(e, BlaymEvent) else e for e in events]
        ticks: dict[int, str] = {}
        clash = False
        for ev in events:
            if ticks.setdefault(ev.tick, str(ev.eventId)) != str(ev.eventId):
                clash = True
        if clash and not self.can_hold_clash:
            raise ValueError("the in-memory store cannot hold a tick clash")
        if self.kind == "memory":
            st = self.store
            for ev in events:
                text = canonical_event_json(ev)
                st._events.setdefault(exploration_id, {})[ev.tick] = text
                st._ids[str(ev.eventId)] = (exploration_id, ev.tick)
                st._explorations.setdefault(
                    exploration_id, ExplorationRecord(exploration_id, ev.userId, ev.workspaceId)
                )
            return
        conn = self.store._conn
        if clash:
            conn.execute("DROP INDEX IF EXISTS events_tick_uq")
        for ev in events:
            body = json.loads(canonical_event_json(ev))
            conn.execute(
                "INSERT OR IGNORE INTO explorations(exploration_id, user_id, workspace_id) VALUES (?,?,?)",
                (exploration_id, ev.userId, ev.workspaceId),
            )
            conn.execute(
                "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type,"
                " author_hint, author_confidence, user_id, workspace_id, body) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    exploration_id, ev.tick, str(ev.eventId), ev.branchId, ev.wallTimeMs, ev.type,
                    body.get("authorHint"), body.get("authorConfidence"), ev.userId, ev.workspaceId,
                    json.dumps(body, sort_keys=True, separators=(",", ":")),
                ),
            )
        conn.commit()

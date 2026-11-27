"""The SQLite event store. Implements `ports.EventStore`.

Events are kept as plain JSON of the contract in `body` (the authority); the other
columns only make reading cheap. Ticks are `MAX(tick) + 1` taken inside the write
transaction, so a rolled-back write leaves no hole. Nothing here updates or deletes
an event.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from blaym_jupyter_types import BlaymEvent

from blaym_jupyter.ports.events import (
    BranchRecord,
    DuplicateEventId,
    ExplorationRecord,
    InvalidEvent,
)

from .db import immediate, open_database, translate_busy


def _parse(text: str):
    return BlaymEvent.model_validate_json(text).root


def _canonical(event) -> str:
    return json.dumps(event.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":"))


class SqliteEventStore:
    def __init__(self, root: Path | str, synchronous: str = "FULL", busy_timeout: float = 5.0):
        self.root = Path(root)
        self._conn = open_database(self.root, synchronous, busy_timeout)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -------------------------------------------------------------------

    @translate_busy
    def append(self, exploration_id, events: Sequence[Mapping[str, Any]], *, branches=(), active_branch_id=None):
        parsed = []
        for raw in events:
            try:
                ev = BlaymEvent.model_validate({**raw, "tick": 1}).root
            except (TypeError, ValueError) as exc:
                raise InvalidEvent(str(exc)) from exc
            if ev.explorationId != exploration_id:
                raise InvalidEvent(f"event belongs to {ev.explorationId}, not {exploration_id}")
            parsed.append(ev)
        with self._lock:
            with immediate(self._conn) as conn:
                return self._write(conn, exploration_id, parsed, branches, active_branch_id)

    def _write(self, conn, exploration_id, parsed, branches, active_branch_id):
        if parsed and conn.execute(
            "SELECT 1 FROM explorations WHERE exploration_id=?", (exploration_id,)
        ).fetchone() is None:
            first = parsed[0]
            conn.execute(
                "INSERT INTO explorations(exploration_id, user_id, workspace_id) VALUES (?,?,?)",
                (exploration_id, first.userId, first.workspaceId),
            )
        row = conn.execute("SELECT MAX(tick) FROM events WHERE exploration_id=?", (exploration_id,)).fetchone()
        tick = row[0] or 0
        stored = []
        for ev in parsed:
            tick += 1
            ev = ev.model_copy(update={"tick": tick})
            body = _canonical(ev)
            # the generated models may hold an enum member or its plain value
            hint = getattr(ev.authorHint, "value", ev.authorHint)
            conf = getattr(ev.authorConfidence, "value", ev.authorConfidence)
            try:
                conn.execute(
                    "INSERT INTO events(exploration_id, tick, event_id, branch_id, wall_time_ms, type,"
                    " author_hint, author_confidence, user_id, workspace_id, body)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (exploration_id, tick, str(ev.eventId), ev.branchId, ev.wallTimeMs, ev.type,
                     hint, conf, ev.userId, ev.workspaceId, body),
                )
            except sqlite3.IntegrityError as exc:
                if "event_id" in str(exc):
                    raise DuplicateEventId(str(ev.eventId)) from exc
                raise
            stored.append(_parse(body))
        for b in branches:
            try:
                conn.execute(
                    "INSERT INTO branches(exploration_id, branch_id, parent_branch_id, parent_tick, name)"
                    " VALUES (?,?,?,?,?)",
                    (exploration_id, b.branch_id, b.parent_branch_id, b.parent_tick, b.name),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidEvent(f"cannot record branch {b.branch_id}: {exc}") from exc
        if active_branch_id is not None:
            self._set_active(conn, exploration_id, active_branch_id)
        elif branches and not self._exists(conn, exploration_id):
            raise InvalidEvent(f"unknown exploration {exploration_id}")
        return stored

    @staticmethod
    def _exists(conn, exploration_id) -> bool:
        return conn.execute("SELECT 1 FROM explorations WHERE exploration_id=?", (exploration_id,)).fetchone() is not None

    def _set_active(self, conn, exploration_id, branch_id) -> None:
        known = conn.execute(
            "SELECT 1 FROM branches WHERE exploration_id=? AND branch_id=?", (exploration_id, branch_id)
        ).fetchone()
        if known is None:
            raise InvalidEvent(f"unknown branch {branch_id}")
        conn.execute("UPDATE explorations SET active_branch_id=? WHERE exploration_id=?", (branch_id, exploration_id))

    def set_active_branch(self, exploration_id, branch_id) -> None:
        with self._lock:
            with immediate(self._conn) as conn:
                self._set_active(conn, exploration_id, branch_id)

    # -- reads --------------------------------------------------------------------

    def events_for(self, exploration_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM events WHERE exploration_id=? ORDER BY tick, id", (exploration_id,)
            ).fetchall()
        return [_parse(r[0]) for r in rows]

    def find_event(self, exploration_id, event_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM events WHERE exploration_id=? AND event_id=?", (exploration_id, str(event_id))
            ).fetchone()
        return _parse(row[0]) if row else None

    def tip_tick(self, exploration_id):
        with self._lock:
            row = self._conn.execute("SELECT MAX(tick) FROM events WHERE exploration_id=?", (exploration_id,)).fetchone()
        return row[0] or 0

    def exploration(self, exploration_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT exploration_id, user_id, workspace_id, active_branch_id FROM explorations"
                " WHERE exploration_id=?", (exploration_id,),
            ).fetchone()
        return ExplorationRecord(*row) if row else None

    def explorations(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT exploration_id, user_id, workspace_id, active_branch_id FROM explorations ORDER BY rowid"
            ).fetchall()
        return [ExplorationRecord(*r) for r in rows]

    def branches(self, exploration_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT branch_id, parent_branch_id, parent_tick, name FROM branches"
                " WHERE exploration_id=? ORDER BY seq", (exploration_id,),
            ).fetchall()
        return [BranchRecord(*r) for r in rows]

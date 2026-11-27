"""Server-side gap rules. Neutral: dicts in, dicts out; no Jupyter and no service.

A gap says "the recorder could not see here". Who opens and closes each kind is in
`docs/findings/s5-edges.md`. This module builds the events the server writes for others:

* `point_gap`: a gap that is written closed (started and ended together), for a recorder that lost
  sight of something already gone (dropped output, a run that could not be linked).
* `startup_gap`: the browser's last session did not end cleanly, so edits may have been lost;
  the new session writes one closed gap (source `browser`, reason `unknown`) after its first snapshot.
"""

from __future__ import annotations

import uuid
from typing import Callable, Optional

NewId = Callable[[], str]


def _default_id() -> str:
    return str(uuid.uuid4())


def point_gap(exploration_id: str, *, source: str, reason: str, at_ms: int, editor: str, origin_id: str,
              new_id: Optional[NewId] = None) -> list[dict]:
    """`[recording_gap_started, recording_gap_ended]` for one new gap id."""
    new_id = new_id or _default_id
    gap_id = new_id()
    base = {"schemaVersion": 2, "explorationId": exploration_id, "wallTimeMs": at_ms, "userId": "local",
            "workspaceId": "local", "originId": origin_id, "editor": editor}
    return [
        {**base, "eventId": new_id(), "type": "recording_gap_started",
         "payload": {"gapId": gap_id, "source": source, "reason": reason}},
        {**base, "eventId": new_id(), "type": "recording_gap_ended", "payload": {"gapId": gap_id}},
    ]


def startup_gap(exploration_id: str, *, at_ms: int, editor: str, origin_id: str,
                new_id: Optional[NewId] = None) -> list[dict]:
    """The gap of a session that did not close cleanly (source `browser`, reason `unknown`)."""
    return point_gap(exploration_id, source="browser", reason="unknown", at_ms=at_ms, editor=editor,
                     origin_id=origin_id, new_id=new_id)


__all__ = ["point_gap", "startup_gap"]

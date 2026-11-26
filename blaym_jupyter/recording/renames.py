"""A rename reported by the editor's host (S5-I): the record follows, and each recorded
notebook gets `notebook_renamed` in its own history."""

from __future__ import annotations

from .ports import Clock, Ids
from .registry import Registry
from .runs import WriteSink
from .sink import PendingWrite


def record_rename(
    registry: Registry,
    sink: WriteSink,
    clock: Clock,
    ids: Ids,
    old: str,
    new: str,
    *,
    editor: str,
    origin_id: str,
) -> list[tuple[str, str, str]]:
    """Returns (notebook id, old path, new path) for every notebook that moved (a folder
    rename moves all those under it)."""
    moved = registry.note_rename(old, new)
    for notebook_id, before, after in moved:
        if registry.recording(notebook_id):
            event = {"schemaVersion": 2, "eventId": ids.new(), "explorationId": notebook_id,
                     "wallTimeMs": clock.now_ms(), "userId": "local", "workspaceId": "local",
                     "originId": origin_id, "editor": editor, "type": "notebook_renamed",
                     "payload": {"oldPath": before, "newPath": after}}
            sink.submit(PendingWrite(notebook_id, [event], {}))
    return moved

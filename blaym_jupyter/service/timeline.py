"""The timeline index: a pure function from one stored event to its timeline entry.

No store, no lock, no clock. Chart and table kinds come from the output item's mime types, the
same rule search indexing uses (`indexing.output_kind`).
"""

from __future__ import annotations

from typing import Any

from .indexing import output_kind

_BY_TYPE = {
    "notebook_snapshot": "snapshot",
    "cell_inserted": "insert",
    "cell_deleted": "delete",
    "cell_moved": "move",
    "cell_type_changed": "type",
    "cell_source_changed": "edit",
    "run_started": "run_started",
    "kernel_started": "kernel_start",
    "kernel_restarted": "kernel_restart",
    "changed_outside_editor": "outside",
    "branch_point_created": "branch_point",
    "recording_gap_started": "gap",
    "recording_gap_ended": "gap",
    "cell_outputs_cleared": "other",
    "cell_metadata_changed": "other",
    "notebook_metadata_changed": "other",
    "outputs_cleared": "other",
    "notebook_renamed": "other",
    "notebook_copied": "other",
}
_RUN_END = {"ok": "run_ok", "error": "run_error", "interrupted": "run_interrupted"}
_OUTPUT_KIND = {"chart_text": "chart", "table_columns": "table"}


def _value(x: Any):
    return getattr(x, "value", x)


def kind_of(event) -> str:
    """The timeline kind of one stored event."""
    t = event.type
    p = event.payload
    if t == "run_finished":
        return _RUN_END.get(_value(p.status), "other")
    if t in ("output_added", "output_replaced"):
        return _OUTPUT_KIND.get(output_kind(p.item.mimeTypes), "text_output")
    if t in ("chat_message_added", "chat_message_updated"):
        sender = getattr(p, "senderKind", None)
        if sender is None:  # an update names no sender; only replies stream, so it is a reply
            return "chat_reply"
        return "chat_prompt" if _value(sender) == "person" else "chat_reply"
    return _BY_TYPE.get(t, "other")


def entry_of(event) -> tuple:
    """`(tick, wallTimeMs, kind, author, confidence, cellId, runId)` for one event."""
    links = event.links
    cell = getattr(event.payload, "cellId", None) or (links.cellId if links else None)
    run = getattr(event.payload, "runId", None) or (links.runId if links else None)
    author = _value(event.authorHint) if event.authorHint is not None else "unknown"
    conf = _value(event.authorConfidence) if event.authorConfidence is not None else None
    return event.tick, event.wallTimeMs, kind_of(event), author, conf, cell, run

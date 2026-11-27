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


class CellFold:
    """Folds a branch's line, in tick order, into the cell order, so each entry can say where its
    cell sits (`position`) and how many cells there were. Pure: `observe` takes events one by one."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def observe(self, event) -> tuple:
        """`(cell index or None, cell count)` for this event: the index after the change
        (before it for a delete), the count after."""
        t, p, order = event.type, event.payload, self.order
        if t in ("notebook_snapshot", "changed_outside_editor"):
            self.order = [c.cellId for c in p.cells]
            return None, len(self.order)
        cell = getattr(p, "cellId", None)
        index = None
        if t == "cell_inserted":
            index = min(max(p.index, 0), len(order))
            order.insert(index, p.cellId)
        elif t == "cell_deleted":
            if cell in order:
                index = order.index(cell)
                order.remove(cell)
        elif t == "cell_moved":
            if cell in order:
                order.remove(cell)
                index = min(max(p.toIndex, 0), len(order))
                order.insert(index, cell)
        elif cell in order and t in ("cell_source_changed", "cell_type_changed"):
            index = order.index(cell)
        return index, len(order)

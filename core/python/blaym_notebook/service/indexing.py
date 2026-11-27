"""What is worth indexing: a pure function from an event to documents.

No store, no port, no clock. `state_after` is the rebuilt state on the event's own branch line
after the event; `needs_state` says which events want it (`run_started`, and `cell_source_changed` when
every edit is indexed); it may be None for every other event.

Policy: the code of a cell is indexed when it is RUN (at the `run_started` tick and branch);
with `index_every_edit` also at every insert, source change, snapshot and outside change.
Chat bodies are indexed. Output text comes from each item's `searchText`, else the preview
of a text mime type. Everything is cut to `text_limit` bytes at a character boundary and
never inside a `[hidden: ...]` marker. Events are already redacted when they get here.
Markdown and raw cells are not indexed as code.
"""

from __future__ import annotations

from blaym_notebook.ports.search import SearchDocument
from blaym_notebook.privacy.kinds import WHOLE_NOTEBOOK_KINDS
from blaym_notebook.privacy.redact import MARKER_RE

TEXT_LIMIT = 65536


def needs_state(event, index_every_edit: bool = False) -> bool:
    """Whether `documents_for` wants the rebuilt state for this event."""
    if event.type in WHOLE_NOTEBOOK_KINDS:  # which of its outputs the state took
        return any(cell.outputs for cell in event.payload.cells)
    return event.type == "run_started" or (index_every_edit and event.type == "cell_source_changed")


_CHART = "application/vnd.plotly.v1+json"
_TABLE = ("application/vnd.dataresource+json", "text/html")


def cut_text(text: str, limit: int = TEXT_LIMIT) -> str:
    """At most `limit` UTF-8 bytes, ending on a character boundary and before (never inside)
    a hidden marker."""
    if len(text.encode("utf-8")) <= limit:
        return text
    cut = text.encode("utf-8")[:limit].decode("utf-8", "ignore")
    for m in MARKER_RE.finditer(text):
        if m.start() < len(cut) < m.end():
            return cut[:m.start()]
    return cut


def truncate_body(body: str, limit: int) -> str:
    """A chat body of at most `limit` UTF-8 bytes. Over the limit it is cut (character boundary,
    never inside a hidden marker) and ends with `[truncated: N bytes not stored]`, N being the
    bytes removed; the whole result, marker included, fits the limit, so cutting a result again
    changes nothing. A limit too small for the marker gets a plain cut."""
    total = len(body.encode("utf-8"))
    if total <= limit:
        return body
    for digits in range(1, 20):
        room = limit - len(f"[truncated: {'9' * digits} bytes not stored]")
        if room < 0:
            break
        kept = cut_text(body, room)
        removed = total - len(kept.encode("utf-8"))
        if len(str(removed)) == digits:
            return f"{kept}[truncated: {removed} bytes not stored]"
    return cut_text(body, limit)


def output_kind(mime_types) -> str:
    mimes = list(mime_types)
    if _CHART in mimes:
        return "chart_text"
    if any(m in _TABLE for m in mimes):
        return "table_columns"
    return "output_text"


def _item_text(item) -> str | None:
    if item.searchText:
        return item.searchText
    if item.preview and any(m.startswith("text/") for m in item.mimeTypes):
        return item.preview
    return None


def documents_for(event, state_after, *, index_every_edit: bool = False,
                  text_limit: int = TEXT_LIMIT) -> list[SearchDocument]:
    docs: list[SearchDocument] = []

    def add(kind: str, text: str | None) -> None:
        text = cut_text(text, text_limit) if text else ""
        if text.strip():
            docs.append(SearchDocument(event.explorationId, event.branchId, event.tick, kind, text))

    def add_code(code: str, cell_type) -> None:
        if getattr(cell_type, "value", cell_type) == "code":
            add("code", code)

    def add_items(items) -> None:
        for item in items or []:
            add(output_kind(item.mimeTypes), _item_text(item))

    p = event.payload
    kind = event.type
    if kind == "run_started":
        cell = None
        if state_after is not None:
            cell = next((c for c in state_after.notebook.cells if c.cellId == p.cellId), None)
        if cell is not None:
            add_code(cell.code, cell.cellType)
    elif kind in ("chat_message_added", "chat_message_updated"):
        add("chat", p.body)
    elif kind in ("output_added", "output_replaced"):
        add_items([p.item])
    elif kind == "cell_inserted":
        if index_every_edit:
            add_code(p.code, p.cellType)
        add_items(p.outputs)
    elif kind == "cell_source_changed":
        if index_every_edit and state_after is not None:
            cell = next((c for c in state_after.notebook.cells if c.cellId == p.cellId), None)
            if cell is not None:
                add_code(p.code, cell.cellType)
    elif kind in WHOLE_NOTEBOOK_KINDS:
        taken = None
        if state_after is not None:
            taken = {c.cellId: c.outputs for c in state_after.notebook.cells}
        for cell in p.cells:
            if index_every_edit:
                add_code(cell.code, cell.cellType)
            if taken is not None and taken.get(cell.cellId) != cell.outputs:
                continue  # ignored by the rebuilder, so not part of this moment
            for group in cell.outputs:
                add_items(group.items)
    return docs

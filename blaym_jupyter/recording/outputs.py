"""Neutral output building: the one place outputs become `OutputItem`s.

Two parts:

* `item_from_output`: one nbformat-shaped output (a stream, display data, an execute result or
  an error; a kernel message's content plus its `output_type` has the same shape) becomes a
  `PreparedItem`: the contract's `OutputItem` plus, for anything that is not small text, the whole
  output as canonical JSON to store as a blob. The hash in the item is provisional (of the bytes as
  given, before the service hides secrets); the sink replaces it with the hash `put_blob` answers.
  Inline rule exactly as the service has it: an item is inline when it is not truncated and
  `sizeBytes` equals the UTF-8 length of its preview. Only plain text without metadata is inline,
  so an inline execute result loses its `execution_count` (the run holds the count).
* `OutputAssembler`: the model of Jupyter's own output area for one run. It turns kernel messages
  into `output_added`, `output_replaced` and `outputs_cleared` events as the rebuilder expects
  them (consecutive chunks of one stream are one item, coalesced by a flush interval; display
  updates replace in place; "clear and wait" sets the flag and the next output replaces the list).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from blaym_jupyter_types import OutputItem

from .ports import Cancel, Scheduler
from .searchtext import SEARCH_TEXT_LIMIT, cut, search_text

HASH_PREFIX = "sha256:"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class OutputLimits:
    """`inline_bytes` and `preview_bytes` mirror the contract's `inlineOutputBytes` and
    `truncatedPreviewBytes`; a stream stops growing in memory at `preview_bytes`."""

    inline_bytes: int = 16384
    preview_bytes: int = 262144
    search_text_bytes: int = SEARCH_TEXT_LIMIT

    def __post_init__(self) -> None:
        if min(self.inline_bytes, self.preview_bytes, self.search_text_bytes) < 0:
            raise ValueError("limits cannot be negative")

    @classmethod
    def from_contract(cls, limits: Any | None = None) -> "OutputLimits":
        """From the contract's optional `StoreLimits` (None keeps each default)."""
        base = cls()
        if limits is None:
            return base

        def pick(value: Any, default: int) -> int:
            return default if value is None else value

        return cls(
            inline_bytes=pick(getattr(limits, "inlineOutputBytes", None), base.inline_bytes),
            preview_bytes=pick(getattr(limits, "truncatedPreviewBytes", None), base.preview_bytes),
        )


@dataclass
class PreparedItem:
    item: OutputItem
    blob: bytes | None = None
    inline: bool = True


def _cut_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore")


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(v for v in value if isinstance(v, str))
    return value if isinstance(value, str) else ""


def _normalise_bundle(data: Mapping[str, Any]) -> dict[str, Any]:
    """nbformat stores multi-line text as lists of lines; join them (JSON mime types stay as they are)."""
    out: dict[str, Any] = {}
    for mime, value in data.items():
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value) and not mime.endswith("json"):
            value = "".join(value)
        out[mime] = value
    return out


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _hash(raw: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(raw).hexdigest()


def _error_texts(out: Mapping[str, Any]) -> tuple[str, str]:
    """(display text, search text) of an error: the traceback, or `ename: evalue` without one."""
    head = f"{out.get('ename', '')}: {out.get('evalue', '')}".strip(": ")
    tb = out.get("traceback")
    trace = "\n".join(t for t in tb if isinstance(t, str)) if isinstance(tb, list) else ""
    shown = trace or head
    searchable = head + ("\n" + _ANSI.sub("", trace) if trace else "")
    return shown, searchable


def item_from_output(output: Mapping[str, Any], limits: OutputLimits = OutputLimits(), *, extra_bytes: int = 0) -> PreparedItem:
    """One output as a `PreparedItem`. `extra_bytes` is content that was dropped before this call
    (a stream cut at the memory limit): it counts in `sizeBytes`, marks the item truncated and
    forces a blob. Raises `ValueError` for an output of an unknown type."""
    kind = output.get("output_type")
    search: str | None = None
    if kind == "stream":
        text = _as_text(output.get("text"))
        mimes = ["text/plain"]
        full = {"name": output.get("name", "stdout"), "output_type": "stream", "text": text}
        plain, plain_only = text, True
    elif kind == "error":
        shown, search = _error_texts(output)
        mimes = ["text/plain"]
        full = {k: v for k, v in output.items() if k != "transient"}
        full["output_type"] = "error"
        plain, plain_only = shown, True
    elif kind in ("display_data", "execute_result"):
        data = _normalise_bundle(output.get("data") or {})
        metadata = output.get("metadata") or {}
        mimes = list(data)
        full = {k: v for k, v in output.items() if k != "transient"}
        full["data"] = data
        full["output_type"] = kind
        if "metadata" not in full:
            full["metadata"] = {}
        plain = _as_text(data.get("text/plain"))
        plain_only = mimes == ["text/plain"] and isinstance(data["text/plain"], str) and not metadata
        search = search_text(data, limits.search_text_bytes)
    else:
        raise ValueError(f"unknown output type {kind!r}")

    if search is not None:
        search = cut(search, limits.search_text_bytes)
    text_bytes = len(plain.encode("utf-8"))
    inline = plain_only and not extra_bytes and text_bytes <= limits.inline_bytes
    if inline:
        raw = plain.encode("utf-8")
        item = OutputItem.model_validate({
            "outputType": kind, "mimeTypes": mimes, "contentHash": _hash(raw), "preview": plain,
            "truncated": False, "sizeBytes": len(raw), "searchText": search,
        })
        return PreparedItem(item, None, True)

    blob = _canonical(full)
    if plain:
        preview = _cut_utf8(plain, limits.inline_bytes)
        truncated = bool(extra_bytes) or len(preview.encode("utf-8")) < text_bytes
    else:
        preview = _cut_utf8("[" + ", ".join(mimes) + "]" if mimes else "[no content]", limits.inline_bytes)
        truncated = bool(extra_bytes)
    size = len(blob) + extra_bytes
    if not truncated and size == len(preview.encode("utf-8")):
        truncated = True  # never let a blob-backed item look inline
    item = OutputItem.model_validate({
        "outputType": kind, "mimeTypes": mimes, "contentHash": _hash(blob), "preview": preview,
        "truncated": truncated, "sizeBytes": size, "searchText": search,
    })
    return PreparedItem(item, blob, False)


# -- the assembler ---------------------------------------------------------------------------------

EventType = Literal["output_added", "output_replaced", "outputs_cleared"]


@dataclass(frozen=True)
class AssembledEvent:
    type: EventType
    run_id: str
    cell_id: str
    output_index: int | None = None
    prepared: PreparedItem | None = None
    wait: bool | None = None

    def payload(self) -> dict[str, Any]:
        """The event's payload as the contract has it (the item's hash is still provisional)."""
        base: dict[str, Any] = {"runId": self.run_id, "cellId": self.cell_id}
        if self.type == "outputs_cleared":
            base["wait"] = bool(self.wait)
        else:
            base["outputIndex"] = self.output_index
            base["item"] = self.prepared.item.model_dump(mode="json", exclude_none=True)
        return base


@dataclass
class _Stream:
    index: int
    name: str
    parts: list[str] = field(default_factory=list)
    kept: int = 0
    total: int = 0
    dirty_text: bool = False
    dirty_size: bool = False


class OutputAssembler:
    """The output area of one run. Feed it kernel messages with `message`; it hands lists of
    `AssembledEvent` to `emit`, in order (the callback runs under the assembler's lock: it must
    not call back in). Call `finish` when the run ends: it flushes and stops the timer."""

    def __init__(
        self,
        run_id: str,
        cell_id: str,
        emit: Callable[[list[AssembledEvent]], None],
        scheduler: Scheduler,
        limits: OutputLimits = OutputLimits(),
        flush_ms: int = 250,
    ) -> None:
        self.run_id, self.cell_id = run_id, cell_id
        self._emit, self._scheduler, self._limits, self._flush_ms = emit, scheduler, limits, flush_ms
        self._lock = threading.RLock()
        self._count = 0  # how long the rebuilder's list for this run is
        self._clear_next = False
        self._stream: _Stream | None = None
        self._displays: dict[str, list[tuple[int, str]]] = {}  # display_id -> (index, output_type)
        self._timer: Cancel | None = None
        self._done = False
        self.unmatched_updates = 0
        self.ignored_messages = 0

    # -- public ---------------------------------------------------------------------------------

    def message(self, msg_type: str, content: Mapping[str, Any]) -> None:
        with self._lock:
            if self._done:
                self.ignored_messages += 1
                return
            if msg_type == "stream":
                self._stream_chunk(content)
            elif msg_type in ("display_data", "execute_result", "error"):
                self._append_output({**content, "output_type": msg_type}, _display_id(content))
            elif msg_type == "update_display_data":
                self._update_display(content)
            elif msg_type == "clear_output":
                self._clear(bool(content.get("wait", False)))
            else:
                self.ignored_messages += 1

    def finish(self) -> None:
        with self._lock:
            if self._done:
                return
            self._flush_stream(final=True)
            self._cancel_timer()
            self._done = True

    # -- internals ------------------------------------------------------------------------------

    def _out(self, events: list[AssembledEvent]) -> None:
        if events:
            self._emit(events)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    def _on_timer(self) -> None:
        with self._lock:
            self._timer = None
            if not self._done:
                self._flush_stream(final=False)

    def _stream_item(self, s: _Stream) -> PreparedItem:
        text = "".join(s.parts)
        out = {"output_type": "stream", "name": s.name, "text": text}
        return item_from_output(out, self._limits, extra_bytes=s.total - s.kept)

    def _flush_stream(self, final: bool) -> None:
        s = self._stream
        if s is None or not (s.dirty_text or (final and s.dirty_size)):
            return
        s.dirty_text = s.dirty_size = False
        self._out([AssembledEvent("output_replaced", self.run_id, self.cell_id, s.index, self._stream_item(s))])
        if final:
            self._cancel_timer()

    def _begin_append(self) -> int:
        """Before any output that is not stream growth: settle the open stream, honour a pending clear."""
        self._flush_stream(final=True)
        self._stream = None
        if self._clear_next:
            self._count = 0
            self._clear_next = False
            self._displays.clear()
        return self._count

    def _stream_chunk(self, content: Mapping[str, Any]) -> None:
        name = content.get("name") if isinstance(content.get("name"), str) else "stdout"
        text = _as_text(content.get("text"))
        s = self._stream
        if s is not None and s.name == name and not self._clear_next:
            self._grow(s, text)
            return
        index = self._begin_append()
        s = _Stream(index, name)
        self._grow(s, text, schedule=False)
        s.dirty_text = s.dirty_size = False
        self._stream = s
        self._count = index + 1
        self._out([AssembledEvent("output_added", self.run_id, self.cell_id, index, self._stream_item(s))])

    def _grow(self, s: _Stream, text: str, schedule: bool = True) -> None:
        if not text:
            return
        size = len(text) if text.isascii() else len(text.encode("utf-8"))
        s.total += size
        room = self._limits.preview_bytes - s.kept
        if room > 0:
            piece = text if size <= room else _cut_utf8(text, room)
            s.parts.append(piece)
            s.kept += len(piece) if piece.isascii() else len(piece.encode("utf-8"))
            s.dirty_text = True
        s.dirty_size = True
        if schedule and s.dirty_text and self._timer is None and self._flush_ms >= 0:
            self._timer = self._scheduler.call_later(self._flush_ms, self._on_timer)

    def _append_output(self, output: Mapping[str, Any], display_id: str | None) -> None:
        index = self._begin_append()
        prepared = item_from_output(output, self._limits)
        self._count = index + 1
        if display_id:
            self._displays.setdefault(display_id, []).append((index, output["output_type"]))
        self._out([AssembledEvent("output_added", self.run_id, self.cell_id, index, prepared)])

    def _update_display(self, content: Mapping[str, Any]) -> None:
        display_id = _display_id(content)
        targets = self._displays.get(display_id or "")
        if not targets:
            self.unmatched_updates += 1
            return
        self._flush_stream(final=True)
        events = []
        for index, output_type in targets:
            output = {"output_type": output_type, "data": content.get("data") or {},
                      "metadata": content.get("metadata") or {}}
            events.append(AssembledEvent("output_replaced", self.run_id, self.cell_id, index,
                                         item_from_output(output, self._limits)))
        self._out(events)

    def _clear(self, wait: bool) -> None:
        self._flush_stream(final=True)
        self._stream = None
        if wait:
            self._clear_next = True  # the old items stay until the next output arrives
        else:
            self._count = 0
            self._clear_next = False
            self._displays.clear()
        self._out([AssembledEvent("outputs_cleared", self.run_id, self.cell_id, wait=wait)])


def _display_id(content: Mapping[str, Any]) -> str | None:
    transient = content.get("transient")
    if isinstance(transient, Mapping) and isinstance(transient.get("display_id"), str):
        return transient["display_id"]
    return None

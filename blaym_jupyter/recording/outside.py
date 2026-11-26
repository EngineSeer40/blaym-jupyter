"""Changes made to a notebook file from outside the editor (S5, Task 18).

Neutral and pure apart from the injected clock and ids: given the file's parsed content and the
hash of what the editor last wrote, `OutsideChangeDetector.inspect` decides `ignore` or
`outside`. An outside change is a `changed_outside_editor` event holding the notebook as found in
the file (the rebuilder keeps recorded outputs of cells that already exist, Task 18 output rule).

The hash is of the *normalised* content (metadata after the S5-Q policy, cell ids where the file
has them, types, sources, cell metadata and outputs), so a rewrite that only changes formatting
(indentation, key order, `source` as a list or a string) is the same hash and records nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .metadata import apply_policy
from .outputs import OutputLimits, item_from_output
from .ports import Clock, Ids

CELL_TYPES = ("code", "markdown", "raw")


class NotebookParseError(ValueError):
    """The file is not (yet) a readable notebook, for example a partial write."""


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(x) for x in value)
    return value if isinstance(value, str) else ""


def parse_notebook(raw: bytes | str) -> dict[str, Any]:
    """The notebook JSON, or `NotebookParseError`."""
    try:
        nb = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NotebookParseError("not json") from exc
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise NotebookParseError("not a notebook")
    for cell in nb["cells"]:
        if not isinstance(cell, dict) or cell.get("cell_type") not in CELL_TYPES:
            raise NotebookParseError("bad cell")
    return nb


def _stable(nb: Mapping[str, Any]) -> bool:
    ids = [c.get("id") for c in nb["cells"]]
    minor = nb.get("nbformat_minor", 0)
    return (nb.get("nbformat") == 4 and isinstance(minor, int) and minor >= 5
            and all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids))


def content_hash(nb: Mapping[str, Any]) -> str:
    """The normalised hash of a parsed notebook (see the module note)."""
    cells = []
    for c in nb["cells"]:
        cell: dict[str, Any] = {
            "type": c["cell_type"], "source": _text(c.get("source")),
            "metadata": apply_policy(c.get("metadata"), "cell")[0],
            "outputs": c.get("outputs") if c["cell_type"] == "code" and isinstance(c.get("outputs"), list) else [],
        }
        if isinstance(c.get("id"), str):
            cell["id"] = c["id"]
        cells.append(cell)
    meta = apply_policy(nb.get("metadata"), "notebook")[0]
    raw = json.dumps({"metadata": meta, "cells": cells}, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TipCell:
    """A cell of the recorded notebook at the tip."""

    cell_id: str
    cell_type: str
    code: str


def _align(cells: Sequence[tuple[str, str]], tip: Sequence[TipCell]) -> list[Optional[str]]:
    """Blaym ids for cells without stable ids (S5-U): same type and exact source in order, then
    same type at the same position; the rest are None (new)."""
    result: list[Optional[str]] = [None] * len(cells)
    used: set[int] = set()
    last = -1
    for i, (ctype, code) in enumerate(cells):
        for j in range(last + 1, len(tip)):
            if j not in used and tip[j].cell_type == ctype and tip[j].code == code:
                result[i], last = tip[j].cell_id, j
                used.add(j)
                break
    for i, (ctype, _) in enumerate(cells):
        if result[i] is None and i < len(tip) and i not in used and tip[i].cell_type == ctype:
            result[i] = tip[i].cell_id
            used.add(i)
    return result


@dataclass
class Decision:
    kind: str  # "ignore" | "outside"
    hash: str
    event: Optional[dict[str, Any]] = None
    blobs: dict[str, bytes] = field(default_factory=dict)
    note: Optional[str] = None  # for example a count of outputs that could not be recorded


class OutsideChangeDetector:
    def __init__(self, clock: Clock, ids: Ids, *, editor: str, origin_id: str,
                 limits: OutputLimits = OutputLimits()):
        self._clock, self._ids, self._editor, self._origin = clock, ids, editor, origin_id
        self._limits = limits

    def inspect(
        self,
        notebook_id: str,
        path: str,
        nb: Mapping[str, Any],
        last_hash: Optional[str],
        cell_revisions: Mapping[str, int],
        tip: Optional[Sequence[TipCell]] = None,
    ) -> Decision:
        """`ignore` when the normalised content is what the editor last wrote. With no last hash
        (nothing known) and a tip that already matches the file in ids, types and sources, the
        file is only noted (`ignore`), so a first look never invents a change."""
        digest = content_hash(nb)
        if digest == last_hash:
            return Decision("ignore", digest)
        if last_hash is None:  # first sighting: a baseline only; the tip is never the reference
            return Decision("ignore", digest)
        raw_cells = nb["cells"]
        sources = [(c["cell_type"], _text(c.get("source"))) for c in raw_cells]
        if _stable(nb):
            ids = [c["id"] for c in raw_cells]
        else:
            aligned = _align(sources, tip or [])
            ids = [a if a is not None else self._ids.new() for a in aligned]
        tip_code = {t.cell_id: t.code for t in tip or []}
        event_id = self._ids.new()
        blobs: dict[str, bytes] = {}
        skipped = 0
        cells = []
        for cid, raw, (ctype, code) in zip(ids, raw_cells, sources):
            known = cell_revisions.get(cid)
            revision = 1 if known is None else (known if tip_code.get(cid) == code else known + 1)
            items = []
            if ctype == "code":
                for out in raw.get("outputs") or []:
                    try:
                        prepared = item_from_output(out, self._limits)
                    except (ValueError, AttributeError):
                        skipped += 1
                        continue
                    item = prepared.item.model_dump(mode="json", exclude_none=True)
                    if prepared.blob is not None:
                        blobs[item["contentHash"]] = prepared.blob
                    items.append(item)
            cells.append({
                "cellId": cid, "cellType": ctype, "code": code, "revision": revision,
                "metadata": apply_policy(raw.get("metadata"), "cell")[0],
                "outputs": [{"runId": f"snapshot:{event_id}", "items": items}] if items else [],
            })
        meta = nb.get("metadata") if isinstance(nb.get("metadata"), dict) else {}
        kernelspec = meta.get("kernelspec") if isinstance(meta.get("kernelspec"), dict) else {}
        info = meta.get("language_info") if isinstance(meta.get("language_info"), dict) else {}
        event = {
            "schemaVersion": 2, "eventId": event_id, "explorationId": notebook_id,
            "wallTimeMs": self._clock.now_ms(), "userId": "local", "workspaceId": "local",
            "originId": self._origin, "editor": self._editor, "type": "changed_outside_editor",
            "authorHint": "outside", "authorConfidence": "known",
            "payload": {
                "path": path,
                "notebookMetadata": apply_policy(meta, "notebook")[0],
                "kernelName": str(kernelspec.get("name") or ""),
                "language": str(info.get("name") or kernelspec.get("language") or ""),
                "cells": cells,
            },
        }
        return Decision("outside", digest, event, blobs, f"{skipped} outputs skipped" if skipped else None)


__all__ = ["Decision", "NotebookParseError", "OutsideChangeDetector", "TipCell", "content_hash", "parse_notebook"]

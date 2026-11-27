"""Which output items an event carries, and which of them keep their content in a blob.

One rule shared by ingest and recovery: an item is inline when its preview holds all of its
content (not truncated and `sizeBytes` equals the preview's UTF-8 length). Every other item
must reference a blob that exists.
"""

from __future__ import annotations

from blaym_jupyter.privacy.kinds import WHOLE_NOTEBOOK_KINDS


def items_of(ev) -> list:
    p = ev.payload
    if ev.type in ("output_added", "output_replaced"):
        return [p.item]
    if ev.type == "cell_inserted":
        return list(p.outputs or [])
    if ev.type in WHOLE_NOTEBOOK_KINDS:
        return [it for c in p.cells for g in c.outputs for it in g.items]
    return []


def is_inline(item) -> bool:
    return not item.truncated and item.sizeBytes == len(item.preview.encode("utf-8"))

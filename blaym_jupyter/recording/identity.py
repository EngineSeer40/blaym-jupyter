"""Notebook identity: which known record, if any, a registering notebook is (S5-I).

Pure. nbformat 4.5 and newer notebooks match by the overlap of their stable cell ids (these
survive copy and rename). Older notebooks have no stable ids, so they match only by an exact
hash of all cell sources; anything else is a new notebook.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def match_record(
    cell_ids_stable: bool,
    fingerprint: dict[str, Any],
    records: Iterable[dict[str, Any]],
    path_exists: Any,
) -> Optional[dict[str, Any]]:
    """The record this content belongs to, or None. `path_exists(path) -> bool`.

    Best match wins; on a tie a record whose path is gone is preferred (a move over a copy),
    then the earlier created, then the id (so the answer never depends on iteration order).
    """
    best: Optional[tuple[tuple[int, int, int, str], dict[str, Any]]] = None
    ids = set(fingerprint.get("cellIds") or []) if cell_ids_stable else set()
    for rec in records:
        score = _score(cell_ids_stable, ids, fingerprint, rec)
        if score <= 0:
            continue
        path = rec.get("path")
        gone = 1 if not path or not path_exists(path) else 0
        key = (score, gone, -int(rec.get("createdMs", 0)), _neg(rec["notebookId"]))
        if best is None or key > best[0]:
            best = (key, rec)
    return best[1] if best else None


def _neg(text: str) -> str:
    # larger key wins: invert so the smaller id wins a full tie
    return "".join(chr(0x10FFFF - ord(c)) for c in text)


def _score(stable: bool, ids: set[str], fp: dict[str, Any], rec: dict[str, Any]) -> int:
    rfp = rec.get("fingerprint") or {}
    if stable:
        if not rec.get("cellIdsStable"):
            return 0
        return len(ids & set(rfp.get("cellIds") or []))
    if rec.get("cellIdsStable"):
        return 0
    h = fp.get("sourceHash")
    return 1 if h and h == rfp.get("sourceHash") else 0


def fingerprint_dict(fingerprint: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"nbformat": fingerprint.nbformat, "sourceHash": fingerprint.sourceHash}
    if fingerprint.cellIds is not None:
        out["cellIds"] = list(fingerprint.cellIds)
    return out

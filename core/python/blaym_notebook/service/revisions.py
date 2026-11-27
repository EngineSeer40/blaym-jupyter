"""The committed revision index. Pure: no locks, no I/O.

For each branch and each cell it keeps the last `DEPTH` pairs `(revision, hash of the stored,
secret-hidden code)`, so a recorder can ask which revision of a cell held the code a run was
started with, even when the cell was edited again after the run was requested.

It is fed only with committed events (the service calls `observe` after a successful commit and
`replay` at hydration or when a branch is created), never with a dry run. Locking belongs to
the caller.
"""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Iterable

from blaym_notebook.privacy.kinds import WHOLE_NOTEBOOK_KINDS

DEPTH = 32


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class RevisionIndex:
    def __init__(self, depth: int = DEPTH):
        self._depth = depth
        self._branches: dict[str, dict[str, deque]] = {}

    def has_branch(self, branch_id: str) -> bool:
        return branch_id in self._branches

    def _push(self, entries: deque, revision: int, code: str) -> None:
        pair = (revision, code_hash(code))
        if not entries or entries[-1] != pair:
            entries.append(pair)

    def _cells(self, branch_id: str) -> dict[str, deque]:
        return self._branches.setdefault(branch_id, {})

    def observe(self, branch_id: str, event) -> None:
        """Take one committed event of `branch_id` (a concrete event model) into the index."""
        cells = self._cells(branch_id)
        kind, p = event.type, event.payload
        if kind in WHOLE_NOTEBOOK_KINDS:
            kept: dict[str, deque] = {}
            for cell in p.cells:
                entries = cells.get(cell.cellId) or deque(maxlen=self._depth)
                self._push(entries, cell.revision, cell.code)
                kept[cell.cellId] = entries
            self._branches[branch_id] = kept
        elif kind == "cell_inserted":
            entries = deque(maxlen=self._depth)
            self._push(entries, p.revision, p.code)
            cells[p.cellId] = entries
        elif kind == "cell_deleted":
            cells.pop(p.cellId, None)
        elif kind == "cell_source_changed":
            entries = cells.get(p.cellId)
            if entries is not None:
                self._push(entries, p.revision, p.code)

    def replay(self, branch_id: str, events: Iterable) -> None:
        """Rebuild `branch_id` from the events on its line (own events and ancestors' up to the cut)."""
        self._branches[branch_id] = {}
        for ev in events:
            self.observe(branch_id, ev)

    def resolve(self, branch_id: str, cell_id: str, hashed: str):
        """`('ok', revision)`, `('code_differs', current revision)`, `('unknown_cell',)`, or None
        for a branch the index does not know."""
        cells = self._branches.get(branch_id)
        if cells is None:
            return None
        entries = cells.get(cell_id)
        if not entries:
            return ("unknown_cell",)
        for revision, h in reversed(entries):
            if h == hashed:
                return ("ok", revision)
        return ("code_differs", entries[-1][0])

    def tip_revisions(self, branch_id: str) -> dict[str, int]:
        return {cid: entries[-1][0] for cid, entries in self._branches.get(branch_id, {}).items() if entries}

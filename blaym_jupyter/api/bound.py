"""The service with a credential bound, for the in-process recorders."""

from __future__ import annotations

from typing import Any, Iterable

from blaym_jupyter_types import (
    IngestRequest,
    IngestResponse,
    OpenGap,
    OutlineRequest,
    PutBlobResponse,
    ResolveCellRevisionRequest,
    ResolveCellRevisionResponse,
    StateAtRequest,
)

from blaym_jupyter.service.core import Service


class BoundIngestor:
    """The service with the credential bound: the `Ingestor` of the in-process recorders."""

    def __init__(self, service: Service, credential: Any):
        self._s, self._c = service, credential

    def ingest(self, request: IngestRequest) -> IngestResponse:
        return self._s.ingest(self._c, request)

    def put_blob(self, exploration_id: str, data: bytes | Iterable[bytes]) -> PutBlobResponse:
        return self._s.put_blob(self._c, exploration_id, data)

    def resolve_cell_revision(self, request: ResolveCellRevisionRequest) -> ResolveCellRevisionResponse:
        return self._s.resolve_cell_revision(self._c, request)

    def cell_revisions(self, exploration_id: str) -> dict[str, int]:
        return self._s.cell_revisions(self._c, exploration_id)


class ServiceProbe:
    """The heartbeat's view of committed state (`StateProbe`), read from the service.

    The branch read is the one holding the newest tick: until branch switching exists that is
    the branch being recorded."""

    def __init__(self, service: Service, credential: Any):
        self._s, self._c = service, credential

    def tip(self, exploration_id: str):
        """The state at the newest tick, or None before any history exists."""
        outline = self._s.outline(self._c, OutlineRequest(explorationId=exploration_id))
        if outline is None:
            return None
        branch = next((b.branchId for b in outline.branches
                       if b.ownTicks and max(t.root for t in b.ownTicks) == outline.highestTick),
                      outline.rootBranchId)
        result = self._s.state_at(self._c, StateAtRequest(
            explorationId=exploration_id, branchId=branch, tick=outline.highestTick))
        return getattr(result, "state", None)

    def runs_open(self, exploration_id: str) -> int:
        state = self.tip(exploration_id)
        return 0 if state is None else sum(1 for r in state.runs if r.status.root == "running")

    def cell_revisions(self, exploration_id: str) -> dict[str, int]:
        return self._s.cell_revisions(self._c, exploration_id)

    def open_gaps(self, exploration_id: str) -> list[OpenGap]:
        state = self.tip(exploration_id)
        if state is None:
            return []
        return [OpenGap(gapId=g.gapId, source=g.source, reason=g.reason)
                for g in state.gaps if g.endedTick is None]

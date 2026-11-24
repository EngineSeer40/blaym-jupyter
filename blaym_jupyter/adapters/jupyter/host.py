"""The Jupyter host (S5, Task 9): builds the recording parts once per server start.

The service (with start-up recovery, because this process owns recording), the notebook
registry over its folder store, the ordered sink, the run recorder and a scheduler, wired in
process. It keeps them on the server app (`web_app.settings["blaym_host"]`) and closes them in
order on shutdown: recorders first (open runs are finished as interrupted), then the sink
(drained), the scheduler, and last the stores.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

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

from blaym_jupyter.api import Recording
from blaym_jupyter.local import SystemClock, open_service
from blaym_jupyter.recording.outputs import OutputLimits
from blaym_jupyter.recording.registry import Registry
from blaym_jupyter.recording.registry_store import FolderRegistryStore
from blaym_jupyter.recording.runs import RunRecorder
from blaym_jupyter.recording.sink import OrderedSink
from blaym_jupyter.service.core import Service

from .auth import Credential, JupyterAuthorizer
from .config import BlaymConfig
from .scheduler import ThreadScheduler

log = logging.getLogger("blaym_jupyter.host")

# The in-process writers are one more "tab" of the JupyterLab editor for the service lease
# (the lease is per editor), with an instance id no browser uses.
SERVER_EDITOR = "jupyterlab"
SERVER_INSTANCE_ID = "blaym-server"


class RandomIds:
    """Random ids for notebook records (never a hash of a path)."""

    def new(self) -> str:
        return uuid.uuid4().hex


class BoundIngestor:
    """The service with the credential bound: the `Ingestor` of the in-process recorders."""

    def __init__(self, service: Service, credential: Credential):
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

    The branch read is the one holding the newest tick: until branch switching exists (S8) that is
    the branch being recorded."""

    def __init__(self, service: Service, credential: Credential):
        self._s, self._c = service, credential

    def _tip(self, exploration_id: str):
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
        state = self._tip(exploration_id)
        return 0 if state is None else sum(1 for r in state.runs if r.status.root == "running")

    def cell_revisions(self, exploration_id: str) -> dict[str, int]:
        return self._s.cell_revisions(self._c, exploration_id)

    def open_gaps(self, exploration_id: str) -> list[OpenGap]:
        state = self._tip(exploration_id)
        if state is None:
            return []
        return [OpenGap(gapId=g.gapId, source=g.source, reason=g.reason)
                for g in state.gaps if g.endedTick is None]


class BlaymHost:
    def __init__(self, config: BlaymConfig, *, root_dir: str | Path | None = None):
        root = Path(config.root).expanduser()
        self.config = config
        self.authorizer = JupyterAuthorizer()
        self._credential = self.authorizer.mint()  # for the in-process recorders only
        self._closed = False
        self._close_lock = threading.Lock()
        clock = SystemClock()
        self.service = open_service(root, self.authorizer, limits=config.store_limits(), clock=clock, recover=True)
        try:
            base = Path(root_dir) if root_dir is not None else Path.cwd()
            self.registry = Registry(
                FolderRegistryStore(root), clock, RandomIds(),
                probe=ServiceProbe(self.service, self._credential),
                alive_window_ms=config.alive_window_ms,
                path_exists=lambda p: (base / p).exists(),
            )
            self.scheduler = ThreadScheduler()
            ingestor = BoundIngestor(self.service, self._credential)
            self.sink = OrderedSink(
                ingestor, clock, editor=SERVER_EDITOR, instance_id=SERVER_INSTANCE_ID,
                max_queued_events=config.max_queued_events,
                max_pending_blob_bytes=config.max_pending_blob_mb * 1024 * 1024)
            limits = OutputLimits.from_contract(config.store_limits())
            self.run_recorder = RunRecorder(
                ingestor, self.sink, clock, self.scheduler, RandomIds(), registry=self.registry,
                settings=config.run_settings(), limits=limits, on_idle=self.sink.on_idle)
            self.recording = Recording(self.service, self.registry, limits)
            self.registry.scan(lambda p: (base / p).exists())
        except BaseException:
            self.service.close()
            raise

    def credential(self) -> Credential:
        """A credential for a request Jupyter has already signed in (call after `@authenticated`)."""
        return self.authorizer.mint()

    def close(self) -> None:
        """Idempotent. Each step runs even if an earlier one failed."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        steps: list[Callable[[], Any]] = [self.run_recorder.close, self.sink.close, self.scheduler.close,
                                          self.service.close]
        for step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                log.warning("closing the recording host: %s failed with %s",
                            getattr(step, "__qualname__", "step"), type(exc).__name__)

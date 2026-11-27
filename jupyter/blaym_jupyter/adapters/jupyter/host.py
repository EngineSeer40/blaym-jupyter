"""The Jupyter host: builds the recording parts once per server start.

The service (with start-up recovery, because this process owns recording), the notebook
registry over its folder store, the ordered sink, the run recorder and a scheduler, wired in
process. It keeps them on the server app (`web_app.settings["blaym_host"]`) and closes them in
order on shutdown: recorders first (open runs are finished as interrupted), then the sink
(drained), the scheduler, and last the stores.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from blaym_notebook.api import Recording
from blaym_notebook.api.bound import BoundIngestor, ServiceProbe
from blaym_notebook.local import open_service
from blaym_notebook.ports.clock import SystemClock
from blaym_notebook.recording.ids import RandomIds
from blaym_notebook.recording.outputs import OutputLimits
from blaym_notebook.recording.registry import Registry
from blaym_notebook.recording.registry_store import FolderRegistryStore
from blaym_notebook.recording.runs import RunRecorder
from blaym_notebook.recording.scheduler import ThreadScheduler
from blaym_notebook.recording.sink import OrderedSink
from blaym_notebook.service.core import Service

from .auth import Credential, JupyterAuthorizer
from .config import BlaymConfig

log = logging.getLogger("blaym_jupyter.host")

# The in-process writers are one more "tab" of the JupyterLab editor for the service lease
# (the lease is per editor), with an instance id no browser uses.
SERVER_EDITOR = "jupyterlab"
SERVER_INSTANCE_ID = "blaym-server"


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
                settings=config.run_settings(), limits=limits)
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

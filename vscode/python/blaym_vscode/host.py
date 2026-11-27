"""The standalone host: builds the recording parts once per start.

The service (with start-up recovery, because this process owns recording), the notebook registry
over its folder store, the ordered sink, a scheduler and the core file watcher, wired in process
as the Jupyter host does, but with no run recorder (the editor reports runs through the API).
`close()` stops the watcher first (it writes through the sink), then the sink (drained), the
scheduler, and last the stores. One process owns a store folder: `take_store_lock`.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, TextIO

from blaym_notebook.api import Recording
from blaym_notebook.api.bound import BoundIngestor, ServiceProbe, state_source
from blaym_notebook.local import open_service
from blaym_notebook.ports.clock import SystemClock
from blaym_notebook.recording.ids import RandomIds
from blaym_notebook.recording.outputs import OutputLimits
from blaym_notebook.recording.outside import OutsideChangeDetector
from blaym_notebook.recording.registry import Registry
from blaym_notebook.recording.registry_store import FolderRegistryStore
from blaym_notebook.recording.scheduler import ThreadScheduler
from blaym_notebook.recording.sink import OrderedSink
from blaym_notebook.recording.watcher import FileWatcher

from .auth import TokenAuthorizer

log = logging.getLogger("blaym_vscode.host")

EDITOR = "vscode"
INSTANCE_ID = "blaym-service"
LOCK_NAME = "vscode-service.lock"


class StoreInUse(Exception):
    """Another process holds this store folder."""


def take_store_lock(root: Path) -> TextIO:
    """Lock `<root>/vscode-service.lock` for the life of the process (the OS drops it on exit).
    Keep the returned file open; raises `StoreInUse` when another process holds it."""
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / LOCK_NAME, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise StoreInUse(str(root)) from exc
    return handle


class VscodeHost:
    def __init__(self, root: str | Path, token: str):
        root = Path(root)
        self.authorizer = TokenAuthorizer(token)
        credential = self.authorizer.credential()
        self._closed = False
        self._close_lock = threading.Lock()
        clock, ids = SystemClock(), RandomIds()
        self.service = open_service(root, self.authorizer, clock=clock, recover=True)
        try:
            self.registry = Registry(FolderRegistryStore(root), clock, ids,
                                     probe=ServiceProbe(self.service, credential))
            self.scheduler = ThreadScheduler()
            self.sink = OrderedSink(BoundIngestor(self.service, credential), clock,
                                    editor=EDITOR, instance_id=INSTANCE_ID)
            limits = OutputLimits()
            detector = OutsideChangeDetector(clock, ids, editor=EDITOR, origin_id="blaym-files", limits=limits)
            # notebook paths are absolute file paths here, so the base folder never applies
            self.watcher = FileWatcher(self.registry, self.sink, detector,
                                       state_source(self.service, credential), self.scheduler, ids, Path(os.sep))
            self.recording = Recording(self.service, self.registry, limits, sink=self.sink, clock=clock, ids=ids,
                                       files=self.watcher, editor=EDITOR)
            self.registry.scan(os.path.exists)
            self.watcher.start()
        except BaseException:
            self.service.close()
            raise

    def close(self) -> None:
        """Idempotent. Each step runs even if an earlier one failed."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        steps: list[Callable[[], Any]] = [self.watcher.close, self.sink.close, self.scheduler.close,
                                          self.service.close]
        for step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                log.warning("closing the service: %s failed with %s",
                            getattr(step, "__qualname__", "step"), type(exc).__name__)

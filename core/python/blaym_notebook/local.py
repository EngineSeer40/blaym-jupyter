"""The composition point: the only module that knows both the SQLite store and the service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from blaym_notebook_types import StoreLimits

from blaym_notebook.ports.auth import Authorizer
from blaym_notebook.ports.clock import SystemClock
from blaym_notebook.service.core import Service
from blaym_notebook.store.blobs import FileBlobStore
from blaym_notebook.store.events import SqliteEventStore
from blaym_notebook.store.search import SqliteSearchIndex


def open_service(
    root: Path | str,
    authorizer: Authorizer,
    *,
    limits: StoreLimits | None = None,
    synchronous: str = "FULL",
    clock: Any = None,
    recover: bool = True,
    **service_options: Any,
) -> Service:
    """A service over a SQLite event store and a file blob store under `root`.
    `service_options` go to `Service` (`interval`, `new_id`, `lease_timeout_ms`).
    `recover=False` skips the start-up recovery (tests).
    Call `close()` on the service to close the stores and the search index."""
    events = SqliteEventStore(root, synchronous=synchronous)
    try:
        blobs = FileBlobStore(root, synchronous=synchronous)
    except BaseException:
        events.close()
        raise
    try:
        search = SqliteSearchIndex(root)
    except BaseException:
        events.close()
        blobs.close()
        raise
    service = Service(events, blobs, authorizer, clock or SystemClock(), limits=limits,
                      search_index=search, **service_options)
    if recover:  # close what a stop or crash left open (see service/recovery.py)
        try:
            service.recover()
        except BaseException:
            service.close()
            raise
    return service

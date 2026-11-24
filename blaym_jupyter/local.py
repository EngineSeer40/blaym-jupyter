"""The composition point: the only module that knows both the SQLite store and the service."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from blaym_jupyter_types import StoreLimits

from blaym_jupyter.ports.auth import Authorizer
from blaym_jupyter.service.core import Service
from blaym_jupyter.store.blobs import FileBlobStore
from blaym_jupyter.store.events import SqliteEventStore
from blaym_jupyter.store.search import SqliteSearchIndex


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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

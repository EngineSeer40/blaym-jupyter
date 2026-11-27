"""Jupyter contents events. Only renames matter here: the server's path is the
truth (the browser's lags). A copy needs nothing now; the registry recognises it when it is
next registered. Blaym writes nothing into notebook files."""

from __future__ import annotations

import logging
from typing import Any

from blaym_notebook.ports.clock import SystemClock
from blaym_notebook.recording.ids import RandomIds
from blaym_notebook.recording.renames import record_rename

from .host import SERVER_EDITOR

log = logging.getLogger("blaym_jupyter.contents")


def install_contents_listener(server_app: Any, host: Any, kernels: Any) -> None:
    manager = server_app.contents_manager
    clock, ids = SystemClock(), RandomIds()

    async def on_contents_event(logger: Any, schema_id: str, data: dict) -> None:
        try:
            if data.get("action") != "rename":
                return
            old, new = str(data["source_path"]).strip("/"), str(data["path"]).strip("/")
            for _, before, after in record_rename(
                    host.registry, host.sink, clock, ids, old, new,
                    editor=SERVER_EDITOR, origin_id="blaym-contents"):
                kernels.note_rename(before, after)
        except Exception as exc:  # noqa: BLE001 - never disturbs the contents request
            log.warning("a rename could not be recorded: %s", type(exc).__name__)

    manager.event_logger.add_listener(schema_id=manager.event_schema_id, listener=on_contents_event)

"""Changes to notebook files from outside Jupyter: the Jupyter binding of the file watcher.

Telling the editor's saves from other writers: Jupyter's contents manager runs registered
pre-save and post-save hooks around its own write (`register_pre_save_hook` and
`register_post_save_hook`, jupyter_server 2.x; they compose with any hook the user configured).
The pre-save hook marks the path as being saved before the file is replaced; the post-save hook
reads the file back right after the editor's own write. The watcher itself lives in
`blaym_notebook.recording.watcher`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from blaym_notebook.api.bound import state_source
from blaym_notebook.recording.outside import OutsideChangeDetector
from blaym_notebook.recording.watcher import FileWatcher

log = logging.getLogger("blaym_jupyter.files")


def install_file_watcher(server_app: Any, host: Any) -> FileWatcher:
    from blaym_notebook.ports.clock import SystemClock
    from blaym_notebook.recording.ids import RandomIds
    from blaym_notebook.recording.scheduler import ThreadScheduler

    from .host import SERVER_EDITOR

    ids = RandomIds()
    detector = OutsideChangeDetector(SystemClock(), ids, editor=SERVER_EDITOR, origin_id="blaym-files",
                                     limits=host.run_recorder._limits)
    watcher = FileWatcher(host.registry, host.sink, detector, state_source(host.service, host.credential()), ThreadScheduler(), ids,
                          Path(server_app.root_dir))
    manager = server_app.contents_manager
    root = Path(manager.root_dir)

    def pre_save(model: dict, path: str, contents_manager: Any = None, **_: Any) -> None:
        if model.get("type") == "notebook":
            watcher.save_started(path.strip("/"))

    def post_save(model: dict, os_path: str, contents_manager: Any = None, **_: Any) -> None:
        if model.get("type") == "notebook":
            watcher.save_finished(os.path.relpath(os_path, root))

    manager.register_pre_save_hook(pre_save)
    manager.register_post_save_hook(post_save)
    watcher.start()
    log.info("watching notebook files for outside changes: %s", watcher.status()["reason"])
    return watcher

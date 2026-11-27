"""Throwaway demo (S5, Task 10; not shipped): record a short scripted session through the real host.

Starts a real Jupyter server (random port, temporary folders) with the Blaym extension, plays the
browser over HTTP with the token (register, first snapshot, an edit), runs three cells through the
kernel websocket of a real ipykernel, then prints the rebuilt notebook at three ticks.

    .venv/bin/python scripts/demo_record.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tornado.websocket  # noqa: E402
from jupyter_server.serverapp import ServerApp  # noqa: E402

from blaym_jupyter.adapters.jupyter import SETTINGS_KEY  # noqa: E402
from blaym_notebook_types import OutlineRequest, StateAtRequest  # noqa: E402

CELLS = [("c1", "x = 20\nprint('x is', x)"), ("c2", "1 / 0"),
         ("c3", "from IPython.display import display\ndisplay({'text/plain': 'a table', 'text/html': '<table/>'}, raw=True)")]


def http(base, token, method, path, body=None):
    req = urllib.request.Request(f"{base}{path}", method=method, headers={"Authorization": f"token {token}"},
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"null")


def event(nb, type_, payload):
    return {"schemaVersion": 2, "eventId": uuid.uuid4().hex, "explorationId": nb, "wallTimeMs": int(time.time() * 1000),
            "userId": "local", "workspaceId": "local", "originId": "demo-browser", "type": type_, "payload": payload}


def show(host, nb, tick, label):
    cred = host.credential()
    outline = host.service.outline(cred, OutlineRequest(explorationId=nb))
    state = host.service.state_at(cred, StateAtRequest(explorationId=nb, branchId=outline.rootBranchId, tick=tick)).state
    print(f"\n=== {label} (tick {tick} of {outline.highestTick}) ===")
    print(f"kernel: {state.kernel.kernelName} {state.kernel.status}; gaps: {len(state.gaps)}")
    for cell in state.notebook.cells:
        run = next((r for r in state.runs if r.cellId == cell.cellId), None)
        print(f"[{cell.cellId}] rev {cell.revision} run {run.status.root if run else '-'}: {cell.code!r}")
        for group in cell.outputs:
            for item in group.items:
                print(f"      out {item.outputType}: {item.preview[:60]!r}")


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="blaym-demo-"))
    os.environ.update(JUPYTER_CONFIG_DIR=str(work / "cfg"), JUPYTER_DATA_DIR=str(work / "data"),
                      JUPYTER_RUNTIME_DIR=str(work / "rt"))
    token = "demo-token"
    app = ServerApp.instance()
    app.initialize(argv=[
        f"--ServerApp.root_dir={work}", f"--ServerApp.token={token}", "--ServerApp.open_browser=False",
        "--ServerApp.port=18900", "--ServerApp.port_retries=50", "--ServerApp.allow_unauthenticated_access=False",
        "--ServerApp.jpserver_extensions={'blaym_jupyter': True}",
        f"--BlaymConfig.root={work / 'blaym'}", "--BlaymConfig.reply_grace_ms=1500"], starter_extension=None)
    base = f"http://127.0.0.1:{app.port}{app.base_url}"
    host = app.web_app.settings[SETTINGS_KEY]
    loop = asyncio.get_running_loop()
    call = lambda *a: loop.run_in_executor(None, http, base, token, *a)  # noqa: E731
    try:
        reg = await call("POST", "blaym-jupyter/api/notebooks/register", {
            "path": "demo.ipynb", "editor": "jupyterlab", "instanceId": "demo-tab", "cellIdsStable": True,
            "setRecording": True, "cellAliases": {},
            "fingerprint": {"nbformat": "4.5", "sourceHash": "h", "cellIds": [c for c, _ in CELLS]}})
        nb = reg["notebookId"]
        snap = event(nb, "notebook_snapshot", {"path": "demo.ipynb", "notebookMetadata": {}, "kernelName": "python3",
                                               "language": "python", "cells": [
            {"cellId": c, "cellType": "code", "code": "pass", "revision": 1, "metadata": {}, "outputs": []}
            for c, _ in CELLS]})
        edits = [event(nb, "cell_source_changed", {"cellId": c, "code": code, "revision": 2}) for c, code in CELLS]
        await call("POST", "blaym-jupyter/api/ingest", {"explorationId": nb, "editor": "jupyterlab",
                                                        "instanceId": "demo-tab", "events": [snap, *edits]})
        sess = await call("POST", "api/sessions", {"path": "demo.ipynb", "name": "demo.ipynb", "type": "notebook",
                                                   "kernel": {"name": "python3"}})
        kid = sess["kernel"]["id"]
        ws = await tornado.websocket.websocket_connect(
            f"ws://127.0.0.1:{app.port}{app.base_url}api/kernels/{kid}/channels?token={token}")
        adapter = app.web_app.settings["blaym_kernels"]
        while not adapter.is_warm(kid):
            await asyncio.sleep(0.1)
        for cell_id, code in CELLS:
            ws.write_message(json.dumps({"channel": "shell", "header": {
                "msg_id": uuid.uuid4().hex, "msg_type": "execute_request", "username": "", "session": uuid.uuid4().hex,
                "date": "", "version": "5.3"}, "parent_header": {}, "metadata": {"cellId": cell_id}, "buffers": [],
                "content": {"code": code, "silent": False, "store_history": True, "user_expressions": {},
                            "allow_stdin": False, "stop_on_error": True}}))
            await asyncio.sleep(2.5)
        await asyncio.sleep(2)
        host.sink.drain()
        outline = host.service.outline(host.credential(), OutlineRequest(explorationId=nb))
        top = outline.highestTick
        show(host, nb, 2, "after the first snapshot and edits begin")
        show(host, nb, max(3, top // 2), "in the middle")
        show(host, nb, top, "now")
    finally:
        await app._cleanup()
        print("\nrecorded under", work)


if __name__ == "__main__":
    asyncio.run(main())

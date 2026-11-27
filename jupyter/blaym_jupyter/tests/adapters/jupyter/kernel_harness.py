"""Test harness for the kernel adapter against a real server and a real ipykernel: a Python stand-in
for the browser (registers the notebook, sends the first snapshot over HTTP with the token) and a
scripted client of the kernel websocket (both protocols)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable, Optional

from blaym_jupyter.adapters.jupyter import SETTINGS_KEY
from blaym_jupyter.adapters.jupyter.kernels import V1_PROTOCOL
from blaym_notebook_types import OutlineRequest, StateAtRequest

API = ("blaym-jupyter", "api")
PATH = "nb.ipynb"


async def api(jp_fetch, route, body=None, params=None, method=None):
    parts = API + tuple(route.strip("/").split("/"))
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    return await jp_fetch(*parts, params=params, body=body, method=method or ("POST" if body is not None else "GET"))


def snapshot_event(exploration_id: str, cells: list[tuple[str, str]], event_id: Optional[str] = None) -> dict:
    return {
        "schemaVersion": 2, "eventId": event_id or uuid.uuid4().hex, "explorationId": exploration_id,
        "wallTimeMs": int(time.time() * 1000), "userId": "local", "workspaceId": "local",
        "originId": "test-browser", "type": "notebook_snapshot",
        "payload": {"path": PATH, "notebookMetadata": {}, "kernelName": "python3", "language": "python",
                    "cells": [{"cellId": cid, "cellType": "code", "code": code, "revision": 1, "metadata": {},
                               "outputs": []} for cid, code in cells]},
    }


class Browser:
    """What the editor's recorder does over HTTP: register, then send the first snapshot."""

    def __init__(self, jp_fetch, instance="tab-1"):
        self.fetch, self.instance, self.notebook_id = jp_fetch, instance, ""

    async def open(self, cells: list[tuple[str, str]], recording=True) -> str:
        reg = await api(self.fetch, "notebooks/register", {
            "path": PATH, "editor": "jupyterlab", "instanceId": self.instance, "cellIdsStable": True,
            "setRecording": recording,
            "fingerprint": {"nbformat": "4.5", "sourceHash": "h", "cellIds": [c for c, _ in cells]},
            "cellAliases": {}})
        self.notebook_id = json.loads(reg.body)["notebookId"]
        await self.send([snapshot_event(self.notebook_id, cells)])
        return self.notebook_id

    async def send(self, events: list[dict]):
        r = await api(self.fetch, "ingest", {"explorationId": self.notebook_id, "editor": "jupyterlab",
                                             "instanceId": self.instance, "events": events})
        out = json.loads(r.body)
        assert all(o["status"] == "accepted" for o in out["outcomes"]), out

    async def edit(self, cell_id: str, code: str, revision: int):
        await self.send([{
            "schemaVersion": 2, "eventId": uuid.uuid4().hex, "explorationId": self.notebook_id,
            "wallTimeMs": int(time.time() * 1000), "userId": "local", "workspaceId": "local",
            "originId": "test-browser", "type": "cell_source_changed",
            "payload": {"cellId": cell_id, "code": code, "revision": revision}}])


class KernelWs:
    """A scripted client of `/api/kernels/<id>/channels` in one of the two protocols."""

    def __init__(self, ws, protocol: Optional[str]):
        self.ws, self.protocol, self.session = ws, protocol, uuid.uuid4().hex
        self.received: list[dict] = []
        self._drain = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            while True:
                raw = await self.ws.read_message()
                if raw is None:
                    return
                if isinstance(raw, bytes):
                    from jupyter_server.services.kernels.connection.base import deserialize_msg_from_ws_v1
                    channel, parts = deserialize_msg_from_ws_v1(raw)
                    h, p, m, c = (json.loads(x) for x in parts[:4])
                    self.received.append({"channel": channel, "header": h, "parent_header": p, "content": c})
                else:
                    self.received.append(json.loads(raw))
        except Exception:  # noqa: BLE001 - the test closed the socket
            return

    def execute(self, code: str, cell_id: Optional[str], silent: bool = False) -> str:
        msg_id = uuid.uuid4().hex
        header = {"msg_id": msg_id, "msg_type": "execute_request", "username": "", "session": self.session,
                  "date": "2026-11-24T00:00:00Z", "version": "5.3"}
        metadata = {"cellId": cell_id} if cell_id else {}
        content = {"code": code, "silent": silent, "store_history": True, "user_expressions": {},
                   "allow_stdin": False, "stop_on_error": True}
        if self.protocol == V1_PROTOCOL:
            parts = [b"shell"] + [json.dumps(x).encode() for x in (header, {}, metadata, content)]
            offsets, pos = [], 8 * (len(parts) + 2)
            for p in parts:
                offsets.append(pos)
                pos += len(p)
            offsets.append(pos)
            frame = len(offsets).to_bytes(8, "little") + b"".join(o.to_bytes(8, "little") for o in offsets) \
                + b"".join(parts)
            self.ws.write_message(frame, binary=True)
        else:
            self.ws.write_message(json.dumps({"channel": "shell", "header": header, "parent_header": {},
                                              "metadata": metadata, "content": content, "buffers": []}))
        return msg_id

    def close(self):
        self.ws.close()
        self._drain.cancel()


def tip_state(host, notebook_id: str):
    """The rebuilt state at the newest tick, read from the real store."""
    cred = host.credential()
    outline = host.service.outline(cred, OutlineRequest(explorationId=notebook_id))
    if outline is None:
        return None
    result = host.service.state_at(cred, StateAtRequest(
        explorationId=notebook_id, branchId=outline.rootBranchId, tick=outline.highestTick))
    return getattr(result, "state", None)


async def wait_for(cond: Callable[[], Any], timeout: float = 30.0, step: float = 0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = cond()
        if value:
            return value
        await asyncio.sleep(step)
    value = cond()
    assert value, "timed out waiting for a condition"
    return value


def run_for(host, notebook_id: str, cell_id: str, index: int = -1):
    state = tip_state(host, notebook_id)
    runs = [r for r in (state.runs if state else []) if r.cellId == cell_id]
    return runs[index] if runs else None


def settings_host(jp_serverapp):
    return jp_serverapp.web_app.settings[SETTINGS_KEY]

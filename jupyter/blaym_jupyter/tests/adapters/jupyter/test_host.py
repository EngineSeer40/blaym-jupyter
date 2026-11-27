"""The Jupyter host with a real `jp_serverapp` (pytest-jupyter): sign-in, the routes over HTTP,
the one active recorder instance, records that survive a restart, and shutdown."""

from __future__ import annotations

import json
import sqlite3

import pytest
from tornado.httpclient import HTTPClientError

from blaym_jupyter.adapters.jupyter import SETTINGS_KEY
from blaym_jupyter.adapters.jupyter.auth import JupyterAuthorizer
from blaym_notebook.api import ROUTES
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import recorder
from blaym_notebook_types import (
    IngestResponse,
    NotebookStatus,
    PrepareOutputsResponse,
    PutBlobResponse,
)

API = ("blaym-jupyter", "api")
_ANY = {"explorationId": "e", "notebookId": "n", "name": "index.html", "contentHash": "sha256:" + "0" * 64}


def registration(path="a.ipynb", instance="tab-1", **extra):
    return {"path": path, "editor": "jupyterlab", "instanceId": instance, "cellIdsStable": True,
            "fingerprint": {"nbformat": "4.5", "sourceHash": "h", "cellIds": ["c1"]}, "cellAliases": {}, **extra}


async def call(jp_fetch, route, body=None, params=None, **kw):
    parts = API + tuple(route.strip("/").split("/"))
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    return await jp_fetch(*parts, params=params, body=body, **kw)


async def register(jp_fetch, instance="tab-1", path="a.ipynb", **extra):
    r = await call(jp_fetch, "notebooks/register", registration(path, instance, **extra), method="POST")
    return NotebookStatus.model_validate_json(r.body)


async def test_every_route_refuses_an_unauthenticated_request(jp_fetch, jp_serverapp):
    host = jp_serverapp.web_app.settings[SETTINGS_KEY]
    for route in ROUTES:
        body = b"{}" if route.method in ("POST", "PUT") else None
        with pytest.raises(HTTPClientError) as err:
            await jp_fetch(*API, *route.path.format(**_ANY).strip("/").split("/"), method=route.method, body=body,
                           headers={"Authorization": ""})
        assert err.value.code == 403, route.name
    assert host.registry.notebook_id_for_path("a.ipynb") is None
    a, b = JupyterAuthorizer(), JupyterAuthorizer()  # a credential is good only for its own authorizer
    assert a.check(a.mint()) and not a.check(b.mint())
    assert not any(a.check(other) for other in ("token", None, b"x", object(), 1))
    assert "key" not in repr(a.mint()).lower()


async def test_the_routes_work_with_the_token_and_round_trip(jp_fetch):
    info = await call(jp_fetch, "info")
    assert json.loads(info.body)["apiVersion"] == 1

    status = await register(jp_fetch)
    assert status.active and not status.recording
    exp = status.explorationId

    beat = await call(jp_fetch, f"notebooks/{exp}/status", params={"instanceId": "tab-1"})
    assert NotebookStatus.model_validate_json(beat.body).active

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    ingest = await call(jp_fetch, "ingest", {
        "explorationId": exp, "editor": "jupyterlab", "instanceId": "tab-1",
        "events": [{**recorder(e), "explorationId": exp} for e in s.events]}, method="POST")
    out = IngestResponse.model_validate_json(ingest.body)
    assert [o.root.status for o in out.outcomes] == ["accepted", "accepted"], ingest.body
    branch = out.outcomes[0].root.branchId

    raw = bytes(range(256)) * 40
    put = await call(jp_fetch, f"blobs/{exp}", raw, method="PUT", headers={"Content-Type": "application/octet-stream"})
    h = PutBlobResponse.model_validate_json(put.body).contentHash
    got = await call(jp_fetch, f"blobs/{exp}/{h}")
    assert got.body == raw and got.headers["Content-Type"] == "application/octet-stream"

    prep = await call(jp_fetch, "outputs/prepare", {"explorationId": exp, "outputs": [
        {"output_type": "stream", "name": "stdout", "text": "hi\n"}]}, method="POST")
    assert len(PrepareOutputsResponse.model_validate_json(prep.body).items) == 1

    rev = await call(jp_fetch, "cells/revision", {"explorationId": exp, "cellId": "c1", "code": "x = 2"}, method="POST")
    assert json.loads(rev.body)["status"] == "ok"
    state = await call(jp_fetch, "state", params={"explorationId": exp, "branchId": branch, "tick": 2})
    assert json.loads(state.body)["status"] == "ok"

    # the heartbeat now reads committed state through the probe
    beat = NotebookStatus.model_validate_json(
        (await call(jp_fetch, f"notebooks/{exp}/status", params={"instanceId": "tab-1"})).body)
    assert beat.cellRevisions == {"c1": 2} and beat.runsOpen == 0

    closed = await call(jp_fetch, "notebooks/close", {"notebookId": exp, "instanceId": "tab-1"}, method="POST")
    assert closed.code == 204


async def test_shutdown_closes_the_recorders_and_the_stores(jp_serverapp):
    host = jp_serverapp.web_app.settings[SETTINGS_KEY]
    assert not host._closed
    await jp_serverapp.cleanup_kernels()
    assert host._closed and host.scheduler._closed and host.sink._stopped
    with pytest.raises(sqlite3.ProgrammingError):  # the SQLite connection is closed
        host.service._events.explorations()
    host.close()  # idempotent

"""Rename and copy through Jupyter's real contents REST API."""

import asyncio
import json

from blaym_jupyter_types import OutlineRequest, StateAtRequest

from blaym_jupyter.adapters.jupyter import SETTINGS_KEY

from ...history.scenario import Scenario
from ...service.helpers import recorder
from .test_host import call, register


async def start_recording(jp_fetch, path):
    exp = (await register(jp_fetch, path=path, setRecording=True)).explorationId
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    await call(jp_fetch, "ingest", {
        "explorationId": exp, "editor": "jupyterlab", "instanceId": "tab-1",
        "events": [{**recorder(e), "explorationId": exp} for e in s.events]}, method="POST")
    return exp


async def settle(app):
    await app.event_logger.gather_listeners()
    await app.contents_manager.event_logger.gather_listeners()
    host = app.web_app.settings[SETTINGS_KEY]
    assert await asyncio.get_running_loop().run_in_executor(None, host.sink.drain)
    return host


def history(host, exp):
    """(highest tick, notebook path at the tip)."""
    cred = host.credential()
    outline = host.service.outline(cred, OutlineRequest(explorationId=exp))
    state = host.service.state_at(cred, StateAtRequest(
        explorationId=exp, branchId=outline.rootBranchId, tick=outline.highestTick)).state
    return outline.highestTick, state.model_dump(mode="json")


async def rename(jp_fetch, old, new):
    await jp_fetch("api", "contents", old, method="PATCH", body=json.dumps({"path": new}))


def make_notebook(root, path):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    return file


async def test_rename_keeps_one_history_even_while_the_page_has_the_old_path(jp_fetch, jp_serverapp, jp_root_dir):
    make_notebook(jp_root_dir, "a.ipynb")
    exp = await start_recording(jp_fetch, "a.ipynb")
    host = await settle(jp_serverapp)
    before, _ = history(host, exp)

    await rename(jp_fetch, "a.ipynb", "b.ipynb")
    await settle(jp_serverapp)
    tick, state = history(host, exp)
    assert tick == before + 1 and "b.ipynb" in json.dumps(state)
    assert host.registry.notebook_id_for_path("b.ipynb") == exp
    # a page (or its kernel session) that has not caught up, then one that has
    assert host.registry.notebook_id_for_path("a.ipynb") == exp
    for path in ("a.ipynb", "b.ipynb"):
        status = await register(jp_fetch, path=path)
        assert (status.explorationId, status.recording, status.notices) == (exp, True, [])
    assert host.registry.notebook_id_for_path("b.ipynb") == exp


async def test_a_copy_makes_a_second_history_and_leaves_the_original_alone(jp_fetch, jp_serverapp, jp_root_dir):
    make_notebook(jp_root_dir, "a.ipynb")
    exp = await start_recording(jp_fetch, "a.ipynb")
    host = await settle(jp_serverapp)
    before = history(host, exp)

    r = await jp_fetch("api", "contents", method="POST", body=json.dumps({"copy_from": "a.ipynb"}))
    copy = json.loads(r.body)["path"]
    await settle(jp_serverapp)
    copied_bytes = (jp_root_dir / copy).read_bytes()
    status = await register(jp_fetch, instance="tab-2", path=copy, setRecording=True)
    await settle(jp_serverapp)

    assert status.explorationId != exp and [n.code for n in status.notices] == ["copied"]
    assert history(host, exp) == before
    assert (jp_root_dir / copy).read_bytes() == copied_bytes
    assert host.registry.notebook_id_for_path("a.ipynb") == exp

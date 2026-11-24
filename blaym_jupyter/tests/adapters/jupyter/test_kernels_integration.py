"""The kernel adapter against a real server and a real ipykernel (marker `integration`).

Scripted runs go through the kernel websocket (both protocols); what is checked is read back from
the real SQLite store through the service. The long run (about three minutes) is scaled down unless
`BLAYM_LONG_RUN=full` (seconds of silence per stretch: 60 in full, 3 otherwise).
"""

from __future__ import annotations

import json
import os
import resource
import signal
import time

import pytest

from blaym_jupyter.adapters.jupyter.kernels import V1_PROTOCOL
from blaym_jupyter.tests.adapters.jupyter.kernel_harness import (
    PATH,
    Browser,
    KernelWs,
    run_for,
    settings_host,
    tip_state,
    wait_for,
)
from blaym_jupyter_types import EventsSinceRequest

pytestmark = pytest.mark.integration


@pytest.fixture
def jp_server_config(jp_server_config):
    jp_server_config["BlaymConfig"]["reply_grace_ms"] = 1500  # the websocket reply is the quick end; idle + 1.5 s otherwise
    return jp_server_config


class Rig:
    def __init__(self, jp_fetch, jp_ws_fetch, jp_serverapp):
        self.fetch, self.ws_fetch, self.app = jp_fetch, jp_ws_fetch, jp_serverapp
        self.host = settings_host(jp_serverapp)
        self.browser, self.kid, self.k, self.nb = Browser(jp_fetch), "", None, ""

    async def start(self, cells, protocol=V1_PROTOCOL, session=True):
        self.nb = await self.browser.open(cells)
        if session:
            r = await self.fetch("api", "sessions", method="POST", body=json.dumps(
                {"path": PATH, "name": PATH, "type": "notebook", "kernel": {"name": "python3"}}))
            self.kid = json.loads(r.body)["kernel"]["id"]
            await self.connect(protocol)
            await self.warm()  # a person cannot run a cell in the first 250 ms of a kernel; a script can
        return self

    async def connect(self, protocol=V1_PROTOCOL):
        ws = await self.ws_fetch("api", "kernels", self.kid, "channels", subprotocols=[protocol] if protocol else [])
        self.k = KernelWs(ws, protocol)

    async def run(self, code, cell_id, wait=True, timeout=60):
        self.k.execute(code, cell_id)
        if wait:
            await self.finished(cell_id, timeout)

    async def finished(self, cell_id, timeout=60, index=-1):
        await wait_for(lambda: (r := run_for(self.host, self.nb, cell_id, index)) and r.status.root != "running",
                       timeout)
        self.host.sink.drain()

    async def started(self, cell_id, timeout=30):
        return await wait_for(lambda: run_for(self.host, self.nb, cell_id), timeout)

    def items(self, cell_id):
        cell = next(c for c in tip_state(self.host, self.nb).notebook.cells if c.cellId == cell_id)
        return [i for group in cell.outputs for i in group.items]

    async def warm(self, timeout=60):
        """After a restart: wait until the adapter's new IOPub client is proven (a person cannot run
        a cell faster than the kernel comes back; a script can)."""
        from blaym_jupyter.adapters.jupyter import KERNELS_KEY
        adapter = self.app.web_app.settings[KERNELS_KEY]
        await wait_for(lambda: adapter.is_warm(self.kid), timeout)

    async def post(self, *parts):
        await self.fetch("api", "kernels", self.kid, *parts, method="POST", body="")


@pytest.fixture
def rig(jp_fetch, jp_ws_fetch, jp_serverapp):
    return Rig(jp_fetch, jp_ws_fetch, jp_serverapp)


CELLS = {
    "c1": "print('hello')",
    "c2": ("from IPython.display import display\n"
           "display({'text/html': '<table><tr><td>1</td></tr></table>', 'text/plain': '   a\\n0  1'}, raw=True)"),
    "c3": ("from IPython.display import display\n"
           "display({'application/vnd.plotly.v1+json': {'data': [{'y': list(range(300000))}], 'layout': {}},"
           " 'text/plain': 'Figure()'}, raw=True)"),
    "c4": "1/0",
    "c5": "from IPython.display import clear_output\nprint('a')\nclear_output(wait=True)\nprint('b')",
    "c6": "from IPython.display import display\nh = display('first', display_id=True)\nh.update('second')",
}


@pytest.mark.parametrize("protocol", [V1_PROTOCOL, None], ids=["v1", "json"])
async def test_every_kind_of_output_is_recorded_and_rebuilt(rig, protocol):
    await rig.start(list(CELLS.items()), protocol)
    for cell_id, code in CELLS.items():
        await rig.run(code, cell_id)
    state = tip_state(rig.host, rig.nb)
    assert state.kernel.kernelId == rig.kid and state.kernel.kernelName == "python3"
    assert state.kernel.language == "python"
    assert [i.preview for i in rig.items("c1")] == ["hello\n"]
    assert set(rig.items("c2")[0].mimeTypes) == {"text/html", "text/plain"}
    chart = rig.items("c3")[0]
    assert "application/vnd.plotly.v1+json" in chart.mimeTypes and chart.sizeBytes > 1_000_000
    assert [i.outputType.root if hasattr(i.outputType, "root") else i.outputType for i in rig.items("c4")] == ["error"]
    assert [i.preview for i in rig.items("c5")] == ["b\n"]  # clear_output(wait=True) dropped "a"
    assert [i.preview for i in rig.items("c6")] == ["'second'"]  # the display update replaced it in place
    statuses = {r.cellId: r.status.root for r in state.runs}
    assert statuses == {"c1": "ok", "c2": "ok", "c3": "ok", "c4": "error", "c5": "ok", "c6": "ok"}


async def test_an_interrupt_ends_the_run_as_interrupted_and_the_next_run_is_recorded(rig):
    await rig.start([("c1", "import time; time.sleep(60)"), ("c2", "print('after')")])
    await rig.run("import time; time.sleep(60)", "c1", wait=False)
    await rig.started("c1")
    await rig.post("interrupt")
    await rig.finished("c1")
    assert run_for(rig.host, rig.nb, "c1").status.root == "interrupted"
    await rig.run("print('after')", "c2")
    assert [i.preview for i in rig.items("c2")] == ["after\n"]


async def test_a_restart_leaves_the_old_run_unfinished_reattaches_and_records_the_next_run(rig):
    await rig.start([("c1", "import time; time.sleep(60)"), ("c2", "print('new process')")])
    await rig.run("import time; time.sleep(60)", "c1", wait=False)
    await rig.started("c1")
    await rig.post("restart")
    await wait_for(lambda: any(g.reason.value == "kernel_restart" for g in tip_state(rig.host, rig.nb).gaps)
                   if rig.host.sink.drain() is not None else False)
    await rig.warm()
    await rig.run("print('new process')", "c2")
    state = tip_state(rig.host, rig.nb)
    assert run_for(rig.host, rig.nb, "c1").status.root in ("unfinished", "interrupted")  # never "running"
    assert [i.preview for i in rig.items("c2")] == ["new process\n"]  # through the re-attached listener
    gaps = [g for g in state.gaps if g.reason.value == "kernel_restart"]
    assert len(gaps) == 1 and gaps[0].source.value == "kernel" and gaps[0].endedTick is not None


async def test_a_kernel_killed_with_sigkill_is_a_restart_and_recording_goes_on(rig):
    await rig.start([("c1", "import time; time.sleep(60)"), ("c2", "print('alive again')")])
    await rig.run("import time; time.sleep(60)", "c1", wait=False)
    await rig.started("c1")
    pid = rig.app.kernel_manager.get_kernel(rig.kid).provisioner.process.pid
    os.kill(pid, signal.SIGKILL)
    await wait_for(lambda: any(g.reason.value == "kernel_restart" for g in tip_state(rig.host, rig.nb).gaps)
                   if rig.host.sink.drain() is not None else False, 60)
    await rig.warm()
    await rig.run("print('alive again')", "c2", timeout=90)
    assert run_for(rig.host, rig.nb, "c1").status.root in ("unfinished", "interrupted")
    assert [i.preview for i in rig.items("c2")] == ["alive again\n"]


async def test_closing_the_websocket_mid_run_still_records_every_later_output(rig):
    code = "import time\nfor i in range(4):\n    print('line', i, flush=True)\n    time.sleep(0.6)"
    await rig.start([("c1", code)])
    await rig.run(code, "c1", wait=False)
    await wait_for(lambda: rig.host.sink.drain() is not None and (rig.items("c1") if run_for(rig.host, rig.nb, "c1") else None))
    rig.k.close()  # the browser goes away
    await rig.finished("c1", 30)
    text = "".join(i.preview for i in rig.items("c1"))
    assert [f"line {n}" in text for n in range(4)] == [True] * 4
    assert run_for(rig.host, rig.nb, "c1").status.root == "ok"  # idle + grace, with no reply to see


async def test_a_session_created_after_the_kernel_start_still_gets_kernel_started(rig):
    await rig.start([("c1", "print(1)")], session=False)
    r = await rig.fetch("api", "kernels", method="POST", body=json.dumps({"name": "python3"}))
    rig.kid = json.loads(r.body)["id"]  # the kernel starts with no session record
    await rig.fetch("api", "sessions", method="POST", body=json.dumps(
        {"path": PATH, "name": PATH, "type": "notebook", "kernel": {"id": rig.kid}}))
    await rig.connect(None)
    await rig.warm()
    await rig.run("print(1)", "c1")
    assert tip_state(rig.host, rig.nb).kernel.kernelId == rig.kid


async def test_a_run_without_a_cell_id_is_an_unlinked_run_gap(rig):
    await rig.start([("c1", "print(1)")])
    rig.k.execute("print(2)", None)
    await wait_for(lambda: rig.host.sink.drain() is not None and any(
        g.reason.value == "unknown" and g.source.value == "kernel" for g in tip_state(rig.host, rig.nb).gaps))
    assert tip_state(rig.host, rig.nb).runs == []


async def test_the_browser_edit_and_the_run_race_in_either_order(rig):
    await rig.start([("c1", "x = 1")])
    await rig.browser.edit("c1", "x = 2", 2)
    await rig.run("x = 2", "c1")
    assert run_for(rig.host, rig.nb, "c1").cellRevision == 2


async def test_a_long_run_with_silent_stretches_and_a_large_output_is_recorded_whole(rig, capsys):
    stretch = 60 if os.environ.get("BLAYM_LONG_RUN") == "full" else 3
    code = (f"import time\nfrom IPython.display import display\n"
            f"print('start', flush=True)\ntime.sleep({stretch})\nprint('middle', flush=True)\ntime.sleep({stretch})\n"
            "display({'application/vnd.plotly.v1+json': {'data': [{'y': list(range(400000))}], 'layout': {}},"
            " 'text/plain': 'big'}, raw=True)\n"
            f"time.sleep({stretch})\nprint('end')")
    await rig.start([("c1", code)])
    t0 = time.monotonic()
    rig.k.execute(code, "c1")
    await rig.started("c1", 60)
    first = time.monotonic() - t0
    await rig.finished("c1", stretch * 3 + 90)
    total = time.monotonic() - t0
    items = rig.items("c1")
    text = "".join(i.preview for i in items if "text/plain" in i.mimeTypes and i.outputType == "stream")
    assert "start" in text and "middle" in text and "end" in text
    assert any(i.sizeBytes > 1_000_000 for i in items)
    assert run_for(rig.host, rig.nb, "c1").status.root == "ok"
    state = tip_state(rig.host, rig.nb)
    events = rig.host.service.events_since(rig.host.credential(), EventsSinceRequest(
        explorationId=rig.nb, branchId=state.position.branchId, afterTick=0)).events
    kinds = [e.root.type.value if hasattr(e.root.type, "value") else str(e.root.type) for e in events]
    with capsys.disabled():
        print(f"\nLONG RUN stretch={stretch}s total={total:.1f}s first_recorded_event={first:.2f}s "
              f"ticks={state.position.lastEventTick} output_events={sum(k.startswith('output') for k in kinds)} "
              f"kinds={ {k: kinds.count(k) for k in set(kinds)} } "
              f"maxrss_mb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f}")

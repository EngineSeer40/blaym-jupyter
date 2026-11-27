"""Installing the kernel adapter wraps the configured connection class once; a restart drops the
old client's late messages."""

from __future__ import annotations

from types import SimpleNamespace

from blaym_jupyter.adapters.jupyter.kernels import install_kernel_adapter

from .test_kernels import Base, FakeRegistry, Rig, req

async def test_install_wraps_the_class_that_was_configured_and_never_replaces_it():
    class Mine(Base):
        pass

    listeners = []
    mkm = SimpleNamespace(list_kernel_ids=lambda: [], get_kernel=lambda k: None)
    app = SimpleNamespace(
        kernel_manager=mkm, session_manager=SimpleNamespace(), kernel_websocket_connection_class="ignored-trait",
        web_app=SimpleNamespace(settings={"kernel_websocket_connection_class": Mine}),
        event_logger=SimpleNamespace(add_listener=lambda listener: listeners.append(listener)))
    host = SimpleNamespace(run_recorder=object(), sink=object(), registry=FakeRegistry())
    adapter = install_kernel_adapter(app, host)
    wrapped = app.web_app.settings["kernel_websocket_connection_class"]
    assert issubclass(wrapped, Mine) and wrapped is not Mine and app.kernel_websocket_connection_class is wrapped
    assert listeners == [adapter.on_event]
    assert install_kernel_adapter(app, host) is adapter  # installing twice wraps and listens once
    assert app.web_app.settings["kernel_websocket_connection_class"] is wrapped and len(listeners) == 1
    await adapter.close()


async def test_a_message_of_the_old_client_already_queued_at_a_restart_is_dropped_by_generation():
    r = Rig(sessions={"k1": "a.ipynb"})
    r.adapter.kernel_started("k1", 1)
    r.adapter.execute_requested("k1", req("m1"))
    await r.settle()
    lane = r.adapter._lanes["k1"]
    old_gen = lane.gen
    r.adapter.kernel_restarted("k1")
    await r.settle()
    assert lane.gen > old_gen
    r.adapter._put(lane, ("iopub", old_gen, "m1", "stream", {"text": "late, from the old client"}))
    await r.settle()
    assert "kernel_message" not in r.rec.names()
    r.adapter._put(lane, ("iopub", lane.gen, "m1", "stream", {"text": "new client"}))
    await r.settle()
    assert r.rec.names().count("kernel_message") == 1
    await r.close()

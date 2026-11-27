"""`/stream` frames from the core iterator: one after an ingest, and a catch-up on connect."""

from __future__ import annotations

import json

from blaym_notebook.api.stream import AdvancedStream
from blaym_notebook.tests.api.test_api import TOKEN, Rig
from blaym_notebook.tests.history.scenario import Scenario


def _data(frame: bytes) -> dict:
    head, body = frame.decode().split("\n", 1)
    assert head == "event: advanced"
    return json.loads(body.removeprefix("data: "))


def test_stream_sends_a_frame_after_an_ingest_and_catches_up_when_behind(tmp_path):
    rig = Rig(tmp_path)
    exp, _ = rig.seed()  # two events stored: the tip is tick 2

    behind = AdvancedStream(rig.service, TOKEN, exp, after_tick=1, keepalive_s=0.05)
    current = AdvancedStream(rig.service, TOKEN, exp, after_tick=2, keepalive_s=0.05)
    try:
        frames = iter(behind)
        assert _data(next(frames))["tick"] == 2  # catch-up first
        assert next(frames) == b": keep-alive\n\n"  # nothing new yet

        live = iter(current)
        assert next(live) == b": keep-alive\n\n"  # not behind: no catch-up
        s = Scenario()  # the seed's own two events, then a third
        s.start(cells=[("c1", "needle = 1")])
        s.edit("c1", "needle = 2")
        s.edit("c1", "needle = 3")
        rig.call("POST", "/ingest", body=rig.ingest_body(exp, s.events[2:]))
        assert _data(next(live))["tick"] == 3
    finally:
        behind.close()
        current.close()

"""The one-recorder lease (S4, Task 7). The lease lives in memory only."""

from __future__ import annotations

from blaym_jupyter_types import ClaimRecorderRequest

from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, recorder


def claim(rig, editor, instance="tab-1", exp=None):
    r = rig.service.claim_recorder(TOKEN, ClaimRecorderRequest(
        explorationId=exp or rig.exp, editor=editor, instanceId=instance))
    return r.root


def send(rig, event, editor, instance="tab-1"):
    req = {"explorationId": rig.exp, "editor": editor, "instanceId": instance, "events": [recorder(event)]}
    return rig.service.ingest(TOKEN, req).outcomes[0].root


def events():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    s.edit("c1", "x = 3")
    s.edit("c1", "x = 4")
    return s.events


def test_tabs_of_one_editor_share_the_lease_and_another_editor_is_refused_with_the_holder():
    rig = Rig()
    assert claim(rig, "jupyterlab", "a").status == "granted"
    assert claim(rig, "jupyterlab", "b").status == "granted"
    refused = claim(rig, "vscode", "c")
    assert refused.status == "refused" and refused.holderEditor == "jupyterlab"
    assert "jupyterlab" in refused.detail


def test_takeover_only_after_the_timeout():
    rig = Rig(lease_timeout_ms=60_000)
    claim(rig, "jupyterlab")
    rig.clock.advance(59.9)
    assert claim(rig, "vscode").status == "refused"
    rig.clock.advance(0.2)  # 60.1 s of silence
    assert claim(rig, "vscode").status == "granted"
    assert claim(rig, "jupyterlab").status == "refused"  # the new holder is live


def test_a_busy_instance_keeps_the_lease_alive_for_its_editor():
    rig = Rig(lease_timeout_ms=60_000)
    claim(rig, "jupyterlab", "a")
    rig.clock.advance(50)
    claim(rig, "jupyterlab", "b")
    rig.clock.advance(50)  # "a" silent 100 s, "b" 50 s
    assert claim(rig, "vscode").status == "refused"


def test_ingest_claims_implicitly_refreshes_and_refuses_another_editor_for_every_event():
    rig = Rig(lease_timeout_ms=60_000)
    ev = events()
    assert send(rig, ev[0], "jupyterlab").status == "accepted"  # implicit claim
    assert claim(rig, "vscode").status == "refused"
    rig.clock.advance(50)
    assert send(rig, ev[1], "jupyterlab", "tab-2").status == "accepted"  # refresh
    rig.clock.advance(50)
    assert claim(rig, "vscode").status == "refused"  # alive thanks to the refresh at 50 s
    req = {"explorationId": rig.exp, "editor": "vscode", "instanceId": "v", "events": [recorder(e) for e in ev[2:]]}
    outs = [o.root for o in rig.service.ingest(TOKEN, req).outcomes]
    assert [o.reason for o in outs] == ["not_active_recorder"] * 2
    assert len(rig.stored()) == 2
    rig.clock.advance(61)
    assert rig.service.ingest(TOKEN, req).outcomes[0].root.status == "accepted"  # takeover by ingest


def test_leases_are_per_exploration():
    rig = Rig()
    claim(rig, "jupyterlab", exp="one")
    assert claim(rig, "vscode", exp="two").status == "granted"

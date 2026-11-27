"""The one-recorder lease. The lease lives in memory only."""

from __future__ import annotations

from blaym_notebook_types import ClaimRecorderRequest

from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import TOKEN, Rig, recorder


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







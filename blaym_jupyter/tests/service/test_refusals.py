from __future__ import annotations

import pytest

from blaym_jupyter_types import OutlineRequest, StateAtRequest, StoreLimits
from blaym_jupyter.ports.events import StoreBusy
from blaym_jupyter.service.auth import FixedTokenAuthorizer, Unauthorized
from blaym_jupyter.service.core import Service, ServiceBusy
from blaym_jupyter.testing.fakes import FakeClock
from blaym_jupyter.testing.memory import MemoryBlobStore, MemoryEventStore
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import Rig, recorder


def basic():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def test_first_event_not_a_snapshot():
    rig, s = Rig(), basic()
    s.insert("c2", "y")
    [o] = rig.outcomes(s.events[1:])
    assert o.reason == "bad_first_event" and rig.events.exploration(rig.exp) is None


def test_unauthorized_refuses_every_event_and_touches_nothing():
    rig, s = Rig(), basic()
    s.edit("c1", "x = 2")
    outs = rig.outcomes(s.events, credential="wrong")
    assert [o.reason for o in outs] == ["unauthorized"] * 2
    assert rig.events.exploration(rig.exp) is None
    with pytest.raises(Unauthorized):
        rig.service.state_at("wrong", StateAtRequest(explorationId=rig.exp, branchId="b", tick=1))
    with pytest.raises(Unauthorized):
        rig.service.outline(None, OutlineRequest(explorationId=rig.exp))
    with pytest.raises(Unauthorized):
        rig.service.put_blob("wrong", rig.exp, b"x")
    assert not rig.blobs._blobs


def test_unknown_target_branch():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    s.edit("c1", "x = 2")
    [o] = rig.outcomes(s.events[1:], target="nope")
    assert o.reason == "unknown_branch"
    [o] = rig.outcomes(s.events[1:], target=rig.root)
    assert o.status == "accepted"
    [o] = Rig().outcomes(s.events[:1], target="nope")
    assert o.reason == "unknown_branch"


def test_unsupported_schema_and_invalid_shape():
    rig, s = Rig(), basic()
    r = recorder(s.events[0])
    [a, b] = rig.outcomes([{**r, "schemaVersion": 1}, {**r, "type": "no_such_type"}])
    assert a.reason == "unsupported_schema_version"
    assert b.reason == "invalid_event" and b.detail and b.rebuild is None
    assert rig.events.exploration(rig.exp) is None


def test_exploration_mismatch_and_service_only_types():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    r = recorder(s.events[0])
    mismatch = {**r, "explorationId": "elsewhere"}
    bp = {**r, "type": "branch_point_created", "payload": {"name": "x", "parentBranchId": "main", "parentTick": 1}}
    cp = {**r, "type": "notebook_copied", "payload": {"oldPath": "a", "newPath": "b", "newExplorationId": "e2"}}
    outs = rig.outcomes([mismatch, bp, cp])
    assert [o.reason for o in outs] == ["invalid_event"] * 3
    assert "made by the service" in outs[1].detail and len(rig.stored()) == 1


def test_second_editor_cannot_start_a_history_another_editor_holds():
    # replaces a stale test that used the removed `lease_check` hook (Task 7 made the lease real)
    from blaym_jupyter_types import ClaimRecorderRequest
    rig = Rig()
    rig.service.claim_recorder("t0ken", ClaimRecorderRequest(explorationId=rig.exp, editor="vscode", instanceId="v"))
    [o] = rig.outcomes(basic().events)  # Rig sends as jupyterlab
    assert o.reason == "not_active_recorder"
    assert rig.events.exploration(rig.exp) is None


class _Busy(MemoryEventStore):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def append(self, *a, **k):
        if self.failures:
            self.failures -= 1
            raise StoreBusy("locked")
        return super().append(*a, **k)


def _busy_rig(failures):
    rig = Rig()
    rig.events = _Busy(failures)
    rig.service = Service(rig.events, rig.blobs, FixedTokenAuthorizer("t0ken"), rig.clock)
    return rig


def test_store_busy_is_retried_then_succeeds():
    rig = _busy_rig(2)
    [o] = rig.outcomes(basic().events)
    assert o.status == "accepted" and rig.clock.sleeps == [0.05, 0.1]


def test_store_busy_through_every_retry_raises_service_busy_with_earlier_outcomes():
    rig, s = _busy_rig(0), basic()
    s.edit("c1", "x = 2")
    rig.outcomes(s.events[:1])
    rig.events.failures = 99
    with pytest.raises(ServiceBusy) as exc:
        rig.send(s.events)  # first event is a duplicate (no write), second needs a write
    assert [o.root.duplicate if hasattr(o, "root") else o.duplicate for o in exc.value.outcomes] == [True]
    assert rig.clock.sleeps == [0.05, 0.1, 0.2]
    rig.events.failures = 0
    assert [o.status for o in rig.outcomes(s.events)] == ["accepted", "accepted"]  # tip state was dropped, not stale
    assert len(rig.stored()) == 2


@pytest.mark.parametrize("limits", [
    StoreLimits(truncatedPreviewBytes=2000, maxStoredOutputBytes=1000),
    StoreLimits(inlineOutputBytes=2000, maxStoredOutputBytes=1000),
    StoreLimits.model_construct(blobGzipLevel=0),
    StoreLimits.model_construct(blobGzipLevel=10),
])
def test_bad_limits_refuse_to_start(limits):
    with pytest.raises(ValueError):
        Service(MemoryEventStore(), MemoryBlobStore(), FixedTokenAuthorizer("t"), FakeClock(), limits=limits)


def test_good_limits_start():
    Service(MemoryEventStore(), MemoryBlobStore(), FixedTokenAuthorizer("t"), FakeClock(),
            limits=StoreLimits(maxStoredOutputBytes=1000, truncatedPreviewBytes=500, inlineOutputBytes=100))

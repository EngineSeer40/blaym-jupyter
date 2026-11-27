from __future__ import annotations

import pytest

from blaym_notebook_types import OutlineRequest, StateAtRequest
from blaym_notebook.ports.events import StoreBusy
from blaym_notebook.service.auth import FixedTokenAuthorizer, Unauthorized
from blaym_notebook.service.core import Service, ServiceBusy
from blaym_notebook.testing.memory import MemoryEventStore
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import Rig


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



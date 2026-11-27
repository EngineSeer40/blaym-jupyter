"""The cached tip state, gaps across branches, branch cuts,
threads, blobs across histories and the lease at its exact edges. In-memory ports."""

from __future__ import annotations

import threading
import time


from blaym_jupyter_types import RebuildOk

from blaym_jupyter.history import reference_rebuild
from blaym_jupyter.service.core import ServiceBusy
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import Rig, create, recorder


def base():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def check_tips(rig):
    """Every cached tip equals the reference rebuild at that branch's last event, and the
    service's own answer agrees at the last event of every branch."""
    exp = rig.service._exps[rig.exp]
    events = rig.stored()
    for b, last in exp.last_tick.items():
        ref = reference_rebuild(events, b, last)
        assert ref.status == "ok", (b, ref)
        got = rig.state(b, last)
        assert isinstance(got, RebuildOk) and got.state == ref.state
        if b in exp.tips:
            assert exp.tips[b] == ref.state, f"cached tip of {b} is wrong"


def gaps(rig, branch=None):
    return [e for e in rig.stored() if e.type == "recording_gap_started" and (branch is None or e.branchId == branch)]


def ended(rig):
    return [e for e in rig.stored() if e.type == "recording_gap_ended"]


# -- (1) the cached tip -------------------------------------------------------------


# -- (2) gaps -----------------------------------------------------------------------


# -- (3) create_branch ----------------------------------------------------------------


# -- (4) threads ---------------------------------------------------------------------

class _Slow:
    """Widen race windows: the store sleeps a little inside every append."""

    def __init__(self, store):
        self._store, self.append0 = store, store.append

    def install(self):
        def append(*a, **k):
            time.sleep(0.0005)
            return self.append0(*a, **k)
        self._store.append = append


def test_two_threads_ingesting_into_one_history_keep_ticks_dense():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    _Slow(rig.events).install()
    evs = [recorder(s.notebook_metadata({"k": i})) for i in range(60)]
    errors, outs = [], []

    def work(chunk):
        try:
            for ev in chunk:
                outs.append(rig.outcomes([ev])[0])
        except ServiceBusy:
            outs.append("busy")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=work, args=(evs[i::2],)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert all(o == "busy" or o.status == "accepted" for o in outs)
    ticks = [e.tick for e in rig.stored()]
    assert ticks == list(range(1, len(ticks) + 1)) and len({str(e.eventId) for e in rig.stored()}) == len(ticks)
    assert len(ticks) == 1 + sum(1 for o in outs if o != "busy")
    check_tips(rig)


def test_threads_creating_branches_while_another_ingests():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    root = rig.root
    _Slow(rig.events).install()
    evs = [recorder(s.notebook_metadata({"k": i})) for i in range(30)]
    errors, made = [], []

    def ingest():
        try:
            for ev in evs:
                rig.outcomes([ev], target=root)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def branch(n):
        try:
            for _ in range(8):
                made.append(create(rig, root, 1, f"b{n}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=ingest)] + [threading.Thread(target=branch, args=(n,)) for n in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert all(m.status == "ok" for m in made) and len({m.branch.branchId for m in made}) == 16
    ticks = [e.tick for e in rig.stored()]
    assert ticks == list(range(1, len(ticks) + 1)) == list(range(1, 1 + 1 + 30 + 16))
    check_tips(rig)


# -- (5) blobs ------------------------------------------------------------------------


# -- (7) the lease at its edges ---------------------------------------------------------



"""Review of S4 Tasks 6 and 7: the cached tip state, gaps across branches, branch cuts,
threads, blobs across histories and the lease at its exact edges. In-memory ports."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from blaym_jupyter_types import ClaimRecorderRequest, RebuildOk

from blaym_jupyter.history import reference_rebuild
from blaym_jupyter.ports.events import DuplicateEventId, StoreBusy
from blaym_jupyter.service.core import ServiceBusy
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create, recorder


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

def test_refused_event_then_next_valid_event_is_accepted_and_tip_is_right():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    ok1 = recorder(s.edit("c1", "a", revision=2))
    bad = recorder(s.edit("c1", "b", revision=9))
    ok2 = recorder(s.edit("c1", "c", revision=3))
    assert rig.outcomes([ok1])[0].status == "accepted"
    assert rig.outcomes([bad])[0].reason == "invalid_event"
    check_tips(rig)
    assert rig.outcomes([ok2])[0].status == "accepted"
    check_tips(rig)
    state = rig.state(rig.root, rig.stored()[-1].tick).state
    assert [c.code for c in state.notebook.cells] == ["c"]


def test_refused_half_applied_snapshot_batch_leaves_a_correct_tip():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    bad = recorder(s.edit("c1", "b", revision=9))
    rig.outcomes([bad])  # opens a gap
    broken = recorder(s.snapshot([("c1", "x")]))
    broken["payload"]["cells"].append(dict(broken["payload"]["cells"][0]))  # duplicate cell id
    o = rig.outcomes([broken])[0]
    if o.status == "refused":  # gap_ended was stepped in place, then the snapshot failed
        check_tips(rig)
        assert not ended(rig)
        assert rig.outcomes([recorder(s.snapshot([("c1", "y")]))])[0].status == "accepted"
    check_tips(rig)


@pytest.mark.parametrize("failure", ["busy", "duplicate"])
def test_failed_commit_leaves_cache_unstepped(failure):
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    ev1 = recorder(s.edit("c1", "a", revision=2))
    ev2 = recorder(s.edit("c1", "b", revision=3))
    real = rig.events.append
    calls = {"n": 0}

    def flaky(exploration_id, events, **kw):
        calls["n"] += 1
        if failure == "busy":
            raise StoreBusy("locked")
        if calls["n"] == 1:
            raise DuplicateEventId("x")
        return real(exploration_id, events, **kw)

    rig.events.append = flaky
    if failure == "busy":
        with pytest.raises(ServiceBusy):
            rig.send([ev1])
    else:
        assert rig.outcomes([ev1])[0].reason == "conflict"
    rig.events.append = real
    check_tips(rig)
    assert len(rig.stored()) == 1
    assert [o.status for o in rig.outcomes([ev1, ev2])] == ["accepted", "accepted"]
    check_tips(rig)
    assert [c.code for c in rig.state(rig.root, 3).state.notebook.cells] == ["b"]


def test_failed_commit_of_a_snapshot_with_closing_gap_events_keeps_gap_open_and_cache_right():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    rig.outcomes([recorder(s.edit("c1", "b", revision=9))])
    snap = recorder(s.snapshot([("c1", "y")]))
    real = rig.events.append
    rig.events.append = lambda *a, **k: (_ for _ in ()).throw(StoreBusy("locked"))
    with pytest.raises(ServiceBusy):
        rig.send([snap])
    rig.events.append = real
    check_tips(rig)
    assert rig.outcomes([snap])[0].status == "accepted"
    assert len(ended(rig)) == 1
    check_tips(rig)


def test_ingest_to_branch_b_does_not_change_the_cached_tip_of_branch_a_and_target_beats_active():
    rig, s = Rig(), base()
    rig.outcomes(s.events + [s.edit("c1", "x = 2")])
    root = rig.root
    out = create(rig, root, 2)
    b = out.branch.branchId  # active now
    rig.outcomes([recorder(s.edit("c1", "A-late", revision=3))], target=root)  # non-active target
    check_tips(rig)
    rig.outcomes([recorder(s.edit("c1", "B-1", revision=3))], target=b)
    rig.outcomes([recorder(s.edit("c1", "A-2", revision=4))], target=root)
    rig.outcomes([recorder(s.edit("c1", "B-2", revision=4))])  # default: active = b
    check_tips(rig)
    exp = rig.service._exps[rig.exp]
    assert exp.tips[root] != exp.tips[b]
    last_a = exp.last_tick[root]
    assert [c.code for c in rig.state(root, last_a).state.notebook.cells] == ["A-2"]
    assert [c.code for c in rig.state(b, exp.last_tick[b]).state.notebook.cells] == ["B-2"]


# -- (2) gaps -----------------------------------------------------------------------

def test_gap_opened_once_per_branch_and_snapshot_on_another_branch_does_not_close_it():
    rig, s = Rig(), base()
    rig.outcomes(s.events + [s.edit("c1", "x = 2")])
    root = rig.root
    b = create(rig, root, 2).branch.branchId
    rig.outcomes([recorder(s.edit("c1", "boom", revision=9))], target=root)
    rig.outcomes([recorder(s.edit("c1", "boom2", revision=9))], target=root)
    assert len(gaps(rig, root)) == 1
    o = rig.outcomes([recorder(s.snapshot([("c1", "z")]))], target=b)[0]
    assert o.status == "accepted" and not ended(rig)
    check_tips(rig)
    o = rig.outcomes([recorder(s.snapshot([("c1", "w")]))], target=root)[0]
    assert o.status == "accepted" and len(ended(rig)) == 1 and ended(rig)[0].branchId == root
    assert rig.stored()[-2].type == "recording_gap_ended" and rig.stored()[-1].type == "notebook_snapshot"
    check_tips(rig)


def test_branch_created_while_a_service_gap_is_open_is_consistent():
    rig, s = Rig(), base()
    rig.outcomes(s.events + [s.edit("c1", "x = 2")])
    root = rig.root
    before = create(rig, root, 2).branch.branchId  # cut before the gap
    rig.outcomes([recorder(s.edit("c1", "boom", revision=9))], target=root)
    gap_tick = gaps(rig, root)[0].tick
    # a branch cut AT the gap event sees the open gap; the parent's gap stays open too
    child = create(rig, root, gap_tick).branch.branchId
    cst = rig.state(child, rig.service._exps[rig.exp].last_tick[child]).state
    assert [g.endedTick for g in cst.gaps if g.source == "service"] == [None]
    other = rig.state(before, rig.service._exps[rig.exp].last_tick[before]).state
    assert not [g for g in other.gaps if g.source == "service"]
    check_tips(rig)
    # a snapshot on the child closes the child's copy of the gap, in its own commit
    assert rig.outcomes([recorder(s.snapshot([("c1", "k")]))], target=child)[0].status == "accepted"
    assert [e.branchId for e in ended(rig)] == [child]
    check_tips(rig)
    # the parent's own gap is still open and closes on its own snapshot
    assert rig.outcomes([recorder(s.snapshot([("c1", "m")]))], target=root)[0].status == "accepted"
    assert [e.branchId for e in ended(rig)] == [child, root]
    check_tips(rig)


# -- (3) create_branch ----------------------------------------------------------------

def test_branch_cut_is_inclusive_and_new_branch_tip_matches_reference():
    rig, s = Rig(), base()
    rig.outcomes(s.events + [s.edit("c1", "two"), s.edit("c1", "three"), s.edit("c1", "four")])
    root = rig.root
    out = create(rig, root, 3)
    b = out.branch
    assert b.parentTick == 3
    bp = rig.stored()[-1]
    assert bp.payload.parentTick == 3 and bp.type == "branch_point_created"
    st = rig.state(b.branchId, bp.tick).state
    assert [c.code for c in st.notebook.cells] == ["three"]  # tick 3 in, tick 4 out
    assert rig.events.branches(rig.exp)[-1].parent_tick == 3
    assert rig.outcomes([recorder(s.edit("c1", "B", revision=4))])[0].status == "accepted"
    check_tips(rig)
    assert [c.code for c in rig.state(b.branchId, rig.stored()[-1].tick).state.notebook.cells] == ["B"]
    assert [c.code for c in rig.state(root, 4).state.notebook.cells] == ["four"]


def test_branch_from_sibling_tick_is_refused_and_shared_parent_part_is_allowed():
    rig, s = Rig(), base()
    rig.outcomes(s.events + [s.edit("c1", "two"), s.edit("c1", "three")])
    root = rig.root
    b1 = create(rig, root, 2).branch.branchId
    rig.outcomes([recorder(s.edit("c1", "B1", revision=3))], target=b1)
    sib_tick = rig.stored()[-1].tick
    b2 = create(rig, root, 2).branch.branchId
    r = rig.service.create_branch(TOKEN, __import__("blaym_jupyter_types").CreateBranchRequest(
        explorationId=rig.exp, fromBranchId=b2, fromTick=sib_tick, name="x")).root
    assert r.status == "refused" and r.reason == "not_on_line"
    r = create(rig, root, 3)  # tick 3 is the parent's own, after b1 and b2 were cut at 2
    assert r.status == "ok"
    grand = create(rig, b1, 2)  # tick 2 is on b1's line through its parent
    assert grand.status == "ok" and grand.branch.parentBranchId == b1 and grand.branch.parentTick == 2
    check_tips(rig)


def test_moment_inside_run_is_unsettled_even_when_a_later_restart_closed_it():
    rig, s = Rig(), base()
    run = s.run("c1")
    s.kernel_restart()
    s.edit("c1", "after")
    rig.outcomes(s.events)
    root = rig.root
    assert create(rig, root, 2).status == "refused"  # run_started: running at that moment
    r = create(rig, root, 3)
    assert r.status == "ok"  # settled by the restart
    assert create(rig, root, 3).status == "ok"  # twice from one moment: two branches
    ids = {b.branch_id for b in rig.events.branches(rig.exp)}
    assert len(ids) == 3
    check_tips(rig)


def test_two_branches_from_one_moment_are_independent():
    rig, s = Rig(), base()
    rig.outcomes(s.events + [s.edit("c1", "two")])
    root = rig.root
    a = create(rig, root, 2).branch.branchId
    b = create(rig, root, 2).branch.branchId
    rig.outcomes([recorder(s.edit("c1", "A", revision=3))], target=a)
    rig.outcomes([recorder(s.edit("c1", "B", revision=3))], target=b)
    check_tips(rig)
    exp = rig.service._exps[rig.exp]
    assert [c.code for c in rig.state(a, exp.last_tick[a]).state.notebook.cells] == ["A"]
    assert [c.code for c in rig.state(b, exp.last_tick[b]).state.notebook.cells] == ["B"]


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

def test_secret_in_a_blob_put_through_the_service_is_not_in_the_stored_bytes():
    rig = Rig()
    secret = "ghp_aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"
    r = rig.service.put_blob(TOKEN, rig.exp, f"token = {secret}\n".encode() * 3)
    data = rig.blobs.read(rig.exp, r.contentHash)
    assert secret.encode() not in data and b"hidden" in data


def test_blob_that_exists_in_another_history_is_missing_here():
    rig, s = Rig(), base()
    run = s.run("c1")
    rig.outcomes(s.events[:1])
    put = rig.service.put_blob(TOKEN, "other-exploration", b"a big chart " * 10)
    ev = recorder(s.output(run, "ignored"))
    ev["payload"]["item"].update(truncated=True, contentHash=put.contentHash, preview="short", sizeBytes=999)
    ev2 = recorder(s.events[1])
    rig.outcomes([ev2])
    o = rig.outcomes([ev])[0]
    assert o.status == "refused" and o.reason == "missing_blob"


# -- (7) the lease at its edges ---------------------------------------------------------

def test_lease_is_live_at_exactly_the_timeout_and_free_one_millisecond_later():
    rig = Rig(lease_timeout_ms=60_000)
    claim = lambda ed: rig.service.claim_recorder(TOKEN, ClaimRecorderRequest(  # noqa: E731
        explorationId=rig.exp, editor=ed, instanceId="t")).root.status
    assert claim("jupyterlab") == "granted"
    rig.clock.advance(60.0)
    assert claim("vscode") == "refused"
    rig.clock.advance(0.001)
    assert claim("vscode") == "granted"


def test_a_refused_ingest_from_the_second_editor_writes_and_announces_nothing_and_keeps_the_lease():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    seen = []
    rig.service.subscribe(rig.exp, seen.append)
    before = (len(rig.stored()), rig.events.tip_tick(rig.exp))
    ev = recorder(s.edit("c1", "other", revision=2))
    rig.clock.advance(30)
    out = rig.service.ingest(TOKEN, {"explorationId": rig.exp, "editor": "vscode", "instanceId": "v",
                                     "events": [ev, recorder(s.edit("c1", "o2", revision=3))]})
    assert [o.root.reason for o in out.outcomes] == ["not_active_recorder"] * 2
    assert (len(rig.stored()), rig.events.tip_tick(rig.exp)) == before and not seen
    rig.clock.advance(31)  # 61 s since the holder was last heard: the refused ingest did not refresh it
    assert rig.service.claim_recorder(TOKEN, ClaimRecorderRequest(
        explorationId=rig.exp, editor="vscode", instanceId="v")).root.status == "granted"


def test_two_threads_starting_the_same_history_make_one_root():
    rig = Rig()
    _Slow(rig.events).install()
    s1, s2 = base(), base()
    barrier = threading.Barrier(2)
    outs, errors = [], []

    def go(s):
        try:
            barrier.wait()
            outs.extend(rig.outcomes([{**recorder(e), "eventId": str(uuid.uuid4())} for e in s.events]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(s,)) for s in (s1, s2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert len(rig.events.branches(rig.exp)) == 1, "two roots were made"
    ticks = [e.tick for e in rig.stored()]
    assert ticks == list(range(1, len(ticks) + 1))
    check_tips(rig)


def test_two_threads_hydrating_a_cold_service_do_not_lose_ticks():
    rig, s = Rig(), base()
    rig.outcomes(s.events)
    from blaym_jupyter.tests.service.test_branches import fresh_service
    cold = fresh_service(rig)
    _Slow(rig.events).install()
    evs = [recorder(s.notebook_metadata({"k": i})) for i in range(20)]
    barrier = threading.Barrier(2)
    errors = []

    def go(chunk):
        try:
            barrier.wait()
            for ev in chunk:
                cold.outcomes([ev])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(evs[i::2],)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    ticks = [e.tick for e in rig.stored()]
    assert ticks == list(range(1, 22))
    assert cold.service._exps[rig.exp].tip_tick == 21


def test_two_threads_editing_different_cells_lose_no_edit():
    s = Scenario()
    s.start(cells=[("c1", "a"), ("c2", "b")])
    rig = Rig()
    rig.outcomes(s.events)
    _Slow(rig.events).install()
    mine = {c: [recorder(s.edit(c, f"{c}-{i}")) for i in range(25)] for c in ("c1", "c2")}
    barrier = threading.Barrier(2)
    errors, outs = [], []

    def go(c):
        try:
            barrier.wait()
            for ev in mine[c]:
                outs.append(rig.outcomes([ev])[0])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=go, args=(c,)) for c in mine]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    assert [o.status for o in outs] == ["accepted"] * 50
    ticks = [e.tick for e in rig.stored()]
    assert ticks == list(range(1, 52))
    check_tips(rig)
    tip = rig.state(rig.root, 51).state
    assert {c.cellId: c.code for c in tip.notebook.cells} == {"c1": "c1-24", "c2": "c2-24"}

"""Ingest through the service on in-memory ports (S4, Task 6)."""

from __future__ import annotations

import json
import random
import uuid

from hypothesis import given, settings, strategies as st

from blaym_jupyter_types import GapSource, RebuildInvalidRevision, RebuildOk
from blaym_jupyter.history import reference_rebuild
from blaym_jupyter.ports.events import canonical_event_json
from blaym_jupyter.privacy.redact import redact_event
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.history.scenario import EXPLORATION, Scenario, output_item
from blaym_jupyter.tests.service.helpers import Rig, recorder

SECRET = "ghp_aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"


def basic():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    return s


def test_first_snapshot_makes_root_branch_active_and_tick_1():
    rig, s = Rig(), basic()
    [o] = rig.outcomes(s.events)
    assert o.status == "accepted" and o.tick == 1 and not o.duplicate
    assert o.branchId == rig.root
    assert [b.branch_id for b in rig.events.branches(rig.exp)] == [rig.root]
    assert rig.stored()[0].branchId == rig.root


def test_identical_resend_is_duplicate_with_existing_record_and_no_write_or_notice():
    rig, s = Rig(), basic()
    s.edit("c1", "x = 2")
    seen = []
    rig.outcomes(s.events[:1])
    rig.service.subscribe(rig.exp, seen.append)
    first = rig.outcomes(s.events)
    again = rig.outcomes(s.events)
    assert [o.duplicate for o in first] == [True, False]
    assert [(o.tick, o.duplicate) for o in again] == [(1, True), (2, True)]
    assert len(rig.stored()) == 2 and [a.tick for a in seen] == [2]


def test_resend_with_null_optionals_is_a_duplicate():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    r = recorder(s.events[0])
    r.update(links=None, editor=None, authorHint=None)
    [o] = rig.outcomes([r])
    assert o.status == "accepted" and o.duplicate


def test_same_id_different_content_is_conflict_and_first_stays():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    r = recorder(s.events[0])
    r["payload"]["path"] = "other.ipynb"
    [o] = rig.outcomes([r])
    assert o.status == "refused" and o.reason == "conflict"
    assert rig.stored()[0].payload.path == "analysis.ipynb"


def test_same_id_into_another_exploration_is_conflict():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    r = recorder(s.events[0])
    r["explorationId"] = "other-exploration"
    [o] = rig.outcomes([r], exp="other-exploration")
    assert o.status == "refused" and o.reason == "conflict"
    assert rig.events.exploration("other-exploration") is None


def test_redaction_happens_before_comparison_and_storage():
    rig, s = Rig(), basic()
    run = s.run("c1")
    s.output(run, f"token {SECRET}")
    s.edit("c1", f'key = "{SECRET}"')
    assert [o.status for o in rig.outcomes(s.events)] == ["accepted"] * 4
    again = rig.outcomes(s.events)
    assert all(o.duplicate for o in again)
    text = " ".join(canonical_event_json(e) for e in rig.stored())
    assert SECRET not in text and "hidden" in text
    assert all(SECRET.encode() not in b for b in rig.blobs._blobs.values())


def test_inline_item_gets_hash_and_size_of_redacted_content():
    rig, s = Rig(), basic()
    run = s.run("c1")
    s.output(run, f"token {SECRET}")
    rig.outcomes(s.events)
    item = rig.stored()[-1].payload.item
    from blaym_jupyter.ports.blobs import content_hash_of
    assert item.contentHash == content_hash_of(item.preview.encode())
    assert item.sizeBytes == len(item.preview.encode()) and SECRET not in item.preview


def _blob_output(s, run, h):
    r = recorder(s.output(run, "ignored"))
    r["payload"]["item"].update(truncated=True, contentHash=h, preview="short", sizeBytes=999)
    return r


def test_blob_item_refused_when_blob_missing_and_accepted_when_put():
    rig, s = Rig(), basic()
    run = s.run("c1")
    rig.outcomes(s.events)
    missing = _blob_output(s, run, "sha256:" + "0" * 64)
    [o] = rig.outcomes([missing])
    assert o.status == "refused" and o.reason == "missing_blob"
    put = rig.service.put_blob("t0ken", rig.exp, b"a big chart " * 10)
    ok = _blob_output(s, run, put.contentHash)
    ok["eventId"] = str(uuid.uuid4())
    [o] = rig.outcomes([ok])
    assert o.status == "accepted"
    assert rig.stored()[-1].payload.item.contentHash == put.contentHash


def _bad_revision(s):
    return recorder(s.edit("c1", "boom", revision=7))


def test_invalid_event_refused_with_typed_result_gap_once_and_snapshot_closes_it():
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    s.edit("c1", "x = 2")
    s.edit("c1", "x = 3", revision=9)  # revision jump
    bad = recorder(s.events[-1])
    [o] = rig.outcomes([bad])
    assert o.status == "refused" and o.reason == "invalid_event"
    assert isinstance(o.rebuild.root if hasattr(o.rebuild, "root") else o.rebuild, RebuildInvalidRevision)
    stored = rig.stored()
    assert [e.type for e in stored] == ["notebook_snapshot", "recording_gap_started"]
    gap = stored[-1]
    assert gap.payload.source == "service" and gap.payload.reason == "unknown"
    assert gap.originId == "blaym-service" and gap.authorHint == "blaym" and gap.authorConfidence == "known"
    unknown_run = {**recorder(s.events[0]), "eventId": str(uuid.uuid4()), "type": "run_finished",
                   "payload": {"runId": "nope", "status": "ok"}}
    [o2] = rig.outcomes([unknown_run])
    assert o2.reason == "invalid_event" and len(rig.stored()) == 2  # no second gap
    snap = recorder(s.snapshot([("c1", "x = 2")]))
    [o3] = rig.outcomes([snap])
    types = [e.type for e in rig.stored()]
    assert types[-2:] == ["recording_gap_ended", "notebook_snapshot"] and o3.tick == 4
    assert rig.stored()[-2].payload.gapId == gap.payload.gapId
    assert isinstance(rig.state(rig.root, 4), RebuildOk)


def test_edit_during_a_run_is_accepted():
    rig, s = Rig(), basic()
    run = s.run("c1")
    s.edit("c1", "x = 2")
    s.finish(run)
    assert [o.status for o in rig.outcomes(s.events)] == ["accepted"] * 4


def test_kernel_restart_closes_running_runs_and_notice_lists_them():
    rig, s = Rig(), basic()
    r1 = s.run("c1")
    r2 = s.run("c1")
    s.finish(r1)
    s.kernel_restart()
    seen = []
    rig.outcomes(s.events[:1])
    rig.service.subscribe(rig.exp, seen.append)
    rig.outcomes(s.events)
    by_tick = {a.tick: a for a in seen}
    assert by_tick[2].runStarted == r1 and by_tick[3].runStarted == r2
    assert by_tick[4].runsEnded == [r1] and by_tick[5].runsEnded == [r2]
    assert by_tick[5].runStarted is None


def test_changed_outside_editor_is_accepted():
    rig, s = Rig(), basic()
    s.outside_change([("c1", "y = 1")])
    assert [o.status for o in rig.outcomes(s.events)] == ["accepted"] * 2
    assert rig.stored()[-1].authorHint == "outside"


def test_outline_is_cached_until_something_new_is_stored():
    from blaym_jupyter_types import OutlineRequest
    rig, s = Rig(), basic()
    rig.outcomes(s.events)
    req = OutlineRequest(explorationId=rig.exp)
    a = rig.service.outline("t0ken", req)
    assert rig.service.outline("t0ken", req) is a
    s.edit("c1", "x = 2")
    rig.outcomes(s.events)
    assert rig.service.outline("t0ken", req) is not a
    assert rig.service.outline("t0ken", OutlineRequest(explorationId="nope")) is None


def test_unknown_exploration_state_is_missing_events_from_tick_1():
    rig = Rig()
    r = rig.state("main", 1)
    assert r.status == "missing_events" and r.firstMissingTick == 1


# -- generated sequences ----------------------------------------------------------


def _strip_hash(d):
    if isinstance(d, dict):
        return {k: _strip_hash(v) for k, v in d.items() if k != "contentHash"}
    if isinstance(d, list):
        return [_strip_hash(v) for v in d]
    return d


def _key(ev):
    d = json.loads(canonical_event_json(ev))
    d.pop("tick", None)
    d.pop("branchId", None)
    return _strip_hash(d)


def _browser_gap(e):
    """The generator writes gaps with source 'service'; a recorder never does, and the service
    closes open service gaps itself at the next snapshot (that rule has named tests), so the
    generated history is sent with the gap source of a recorder."""
    if e.type == "recording_gap_started" and e.payload.source == "service":
        return e.model_copy(update={"payload": e.payload.model_copy(update={"source": GapSource.browser})})
    return e


@settings(max_examples=150)
@given(h=histories(shapes=("none",), max_events=40, service_safe=True), seed=st.integers(0, 10**6), invalid=st.booleans())
def test_generated_single_branch_histories_through_the_service(h, seed, invalid):
    rnd = random.Random(seed)
    rig = Rig(interval=5)
    events = [_browser_gap(e) for e in h.events]
    kept = [e for e in events if e.type != "notebook_copied"]
    expected = [redact_event(e.model_copy(update={"tick": i + 1})) for i, e in enumerate(kept)]
    sent = events
    i = 0
    while i < len(sent):
        batch = sent[i:i + rnd.randint(1, 6)]
        i += len(batch)
        resend = rnd.sample(sent[:i], min(2, i)) if rnd.random() < 0.5 else []
        outs = rig.outcomes(batch + resend)
        for ev, o in zip(batch, outs):
            if ev.type == "notebook_copied":
                assert o.status == "refused" and o.reason == "invalid_event"
            else:
                assert o.status == "accepted" and not o.duplicate
        for ev, o in zip(resend, outs[len(batch):]):
            assert o.status == "refused" if ev.type == "notebook_copied" else o.duplicate
        stored = rig.stored()
        n = len(stored)
        assert [e.tick for e in stored] == list(range(1, n + 1))
        assert [_key(e) for e in stored] == [_key(e) for e in expected[:n]]
        root = rig.root
        assert isinstance(rig.state(root, n), RebuildOk)
        t = rnd.randint(1, n)
        assert rig.state(root, t).model_dump() == reference_rebuild(stored, root, t).model_dump()
        tip = rig.service._exps[rig.exp].tips[root]
        assert tip.model_dump() == rig.state(root, n).state.model_dump()
    assert len(rig.stored()) == len(kept)
    for t in range(1, len(kept) + 1):
        assert isinstance(rig.state(rig.root, t), RebuildOk)
    if invalid:
        before = len(rig.stored())
        bad = {**recorder(kept[0]), "eventId": str(uuid.uuid4()), "type": "run_finished",
               "payload": {"runId": "nope", "status": "ok"}}
        assert rig.outcomes([bad])[0].reason == "invalid_event"
        assert len(rig.stored()) == before + 1 and rig.stored()[-1].type == "recording_gap_started"
        for t in (1, before // 2 or 1, before):
            assert rig.state(rig.root, t).model_dump() == reference_rebuild(rig.stored(), rig.root, t).model_dump()

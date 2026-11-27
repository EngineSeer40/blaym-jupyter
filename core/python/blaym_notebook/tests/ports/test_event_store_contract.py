"""The event store contract, run against the in-memory and the SQLite store. A store is
accepted by passing it. The model-based sequence test checks a Python list as the reference
after every step."""

from __future__ import annotations

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from blaym_notebook.ports.events import BranchRecord, DuplicateEventId, InvalidEvent, canonical_event_json
from blaym_notebook.tests.history.conftest import DEEP

from .harness import EXP, KINDS, Harness, dump, make_event


def test_ticks_records_exact_round_trip_and_author_fields(harness):
    s = harness.store
    assert (s.events_for("nope"), s.tip_tick("nope"), s.exploration("nope"), s.branches("nope")) == ([], 0, None, [])
    assert s.append(EXP, []) == [] and s.exploration(EXP) is None
    # ticks start at one, follow in order, and are per exploration
    assert [e.tick for e in s.append(EXP, [make_event(1, userId="u1", workspaceId="w1"), make_event(2)])] == [1, 2]
    assert [e.tick for e in s.append("b", [make_event(3, exp="b")])] == [1]
    assert [e.tick for e in s.append(EXP, [make_event(4)])] == [3]
    assert s.tip_tick(EXP) == 3
    rec = s.exploration(EXP)
    assert (rec.user_id, rec.workspace_id, rec.active_branch_id) == ("u1", "w1", None)
    assert {r.exploration_id for r in s.explorations()} == {EXP, "b"}
    # events come back as written, with the author fields as strings, enum values or the literal "outside"
    sent = make_event(
        5, exp="c", branch="b-7", authorHint="ai", authorConfidence="guess", editor="jupyterlab",
        payload={"cellId": "c9", "code": "print('h\u00e9llo \u2603')\n", "revision": 4},
    )
    s.append("c", [sent, make_event(6, exp="c", authorHint="blaym"), make_event(7, exp="c", authorHint="outside")])
    back = s.find_event("c", sent["eventId"])
    assert back.tick == 1 and back.branchId == "b-7" and back.payload.code == sent["payload"]["code"]
    assert back.authorHint.value == "ai" and str(back.eventId) == sent["eventId"] and back == s.events_for("c")[0]
    assert [str(getattr(e.authorHint, "value", e.authorHint)) for e in s.events_for("c")] == ["ai", "blaym", "outside"]
    # a planted hole is returned as a hole and the next tick is max + 1
    harness_events = [make_event(n, exp="h") | {"tick": n} for n in (1, 2, 4)]
    from blaym_notebook_types import BlaymEvent

    harness.load_exact("h", [BlaymEvent.model_validate(e) for e in reversed(harness_events)])
    assert [e.tick for e in s.events_for("h")] == [1, 2, 4]
    assert s.append("h", [make_event(9, exp="h")])[0].tick == 5


def test_refused_batches_write_nothing_and_leave_the_next_tick_unchanged(harness):
    s = harness.store
    s.append(EXP, [make_event(1)])
    bad_events = [
        {"type": "no_such_event"},
        {"payload": {"cellId": "c1", "code": "x", "revision": 0}},
        {"payload": {"cellId": "c1"}},
        {"surprise": 1},
        {"explorationId": "someone-else"},
        {"schemaVersion": 1},
    ]
    for bad in bad_events:
        with pytest.raises(InvalidEvent):
            s.append(EXP, [make_event(2), make_event(3, **bad)])
    for batch in ([make_event(2), make_event(3), make_event(1)], [make_event(4), make_event(4)]):
        with pytest.raises(DuplicateEventId):
            s.append(EXP, batch)
    assert [e.tick for e in s.events_for(EXP)] == [1]
    assert s.find_event(EXP, make_event(2)["eventId"]) is None
    assert s.append(EXP, [make_event(2)])[0].tick == 2
    # absent and null optionals are the same event: a duplicate, stored without nulls
    s.append(EXP, [make_event(5, nulls=True)])
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(5)])
    stored = canonical_event_json(s.find_event(EXP, make_event(5)["eventId"]))
    assert stored == canonical_event_json({**make_event(5), "tick": 3}) and "null" not in stored


def test_branch_records_and_active_branch_are_written_with_the_events_or_not_at_all(harness):
    s = harness.store
    s.append(EXP, [make_event(1)], branches=[BranchRecord("main", None, None, "main")], active_branch_id="main")
    s.append(EXP, [make_event(2, branch="b1")], branches=[BranchRecord("b1", "main", 1, "try")], active_branch_id="b1")
    assert s.branches(EXP) == [BranchRecord("main", None, None, "main"), BranchRecord("b1", "main", 1, "try")]
    assert s.exploration(EXP).active_branch_id == "b1"
    s.set_active_branch(EXP, "main")
    with pytest.raises(InvalidEvent):
        s.set_active_branch(EXP, "ghost")
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(3), make_event(1)], branches=[BranchRecord("b2", "main", 1)], active_branch_id="b2")
    with pytest.raises(InvalidEvent):  # a branch id that already exists
        s.append(EXP, [make_event(4)], branches=[BranchRecord("main")])
    with pytest.raises(InvalidEvent):  # active branch with no record
        s.append(EXP, [make_event(5)], active_branch_id="nowhere")
    assert [b.branch_id for b in s.branches(EXP)] == ["main", "b1"]
    assert s.exploration(EXP).active_branch_id == "main" and s.tip_tick(EXP) == 2


# --- model-based sequences ---------------------------------------------------------

BRANCHES = ["main", "b1", "b2"]
codes = st.text(max_size=30)


def machine_for(kind: str):
    class StoreMachine(RuleBasedStateMachine):
        """The reference is a list of (event dict, tick); each step is applied to both."""

        def __init__(self):
            super().__init__()
            self.h = Harness(kind)
            self.model: list[dict] = []  # stored events as dicts with ticks
            self.ids: set[str] = set()
            self.n = 0
            self.branch_model: list[BranchRecord] = []
            self.active: str | None = None

        def teardown(self):
            self.h.close()

        def _fresh(self, code, branch, nulls):
            self.n += 1
            return make_event(self.n, code=code, branch=branch, nulls=nulls)

        def _record(self, events):
            for ev in events:
                tick = len(self.model) + 1
                self.model.append({**ev, "tick": tick})
                self.ids.add(ev["eventId"])

        @rule(code=codes, branch=st.sampled_from(BRANCHES), nulls=st.booleans())
        def append_one(self, code, branch, nulls):
            ev = self._fresh(code, branch, nulls)
            got = self.h.store.append(EXP, [ev])
            assert [e.tick for e in got] == [len(self.model) + 1]
            self._record([ev])

        @rule(items=st.lists(st.tuples(codes, st.sampled_from(BRANCHES)), min_size=0, max_size=5))
        def append_batch(self, items):
            evs = [self._fresh(c, b, False) for c, b in items]
            got = self.h.store.append(EXP, evs)
            assert [e.tick for e in got] == list(range(len(self.model) + 1, len(self.model) + 1 + len(evs)))
            self._record(evs)

        @rule(
            items=st.lists(st.tuples(codes, st.sampled_from(BRANCHES)), min_size=1, max_size=4),
            at=st.integers(0, 4),
            how=st.sampled_from(["dup_stored", "dup_in_batch", "invalid"]),
        )
        def failing_batch(self, items, at, how):
            evs = [self._fresh(c, b, False) for c, b in items]
            at = min(at, len(evs))
            if how == "dup_stored" and self.model:
                bad = dict(self.model[0])
                bad.pop("tick")
                exc = DuplicateEventId
            elif how == "dup_in_batch":
                bad = evs[0]
                exc = DuplicateEventId
            else:
                bad = self._fresh("x", "main", False)
                bad["payload"] = {"cellId": "c1"}
                exc = InvalidEvent
            evs.insert(at, bad)
            with pytest.raises(exc):
                self.h.store.append(EXP, evs)

        @rule(which=st.integers(0, 10_000), nulls=st.booleans())
        def duplicate_send(self, which, nulls):
            if not self.model:
                return
            sent = {k: v for k, v in self.model[which % len(self.model)].items() if k != "tick"}
            if nulls:
                sent.update({"authorHint": None, "editor": None})
            with pytest.raises(DuplicateEventId):
                self.h.store.append(EXP, [sent])

        @rule(
            code=codes,
            branch=st.sampled_from(BRANCHES),
            activate=st.sampled_from(["new", "other", "none"]),
            fail=st.booleans(),
        )
        def append_creating_a_branch(self, code, branch, activate, fail):
            """A batch that writes a branch record and maybe changes the active branch; with
            `fail` the same batch carries a duplicate and must change nothing at all."""
            if not self.model:
                return  # the exploration exists only once an event is stored
            have = {b.branch_id for b in self.branch_model}
            fresh = [b for b in BRANCHES if b not in have]
            ev = self._fresh(code, branch, False)
            records, active = [], None
            if fresh:
                records = [BranchRecord(fresh[0], "main", 1, "n")]
            if activate == "new" and records:
                active = records[0].branch_id
            elif activate == "other" and have:
                active = sorted(have)[0]
            evs = [ev]
            if fail:
                evs.append(dict(ev))  # same event id twice: the batch rolls back
                with pytest.raises(DuplicateEventId):
                    self.h.store.append(EXP, evs, branches=records, active_branch_id=active)
                return
            self.h.store.append(EXP, evs, branches=records, active_branch_id=active)
            self._record([ev])
            self.branch_model.extend(records)
            if active is not None:
                self.active = active

        @rule(pick=st.integers(0, 10))
        def set_active(self, pick):
            if not self.branch_model:
                return
            b = self.branch_model[pick % len(self.branch_model)].branch_id
            self.h.store.set_active_branch(EXP, b)
            self.active = b

        @rule()
        def set_active_unknown(self):
            if self.model:
                with pytest.raises(InvalidEvent):
                    self.h.store.set_active_branch(EXP, "ghost")

        if kind == "sqlite":

            @rule()
            def reopen(self):
                self.h.reopen()

        @invariant()
        def equals_the_model(self):
            s = self.h.store
            stored = s.events_for(EXP)
            assert dump(stored) == [canonical_event_json(m) for m in self.model]
            assert [e.tick for e in stored] == list(range(1, len(self.model) + 1))
            assert s.tip_tick(EXP) == len(self.model)
            assert len({str(e.eventId) for e in stored}) == len(stored)
            assert s.branches(EXP) == self.branch_model
            rec = s.exploration(EXP)
            assert (rec.active_branch_id if rec else None) == self.active

    StoreMachine.__name__ = f"StoreMachine_{kind}"
    return StoreMachine


def _machine_settings(kind: str):
    steps = 40 if DEEP else 20
    examples = (1000 if DEEP else 300) if kind == "memory" else (300 if DEEP else 60)
    return settings(max_examples=examples, stateful_step_count=steps)


for _kind in KINDS:
    _cls = machine_for(_kind)
    _cls.TestCase.settings = _machine_settings(_kind)
    globals()[f"TestModel_{_kind}"] = _cls.TestCase

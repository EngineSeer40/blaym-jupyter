"""The shared event store contract (S4, Task 2).

Runs against every implementation (in memory, SQLite). This suite is the definition of
correct behaviour: a future store is accepted by passing it. Includes a model-based
sequence test: a Python list is the reference, checked after every step.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from blaym_jupyter.ports.events import (
    BranchRecord,
    DuplicateEventId,
    InvalidEvent,
    canonical_event_json,
)
from blaym_jupyter.tests.history.conftest import DEEP

from .harness import KINDS, Harness

NS = uuid.UUID("5c1f0a6e-0000-4000-8000-0000000005a4")
EXP = "exp-1"


def make_event(n: int, *, exp: str = EXP, branch: str = "main", code: str = "x = 1", nulls: bool = False, **extra):
    """A `cell_source_changed` event without a tick. `n` makes the event id."""
    ev = {
        "schemaVersion": 2,
        "eventId": str(uuid.uuid5(NS, f"{exp}:{n}")),
        "explorationId": exp,
        "branchId": branch,
        "wallTimeMs": 1_760_000_000_000 + n,
        "userId": "local",
        "workspaceId": "local",
        "originId": "origin-1",
        "type": "cell_source_changed",
        "payload": {"cellId": "c1", "code": code, "revision": 1 + n},
    }
    if nulls:
        ev.update({"authorHint": None, "authorConfidence": None, "editor": None, "links": None})
    ev.update(extra)
    return ev


def dump(events):
    return [canonical_event_json(e) for e in events]


# --- named cases -----------------------------------------------------------------


def test_ticks_start_at_one_and_follow_in_order(harness):
    s = harness.store
    got = s.append(EXP, [make_event(1), make_event(2)])
    assert [e.tick for e in got] == [1, 2]
    got = s.append(EXP, [make_event(3)])
    assert [e.tick for e in got] == [3]
    assert [e.tick for e in s.events_for(EXP)] == [1, 2, 3]
    assert s.tip_tick(EXP) == 3


def test_empty_and_unknown_exploration(harness):
    s = harness.store
    assert s.events_for("nope") == []
    assert s.tip_tick("nope") == 0
    assert s.exploration("nope") is None
    assert s.branches("nope") == []
    assert s.append(EXP, []) == []
    assert s.exploration(EXP) is None


def test_events_come_back_exactly_as_written(harness):
    s = harness.store
    sent = make_event(
        1, branch="b-7", authorHint="ai", authorConfidence="guess", editor="jupyterlab",
        payload={"cellId": "c9", "code": "print('héllo ☃')\n", "revision": 4},
    )
    s.append(EXP, [sent])
    (back,) = s.events_for(EXP)
    assert back.tick == 1 and back.branchId == "b-7" and back.payload.code == "print('héllo ☃')\n"
    assert back.authorHint.value == "ai"
    assert str(back.eventId) == sent["eventId"]
    assert back == s.find_event(EXP, sent["eventId"])


def test_ticks_are_per_exploration(harness):
    s = harness.store
    s.append("a", [make_event(1, exp="a")])
    s.append("b", [make_event(2, exp="b"), make_event(3, exp="b")])
    s.append("a", [make_event(4, exp="a")])
    assert [e.tick for e in s.events_for("a")] == [1, 2]
    assert [e.tick for e in s.events_for("b")] == [1, 2]


def test_duplicate_event_id_is_refused_and_writes_nothing(harness):
    s = harness.store
    s.append(EXP, [make_event(1)])
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(1)])
    assert s.tip_tick(EXP) == 1
    assert s.append(EXP, [make_event(2)])[0].tick == 2


def test_failed_batch_rolls_back_and_leaves_the_next_tick_unchanged(harness):
    s = harness.store
    s.append(EXP, [make_event(1)])
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(2), make_event(3), make_event(1)])
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(4), make_event(4)])
    assert [e.tick for e in s.events_for(EXP)] == [1]
    assert s.find_event(EXP, make_event(2)["eventId"]) is None
    assert s.append(EXP, [make_event(2)])[0].tick == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "no_such_event"},
        {"payload": {"cellId": "c1", "code": "x", "revision": 0}},
        {"payload": {"cellId": "c1"}},
        {"surprise": 1},
        {"explorationId": "someone-else"},
        {"schemaVersion": 1},
    ],
)
def test_invalid_event_is_refused_and_writes_nothing(harness, bad):
    s = harness.store
    s.append(EXP, [make_event(1)])
    with pytest.raises(InvalidEvent):
        s.append(EXP, [make_event(2), make_event(3, **bad)])
    assert [e.tick for e in s.events_for(EXP)] == [1]
    assert s.append(EXP, [make_event(2)])[0].tick == 2


def test_null_optionals_and_absent_ones_are_the_same_event(harness):
    s = harness.store
    first = make_event(1)
    again = make_event(1, nulls=True)
    s.append(EXP, [first])
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [again])  # a duplicate, not a different event
    stored = s.find_event(EXP, first["eventId"])
    assert canonical_event_json({**again, "tick": 1}) == canonical_event_json(stored)
    assert "null" not in canonical_event_json(stored)
    # and the other way round, and nulls sent first
    s.append(EXP, [make_event(2, nulls=True)])
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(2)])
    assert canonical_event_json({**make_event(2), "tick": 2}) == canonical_event_json(s.find_event(EXP, make_event(2)["eventId"]))


def test_exploration_record_comes_from_the_first_event(harness):
    s = harness.store
    s.append(EXP, [make_event(1, userId="u1", workspaceId="w1")])
    rec = s.exploration(EXP)
    assert (rec.exploration_id, rec.user_id, rec.workspace_id, rec.active_branch_id) == (EXP, "u1", "w1", None)
    assert [r.exploration_id for r in s.explorations()] == [EXP]


def test_branch_records_and_active_branch_are_written_with_the_events(harness):
    s = harness.store
    s.append(EXP, [make_event(1)], branches=[BranchRecord("main", None, None, "main")], active_branch_id="main")
    s.append(
        EXP, [make_event(2, branch="b1")],
        branches=[BranchRecord("b1", "main", 1, "try")], active_branch_id="b1",
    )
    assert s.branches(EXP) == [BranchRecord("main", None, None, "main"), BranchRecord("b1", "main", 1, "try")]
    assert s.exploration(EXP).active_branch_id == "b1"
    s.set_active_branch(EXP, "main")
    assert s.exploration(EXP).active_branch_id == "main"
    with pytest.raises(InvalidEvent):
        s.set_active_branch(EXP, "ghost")
    assert s.exploration(EXP).active_branch_id == "main"


def test_a_failed_batch_leaves_no_branch_record_and_keeps_the_active_branch(harness):
    s = harness.store
    s.append(EXP, [make_event(1)], branches=[BranchRecord("main")], active_branch_id="main")
    with pytest.raises(DuplicateEventId):
        s.append(EXP, [make_event(2), make_event(1)], branches=[BranchRecord("b1", "main", 1)], active_branch_id="b1")
    with pytest.raises(InvalidEvent):  # a branch id that already exists
        s.append(EXP, [make_event(3)], branches=[BranchRecord("main")])
    with pytest.raises(InvalidEvent):  # active branch with no record
        s.append(EXP, [make_event(4)], active_branch_id="nowhere")
    assert [b.branch_id for b in s.branches(EXP)] == ["main"]
    assert s.exploration(EXP).active_branch_id == "main"
    assert s.tip_tick(EXP) == 1


def test_no_update_or_delete_path_exists(harness):
    public = {n for n in dir(harness.store) if not n.startswith("_")}
    assert not {n for n in public if n.startswith(("update", "delete", "remove", "replace", "edit", "drop"))}


def test_a_planted_hole_is_returned_as_a_hole_not_repaired(harness):
    s = harness.store
    ev = [dict(make_event(n), tick=n) for n in (1, 2, 4, 5)]
    from blaym_jupyter_types import BlaymEvent

    harness.load_exact(EXP, [BlaymEvent.model_validate(e) for e in reversed(ev)])
    assert [e.tick for e in s.events_for(EXP)] == [1, 2, 4, 5]
    assert s.tip_tick(EXP) == 5
    assert s.append(EXP, [make_event(9)])[0].tick == 6  # MAX+1: the hole stays
    assert [e.tick for e in s.events_for(EXP)] == [1, 2, 4, 5, 6]


def test_state_survives_reopening():
    h = Harness("sqlite")
    try:
        s = h.store
        s.append(EXP, [make_event(1), make_event(2)], branches=[BranchRecord("main")], active_branch_id="main")
        before = dump(s.events_for(EXP))
        s = h.reopen()
        assert dump(s.events_for(EXP)) == before
        assert s.tip_tick(EXP) == 2
        assert s.exploration(EXP).active_branch_id == "main"
        assert s.append(EXP, [make_event(3)])[0].tick == 3
        with pytest.raises(DuplicateEventId):
            s.append(EXP, [make_event(1)])
    finally:
        h.close()


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


def test_memory_and_sqlite_return_identical_events_for_identical_input():
    mem, sql = Harness("memory"), Harness("sqlite")
    try:
        batches = [
            [make_event(1, nulls=True), make_event(2, branch="b1", authorHint="ai", authorConfidence="known")],
            [make_event(3, code="é☃\n")],
        ]
        for b in batches:
            assert mem.store.append(EXP, b) == sql.store.append(EXP, b)
        assert mem.store.events_for(EXP) == sql.store.events_for(EXP)
        assert mem.store.find_event(EXP, make_event(2)["eventId"]) == sql.store.find_event(EXP, make_event(2)["eventId"])
    finally:
        mem.close()
        sql.close()


# --- author fields ----------------------------------------------------------------
# The trigger of the Task 3 finding: `changed_outside_editor` (and the matching recorder
# event) declare `authorHint` as `Literal["outside"]`, so a validated event holds the plain
# str "outside" there while every other event type holds the `AuthorHint` enum. A store that
# calls `.value` on the field fails for exactly those events.


def _author_events():
    from blaym_jupyter_types import AuthorConfidence, AuthorHint

    from blaym_jupyter.tests.history.scenario import EXPLORATION, Scenario

    plain = [
        make_event(1, exp=EXPLORATION, authorHint="person", authorConfidence="known"),
        make_event(2, exp=EXPLORATION, authorHint="ai", authorConfidence="guess"),
        make_event(3, exp=EXPLORATION, authorHint=AuthorHint.ai, authorConfidence=AuthorConfidence.guess),
        make_event(4, exp=EXPLORATION, authorHint="blaym"),
    ]
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.outside_change([("c1", "x = 2")])
    from_scenario = [e.model_dump(mode="json", exclude_none=True) for e in s.events]
    return EXPLORATION, plain, from_scenario


def test_author_fields_as_plain_strings_enum_values_and_scenario_events(harness):
    exp, plain, from_scenario = _author_events()
    s = harness.store
    got = s.append(exp, plain)
    assert [str(getattr(e.authorHint, "value", e.authorHint)) if e.authorHint else None for e in got] == \
        ["person", "ai", "ai", "blaym"]
    assert any(e["type"] == "changed_outside_editor" for e in from_scenario)
    got2 = s.append(exp, from_scenario)  # includes an event whose authorHint is the plain str "outside"
    outside = [e for e in got2 if e.type == "changed_outside_editor"]
    assert outside and all(str(e.authorHint) == "outside" for e in outside)
    assert len(s.events_for(exp)) == len(plain) + len(from_scenario)


def test_the_literal_outside_hint_is_a_plain_str_after_validation():
    """Guards the premise of the test above: if the contract ever makes this an enum the
    store's tolerance is still right, but the trigger would have changed."""
    from blaym_jupyter_types import BlaymEvent

    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.outside_change([("c1", "x = 2")])
    ev = BlaymEvent.model_validate({**s.events[-1].model_dump(mode="json", exclude_none=True)}).root
    assert ev.type == "changed_outside_editor" and type(ev.authorHint) is str

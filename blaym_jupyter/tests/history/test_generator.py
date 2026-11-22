"""Self-tests of the sequence generator (S3a, Task 4).

They show the generator is trustworthy before the properties of Task 6 rely on it:
valid histories are `ok` everywhere, every action and delivery problem occurs, and
each deliberately invalid history gives the typed answer it was built for.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import reference_rebuild, reference_replay_all
from blaym_jupyter.tests.history.conftest import DEEP, MAX_EVENTS, POSITION_LIMIT
from blaym_jupyter.tests.history.test_adversarial import cross_check
from blaym_jupyter.tests.history.generate import (
    ALL_EVENT_TYPES,
    INVALID_KINDS,
    MUTATIONS,
    SHAPES,
    deliveries,
    histories,
    invalid_histories,
)

EXPECTED_TAGS = {
    "edit_while_running", "clear_wait", "clear_nowait", "output_after_clear_wait",
    "restart_with_unfinished_run", "streamed_chat_update", "gap_recovery_snapshot",
    "branch_from_first_event", "branch_before_parent_existed", "branch_from_parent_latest",
    "nested_branch", "nested_3_levels", "sibling_branches",
    "output_for_deleted_cell", "output_for_deleted_running_cell", "snapshot_drops_running_cell",
}


def _positions(h, limit: int = POSITION_LIMIT):
    """Every (branch, tick) for short histories; a spread plus every branch head otherwise."""
    last = h.last_tick
    ticks = list(range(1, last + 1))
    if len(ticks) > limit:
        step = max(1, len(ticks) // limit)
        keep = set(ticks[::step]) | {1, last}
        for e in h.events:  # every branch point with its neighbours, and every branch head
            if e.type == "branch_point_created":
                keep |= {e.tick - 1, e.tick + 1, e.payload.parentTick}
        for branch in h.branches:
            keep.add(max(x.tick for x in h.events if x.branchId == branch))
        ticks = sorted(t for t in keep if 1 <= t <= last)
    return [(b, t) for b in h.branches for t in ticks]


@settings(max_examples=1000 if DEEP else 100)
@given(histories(max_events=MAX_EVENTS))
def test_valid_histories_are_ok_at_every_position_on_every_branch(h):
    assert [e.tick for e in h.events] == list(range(1, len(h.events) + 1))
    assert len({str(e.eventId) for e in h.events}) == len(h.events)
    for (branch, tick), r in reference_replay_all(h.events).items():  # every position
        assert r.status == "ok", f"{branch}@{tick}: {r}"


def test_every_event_type_action_shape_and_tag_occurs():
    types, shapes, tags, lengths, branch_counts = Counter(), Counter(), Counter(), [], Counter()

    @settings(max_examples=150, derandomize=True)
    @given(histories())
    def collect(h):
        types.update(e.type for e in h.events)
        shapes[h.shape] += 1
        tags.update(h.tags)
        lengths.append(len(h.events))
        branch_counts[len(h.branches)] += 1

    collect()
    assert set(types) == set(ALL_EVENT_TYPES), set(ALL_EVENT_TYPES) - set(types)
    assert set(shapes) == set(SHAPES)
    assert EXPECTED_TAGS <= set(tags), EXPECTED_TAGS - set(tags)
    assert min(lengths) <= 5 and max(lengths) >= 60
    assert {1, 2, 3, 4}.issubset(branch_counts), branch_counts


def test_every_delivery_problem_occurs_and_is_labelled():
    seen = Counter()

    @settings(max_examples=150, derandomize=True)
    @given(deliveries())
    def collect(d):
        for label in d.labels:
            seen[label.split(":")[0]] += 1
        if d.dropped_ticks:
            assert d.first_missing_tick == min(d.dropped_ticks)

    collect()
    assert set(seen) == set(MUTATIONS), set(MUTATIONS) - set(seen)


@settings(max_examples=1000 if DEEP else 100)
@given(histories(max_events=MAX_EVENTS))
def test_valid_histories_agree_with_the_independent_oracle(h):
    """Runs at the profile's example count (300 default, 1,000 deep), histories up to
    `MAX_EVENTS` events (40 default, 80 deep), `POSITION_LIMIT` positions per branch."""
    cross_check(list(h.events), _positions(h))


@settings(max_examples=1000 if DEEP else 100)
@given(st.data())
def test_delivery_labels_match_what_was_done(data):
    d = data.draw(deliveries())
    ids = Counter(str(e.eventId) for e in d.events)
    for event_id in d.conflict_event_ids:
        assert ids[event_id] >= 2
        versions = {e.model_dump_json() for e in d.events if str(e.eventId) == event_id}
        assert len(versions) >= 2
    by_tick = {}
    for e in d.events:
        by_tick.setdefault(e.tick, set()).add(str(e.eventId))
    for tick in d.clash_ticks:
        assert len(by_tick[tick]) >= 2
    for branch in d.orphan_branches:
        bp = next(e for e in d.events if e.branchId == branch and e.type == "branch_point_created")
        assert bp.payload.parentBranchId == "no-such-parent"
    if d.unknown_branch:
        assert all(e.branchId != d.unknown_branch for e in d.events)
    if not d.labels:
        assert d.events == d.history.events


@given(invalid_histories())
def test_invalid_histories_give_the_intended_typed_answer(ih):
    r = reference_rebuild(ih.events, ih.branch, ih.tick)
    assert r.status == ih.expected_status, f"{ih.kind}: {r}"
    if ih.expected_reason is not None:
        assert r.reason == ih.expected_reason, f"{ih.kind}: {r}"
    assert str(r.eventId) == ih.event_id, f"{ih.kind}: {r}"


def test_every_invalid_kind_gives_its_intended_answer_when_asked_for_alone():
    results = set()
    for kind in INVALID_KINDS:

        @settings(max_examples=15, derandomize=True)
        @given(invalid_histories(kinds=(kind,)))
        def check(ih):
            assert ih.kind == kind
            r = reference_rebuild(ih.events, ih.branch, ih.tick)
            assert r.status == ih.expected_status, f"{kind}: {r}"
            if ih.expected_reason is not None:
                assert r.reason == ih.expected_reason, f"{kind}: {r}"
            results.add((ih.expected_status, ih.expected_reason))

        check()
    reasons = {r for s, r in results if s == "invalid_event"}
    assert reasons == {
        "first_event_not_snapshot", "tick_clash", "duplicate_id", "unknown_run", "run_not_running",
        "unknown_message", "unknown_gap", "gap_already_ended", "no_output_at_index",
        "index_out_of_range", "position_mismatch", "invalid_branch_point", "run_cell_mismatch"}
    assert {s for s, _ in results} == {"invalid_event", "unknown_cell", "invalid_revision"}

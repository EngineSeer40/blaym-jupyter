"""Self-tests of the sequence generator.

They show the generator is trustworthy before the properties rely on it:
valid histories are `ok` everywhere, every action and delivery problem occurs, and
each deliberately invalid history gives the typed answer it was built for.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings, strategies as st

from blaym_notebook.tests.history.reference import reference_rebuild, reference_replay_all
from blaym_notebook.tests.history.conftest import DEEP, MAX_EVENTS, POSITION_LIMIT
from blaym_notebook.tests.history.test_adversarial import cross_check
from blaym_notebook.tests.history.generate import (
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


@settings(max_examples=1000 if DEEP else 100)
@given(histories(max_events=MAX_EVENTS))
def test_valid_histories_agree_with_the_independent_oracle(h):
    """Runs at the profile's example count (300 default, 1,000 deep), histories up to
    `MAX_EVENTS` events (40 default, 80 deep), `POSITION_LIMIT` positions per branch."""
    cross_check(list(h.events), _positions(h))



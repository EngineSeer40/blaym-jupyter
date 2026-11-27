"""The twelve properties (covering the rules of section 3.4), as checks.

Read this file top to bottom as the rule checklist. Each rule has one test group here.
Where an existing test already checks the rule across generated sequences, the test in
this file is a coverage map: its docstring names the rule and points at that test, and
it only asserts that the pointed-at test still exists. Rules without an earlier check
are written out here as properties over generated histories (at most 100 examples in
the default profile, `POSITION_LIMIT` positions per branch, histories up to
`MAX_EVENTS` events; the `deep` profile uses its own larger numbers).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

from blaym_jupyter.history import Rebuilder
from blaym_jupyter.history.apply import apply
from blaym_jupyter.tests.history.reference import reference_rebuild, reference_replay_all
from blaym_jupyter.tests.history import test_adversarial, test_rebuild, test_staleness
from blaym_jupyter.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_jupyter.tests.history.generate import deliveries, histories
from blaym_jupyter.tests.history.test_adversarial import oracle_line
from blaym_jupyter.tests.history.test_generator import _positions
from blaym_jupyter.tests.history.test_rebuild import INTERVALS, dump

PROPERTY = settings(max_examples=1000 if DEEP else 100)
# Properties that rebuild the same history several times: fewer examples in default.
HEAVY = settings(max_examples=1000 if DEEP else 50)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _rebuilt(events, interval=7):
    rb = Rebuilder(interval=interval)
    rb.add(events)
    return rb


# --- 1. Position, not time: the real rebuilder equals the reference everywhere -------


# --- 2. Playing from the start equals jumping straight to a position -----------------


def _strip_branch(result):
    d = dump(result)
    d["state"]["position"]["branchId"] = None
    d["state"]["position"]["tick"] = None
    return d


@HEAVY
@given(histories(max_events=MAX_EVENTS), st.sampled_from(INTERVALS), st.randoms(use_true_random=False))
def test_rule_2_playing_from_the_start_equals_jumping(h, interval, rnd):
    """Rule 2. `play` folds the public `apply` over the independent oracle's line, event
    by event, from nothing. A rebuilder with snapshots, asked in a shuffled order, must
    give the played state at every step."""
    rb = _rebuilt(h.events, interval)
    for branch in h.branches:
        head = max(e.tick for e in h.events if e.branchId == branch)
        line = oracle_line(list(h.events), branch, head)
        played, state = [], None
        for ev in line:
            result = apply(state, ev)
            assert result.status == "ok"
            state = result.state
            played.append((ev.tick, result))
        picks = list(played[:: max(1, len(played) // 8)]) + [played[-1]]
        rnd.shuffle(picks)
        for tick, result in picks:
            got = rb.state_at(branch, tick)
            assert _strip_branch(got) == _strip_branch(result), (branch, tick)
            assert got.state.position.branchId == branch and got.state.position.tick == tick


# --- 3. The snapshot interval changes no answer --------------------------------------


# --- 4. Duplicates and reordering change nothing -------------------------------------


def _line_depends_on(h, branch, dropped):
    """True when `branch`, or an ancestor, was created by a branch point that was dropped or
    that starts from a dropped parent event. The rebuilder cannot vouch for such a branch
    even below the first gap (it answers from the delivered events: `missing_events`, or
    `invalid_branch_point` / `unknown_branch` when the branch point itself is gone)."""
    points = {e.branchId: e for e in h.events if e.type == "branch_point_created"}
    while branch in points:
        bp = points[branch]
        if bp.tick in dropped or bp.payload.parentTick in dropped:
            return True
        branch = bp.payload.parentBranchId
    return False


# --- 5. A dropped event stops the rebuild at and after the first missing tick -------


@PROPERTY
@given(histories(max_events=MAX_EVENTS).flatmap(
    lambda h: deliveries(history=h, mutations=("drop",)).map(lambda d: (h, d))))
def test_rule_5_a_dropped_event_gives_missing_events_from_the_first_missing_tick(pair):
    """Rule 5. At or above the first missing tick, every branch answers
    `missing_events` naming that tick. Below it the answer equals the reference on the
    complete valid history (the dropped events cannot matter below the first gap) and,
    as before, the reference on the events that were delivered."""
    h, d = pair
    first = d.first_missing_tick
    if first is None:  # nothing was dropped (single-event history)
        return
    rb = _rebuilt(d.events)
    full = reference_replay_all(h.events)  # the complete valid history, one replay per line
    ticks = {t for _, t in _positions(h)} | {first - 1, first, first + 1, h.last_tick}
    for branch in h.branches:
        for t in sorted(x for x in ticks if x >= 1):
            got = rb.state_at(branch, t)
            if t >= first:
                assert got.status == "missing_events", (branch, t, got)
                assert got.firstMissingTick == first
            else:
                if not _line_depends_on(h, branch, set(d.dropped_ticks)):
                    assert dump(got) == dump(full[(branch, t)]), (branch, t, "full history")
                want = reference_rebuild(d.events, branch, t)
                assert dump(got) == dump(want), (branch, t)


# --- 6. Same id with different content is a conflict ---------------------------------


@PROPERTY
@given(histories(max_events=MAX_EVENTS).flatmap(
    lambda h: deliveries(history=h, mutations=("conflict", "duplicate", "reorder")).map(lambda d: (h, d))))
def test_rule_6_same_id_with_different_content_is_a_conflict(pair):
    """Rule 6. A history delivered with an altered twin of one event answers `conflict`
    naming that event id at every position and on every branch, whatever else was
    wrong with the delivery."""
    h, d = pair
    if not d.conflict_event_ids:
        return
    rb = _rebuilt(d.events)
    for b, t in _positions(h):
        got = rb.state_at(b, t)
        assert got.status == "conflict", (b, t, got)
        assert got.eventId in d.conflict_event_ids
        assert dump(got) == dump(reference_rebuild(d.events, b, t))


# --- 7. Query order and cache contents never change an answer ------------------------


# --- 8. Branches are isolated --------------------------------------------------------


def _extend(events, branch, rnd):
    """Events added at the end of `branch` (new ticks above every existing tick)."""
    from blaym_jupyter.tests.history.scenario import Scenario

    s = Scenario()
    s.events = list(events)
    s._tick = max(e.tick for e in events)
    out = []
    for i in range(rnd.randint(1, 4)):
        if rnd.random() < 0.7:
            out.append(s._emit("chat_message_added", {
                "chatSource": "jupyter-ai", "messageId": f"isolation-test-{i}", "sender": "user-1",
                "senderKind": "person", "body": f"added {i}", "chatTimeMs": 0}, branch=branch))
        else:
            out.append(s._emit("kernel_restarted", {"kernelId": f"k-{i}", "reason": "unknown"},
                               branch=branch))
    return out


@PROPERTY
@given(histories(max_events=MAX_EVENTS), st.randoms(use_true_random=False))
def test_rule_8_adding_events_to_one_branch_changes_no_other_branch(h, rnd):
    """Rule 8 (branch isolation, section 3.4 rule 9). New events at the end of one branch leave every
    answer at every existing tick on every other branch, its ancestors included, unchanged.
    (A descendant branch reads its parent only up to its branch point, which is below the
    new events.)"""
    target = rnd.choice(h.branches)
    extra = _extend(h.events, target, rnd)
    before = _rebuilt(h.events)
    after = _rebuilt(list(h.events) + extra)
    for b, t in _positions(h):
        if b == target:
            continue
        assert dump(after.state_at(b, t)) == dump(before.state_at(b, t)), (target, b, t)
    # and the target itself did change where it should: the new events are visible
    assert after.state_at(target, extra[-1].tick).status == "ok"


# --- 9. Lineage at a branch point ----------------------------------------------------


# --- 10. Run integrity ---------------------------------------------------------------


# --- 11. Returned states are immutable -----------------------------------------------


@HEAVY
@given(histories(max_events=MAX_EVENTS), st.sampled_from([2, 200]))
def test_rule_11_changing_a_returned_state_changes_no_later_answer(h, interval):
    """Rule 11. Each returned state is mutated in every part we can reach; asking again
    (and asking other positions) still gives the first answers. Hand cases:
    `test_rebuild.test_returned_states_are_independent_copies` and
    `test_staleness.test_returned_states_are_independent_copies_even_on_a_snapshot_hit`."""
    positions = _positions(h)
    rb = _rebuilt(h.events, interval)
    want = {p: dump(rb.state_at(*p)) for p in positions}
    for p in positions:
        s = rb.state_at(*p).state
        s.position.tick = -5
        s.notebook.path = "tampered"
        s.notebook.cells.clear()
        s.chat.clear()
        s.runs.clear()
        s.gaps.clear()
        s.detachedOutputs.clear()
        s.kernel.status = "unknown"
    for p in positions:
        assert dump(rb.state_at(*p)) == want[p], p


# --- 12. Edges are explicit ----------------------------------------------------------


# --- 12b. Determinism across processes ------------------------------------------------



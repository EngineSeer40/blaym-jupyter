"""The twelve properties of S3a Task 6 (covering the rules of section 3.4), as checks.

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

from blaym_jupyter.history import Rebuilder, apply, reference_rebuild, reference_replay_all
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


def test_rule_1_real_equals_reference_at_every_position():
    """Rule 1 (and the core equivalence). Covered by
    `test_rebuild.test_valid_histories_match_the_reference_at_sampled_positions`
    and the two delivery properties there; clock time is never an input
    (`test_adversarial.test_wall_clock_is_never_used_for_ordering`)."""
    assert callable(test_rebuild.test_valid_histories_match_the_reference_at_sampled_positions)
    assert callable(test_rebuild.test_deliveries_match_the_reference_in_one_batch)
    assert callable(test_adversarial.test_wall_clock_is_never_used_for_ordering)


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


@HEAVY
@given(histories(max_events=MAX_EVENTS))
def test_rule_3_snapshot_interval_changes_no_answer(h):
    """Rule 3 (snapshots are only a shortcut, section 3.4 rule 8). Intervals 1, 7, 50, 200 and none give identical answers."""
    positions = _positions(h)
    answers = []
    for interval in INTERVALS:
        rb = _rebuilt(h.events, interval)
        answers.append([dump(rb.state_at(b, t)) for b, t in positions])
    assert all(a == answers[0] for a in answers[1:])


# --- 4. Duplicates and reordering change nothing -------------------------------------


@PROPERTY
@given(histories(max_events=MAX_EVENTS).flatmap(
    lambda h: deliveries(history=h, mutations=("duplicate", "reorder")).map(lambda d: (h, d))))
def test_rule_4_reordering_and_duplication_change_no_answer(pair):
    """Rule 4. The same history delivered shuffled and with repeats gives the answers
    of the history delivered once in order, on every sampled position."""
    h, d = pair
    ordered = _rebuilt(h.events)
    messy = _rebuilt(d.events)
    for b, t in _positions(h):
        assert dump(messy.state_at(b, t)) == dump(ordered.state_at(b, t)), (b, t)


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


@HEAVY
@given(histories(max_events=MAX_EVENTS), st.sampled_from([1, 3, 7, 200]), st.randoms(use_true_random=False))
def test_rule_7_question_order_and_cache_temperature_change_no_answer(h, interval, rnd):
    """Rule 7. Every position is asked cold (fresh rebuilder, no snapshots), then the
    same positions are asked again, repeated and in a shuffled order, on one warm
    rebuilder. Late-arriving events and tiny batches are covered by `test_staleness`."""
    assert callable(test_staleness.test_tiny_batches_late_events_and_extras_never_serve_stale_answers)
    positions = _positions(h)
    cold = _rebuilt(h.events, None)
    want = {p: dump(cold.state_at(*p)) for p in positions}
    warm = _rebuilt(h.events, interval)
    asked = positions * 2
    rnd.shuffle(asked)
    for p in asked:
        assert dump(warm.state_at(*p)) == want[p], p


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
                "messageId": f"isolation-test-{i}", "sender": "user-1",
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


@PROPERTY
@given(histories(max_events=MAX_EVENTS))
def test_rule_9_a_branch_starts_as_its_parent_state_plus_the_branch_point(h):
    """Rule 9 (lineage, section 3.4 rule 2). The state on the child at the branch point's own tick is the
    parent's state at `parentTick` with the branch point applied (apply, from the
    parent's rebuilt state), on the new branch's position."""
    rb = _rebuilt(h.events)
    for e in h.events:
        if e.type != "branch_point_created":
            continue
        parent = rb.state_at(e.payload.parentBranchId, e.payload.parentTick)
        assert parent.status == "ok"
        expected = apply(parent.state, e)
        assert expected.status == "ok"
        got = rb.state_at(e.branchId, e.tick)
        assert got.status == "ok"
        assert _strip_branch(got) == _strip_branch(expected), e.branchId
        p = got.state.position
        assert (p.branchId, p.tick, p.lastEventTick) == (e.branchId, e.tick, e.tick)
        # the cut is exact: the child shows nothing of the parent after parentTick
        later = rb.state_at(e.payload.parentBranchId, e.payload.parentTick)
        assert dump(later)["state"]["notebook"] == dump(got)["state"]["notebook"]


# --- 10. Run integrity ---------------------------------------------------------------


@PROPERTY
@given(histories(max_events=MAX_EVENTS))
def test_rule_10_runs_keep_their_revision_and_outputs_belong_to_their_run(h):
    """Rule 10. At every sampled position: a run records the revision that was sent,
    which is the cell's revision at the tick the run started and never changes later
    (the cell may have been edited during the run); every output group sits on the cell
    its run belongs to, or among the detached outputs when that cell is gone. The
    named scenario `edit_a_cell_while_it_runs` shows it on a readable case."""
    rb = _rebuilt(h.events)
    for branch, tick in _positions(h):
        s = rb.state_at(branch, tick).state
        by_run = {r.runId: r for r in s.runs}
        cells = {c.cellId for c in s.notebook.cells}
        for c in s.notebook.cells:
            for g in c.outputs:
                assert g.runId in by_run and by_run[g.runId].cellId == c.cellId, (branch, tick, g.runId)
        for g in s.detachedOutputs:
            assert g.runId in by_run and by_run[g.runId].cellId not in cells, (branch, tick, g.runId)
        for r in s.runs:
            at_start = rb.state_at(branch, r.startedTick).state
            cell = next(c for c in at_start.notebook.cells if c.cellId == r.cellId)
            assert cell.revision == r.cellRevision, (branch, r.runId)
            then = next(x for x in at_start.runs if x.runId == r.runId)
            assert (then.cellRevision, then.runCount, then.startedTick) == (
                r.cellRevision, r.runCount, r.startedTick)


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


@PROPERTY
@given(histories(max_events=MAX_EVENTS))
def test_rule_12_edges_appear_in_the_state_where_they_happened(h):
    """Rule 12. Read off the line's events independently of `apply`: every gap that was
    opened and not closed is an open gap, every closed one has its end tick; the last
    kernel restart and the last outside change are marked with their ticks; a run that
    started and was not finished before a later restart is `unfinished`, one without
    either is `running`."""
    rb = _rebuilt(h.events)
    for branch, tick in _positions(h):
        s = rb.state_at(branch, tick).state
        line = oracle_line(list(h.events), branch, tick)
        opened = {e.payload.gapId: e.tick for e in line if e.type == "recording_gap_started"}
        closed = {e.payload.gapId: e.tick for e in line if e.type == "recording_gap_ended"}
        assert {g.gapId: (g.startedTick, g.endedTick) for g in s.gaps} == {
            g: (t, closed.get(g)) for g, t in opened.items()}, (branch, tick)
        restarts = [e for e in line if e.type == "kernel_restarted"]
        if restarts:
            assert s.kernel.lastRestart is not None and s.kernel.lastRestart.tick == restarts[-1].tick
        else:
            assert s.kernel.lastRestart is None
        outside = [e for e in line if e.type == "changed_outside_jupyter"]
        assert (s.lastOutsideChange.tick if s.lastOutsideChange else None) == (
            outside[-1].tick if outside else None)
        finished = {e.payload.runId for e in line if e.type == "run_finished"}
        for r in s.runs:
            if r.runId in finished:
                continue
            restarted_after = any(x.tick > r.startedTick for x in restarts)
            assert r.status.root == ("unfinished" if restarted_after else "running"), (branch, tick, r.runId)


# --- 12b. Determinism across processes ------------------------------------------------


def digest(examples: int = 12) -> str:
    """Hash of the serialised events and answers of a fixed, derandomised sample of
    histories. The same source gives the same sample in every process."""
    h = hashlib.sha256()

    @settings(derandomize=True, max_examples=examples, database=None, deadline=None)
    @given(histories(max_events=MAX_EVENTS))
    def run(hist):
        h.update(json.dumps([e.model_dump(mode="json") for e in hist.events]).encode())
        rb = _rebuilt(hist.events, 7)
        for b, t in _positions(hist):
            h.update(json.dumps(rb.state_at(b, t).model_dump(mode="json")).encode())

    run()
    return h.hexdigest()


def test_rule_12_same_input_gives_the_same_answers_across_processes():
    """Rule 12 (determinism). The digest of the serialised answers for a fixed
    sample is the same in this process and in fresh interpreters with different string
    hash seeds (which change set and dict-of-set iteration order)."""
    here = digest()
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c",
             "from blaym_jupyter.tests.history.test_properties import digest; print(digest())"],
            cwd=PROJECT_ROOT, env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True, text=True, check=True, timeout=120)
        assert out.stdout.strip().splitlines()[-1] == here, f"PYTHONHASHSEED={seed}"

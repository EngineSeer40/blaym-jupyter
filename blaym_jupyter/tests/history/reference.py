"""The deliberately simple reference rebuilder (S3a, Task 3).

Slow and obviously correct: every call starts from nothing, works out the line of
events for the asked position, applies them one by one with `apply`, and keeps a
separate full copy of the state after each event. No snapshots, no caching.

Order of answers: `conflict` (same id, different content), then `invalid_event` with
`tick_clash` (two different events at one tick), then `missing_events`
(rule 5: any missing tick at or below the target, on every branch; a target
beyond the last tick counts as missing the tick after it), then `unknown_branch`
and `lineage_loop` (rule 2), then the first error `apply` reports.

The state returned for a position on branch B is the state after the last event
of B's line with tick <= the asked tick; `position.tick` is the asked tick and
`position.lastEventTick` that last event's tick (ruling C).
Branch points are validated (ruling B), each on its own against its parent's line:
`parentTick` before the branch point's own tick and on the parent's line, and every
non-root branch starts with its branch point (`invalid_branch_point`). Consistency
ruling: an invalid first branch point invalidates every position on that branch and
its descendants (it defines the ancestry, which cannot be trusted); a branch point
that is not the first event of its branch is invalid at its own tick, for positions
whose line includes it, and earlier positions are unaffected.
Asking for a tick before B existed gives
the parent's state at that tick, and the position still names B (S3b rule 2).
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from blaym_jupyter_types import (
    BlaymEvent,
    RebuildConflict,
    RebuildInvalidEvent,
    RebuildLineageLoop,
    RebuildMissingEvents,
    RebuildOk,
    RebuildUnknownBranch,
)

from blaym_jupyter.history.apply import ApplyResult, apply, unwrap
from blaym_jupyter.history.rebuild import StateResult

ReferenceResult = StateResult


def _dedupe(events):
    """Return (events by id in first-seen order, conflict or None)."""
    by_id: dict[str, object] = {}
    for raw in events:
        ev = unwrap(raw)
        key = str(ev.eventId)
        if key in by_id:
            if by_id[key].model_dump(mode="json") != ev.model_dump(mode="json"):
                return None, RebuildConflict(
                    status="conflict",
                    eventId=key,
                    first=BlaymEvent(by_id[key]),
                    second=BlaymEvent(ev),
                )
            continue
        by_id[key] = ev
    return list(by_id.values()), None


def _invalid_branch_point(ev, detail: str) -> RebuildInvalidEvent:
    return RebuildInvalidEvent(
        status="invalid_event",
        eventId=str(ev.eventId),
        tick=ev.tick,
        reason="invalid_branch_point",
        detail=detail,
    )


def reference_rebuild(events, branch_id: str, tick: int) -> ReferenceResult:
    unique, conflict = _dedupe(events)
    if conflict is not None:
        return conflict

    by_tick: dict[int, object] = {}
    for ev in unique:
        if ev.tick in by_tick:
            return RebuildInvalidEvent(
                status="invalid_event",
                eventId=str(ev.eventId),
                tick=ev.tick,
                reason="tick_clash",
                detail=f"two different events at tick {ev.tick}",
            )
        by_tick[ev.tick] = ev

    first_missing = 1
    while first_missing in by_tick:
        first_missing += 1
    if tick >= first_missing or tick < 1:  # a tick before the first event cannot be vouched for
        return RebuildMissingEvents(status="missing_events", firstMissingTick=first_missing)

    own: dict[str, list] = defaultdict(list)
    branch_point: dict[str, object] = {}
    for t in sorted(by_tick):
        ev = by_tick[t]
        if ev.branchId not in own and ev.type == "branch_point_created":
            branch_point[ev.branchId] = ev  # only counts as the branch's first event
        own[ev.branchId].append(ev)

    root_branch = by_tick[1].branchId  # one root per notebook history

    chain: list[str] = []  # target first, root last
    b = branch_id
    while True:
        if b in chain:
            return RebuildLineageLoop(status="lineage_loop", branchIds=chain[chain.index(b):])
        if b not in own:
            return RebuildUnknownBranch(status="unknown_branch", branchId=b)
        chain.append(b)
        bp = branch_point.get(b)
        if bp is None:
            if b != root_branch:
                first = own[b][0]
                return _invalid_branch_point(
                    first, f"branch {b} does not start with a branch point and is not the root"
                )
            break
        b = bp.payload.parentBranchId

    # Ruling B: every branch point on the lineage is checked on its own, against its own
    # parent's line at its own parentTick, never with a cut taken from a descendant.
    for i, child in enumerate(chain[:-1]):
        bp = branch_point[child]
        pt = bp.payload.parentTick
        if pt >= bp.tick:
            return _invalid_branch_point(bp, f"parentTick {pt} is not before the branch point at tick {bp.tick}")
        cap = {chain[i + 1]: pt}  # the parent's line: its events up to pt, then its ancestors' up to theirs
        for c, par in zip(chain[i + 1:], chain[i + 2:]):
            cap[par] = min(cap[c], branch_point[c].payload.parentTick)
        at = by_tick.get(pt)
        if at is None:  # the parent event was never delivered: a gap, not a guess
            return RebuildMissingEvents(status="missing_events", firstMissingTick=first_missing)
        if at.branchId not in cap or at.tick > cap[at.branchId]:
            return _invalid_branch_point(
                bp, f"parentTick {pt} is not an event on the line of branch {chain[i + 1]}"
            )

    cutoff = {chain[0]: tick}
    for child, parent in zip(chain, chain[1:]):
        cutoff[parent] = min(cutoff[child], branch_point[child].payload.parentTick)

    line = [ev for name in reversed(chain) for ev in own[name] if ev.tick <= cutoff[name]]
    if not line:
        return RebuildMissingEvents(status="missing_events", firstMissingTick=1)

    # A branch point that is not its branch's first event is invalid at its own tick.
    # Only events on the line count, so positions before it are unaffected.
    for ev in line:
        if ev.type == "branch_point_created" and own[ev.branchId][0] is not ev:
            return _invalid_branch_point(
                ev, f"branch point at tick {ev.tick} is not the first event of branch {ev.branchId}"
            )

    state = None
    copies = []  # a full separate copy after every event
    for ev in line:
        result = apply(state, ev)
        if not isinstance(result, RebuildOk):
            return result
        state = result.state
        copies.append(deepcopy(state))

    answer = copies[-1]
    answer.position.branchId = branch_id
    answer.position.tick = tick  # the tick asked for; lastEventTick says what the content reflects
    return RebuildOk(status="ok", state=answer)


def reference_all(events) -> dict[tuple[str, int], ReferenceResult]:
    """The answer at every (branch, tick) for every branch and tick 1..last."""
    unique, _ = _dedupe(events)
    unique = unique or []
    branches = sorted({ev.branchId for ev in unique})
    last = max((ev.tick for ev in unique), default=0)
    return {
        (b, t): reference_rebuild(events, b, t) for b in branches for t in range(1, last + 1)
    }


def reference_replay_all(events, positions=None) -> dict[tuple[str, int], ReferenceResult]:
    """The same answers as `reference_all`, from one straight replay per branch line.

    For each branch the line (ancestors up to their branch points, then the branch's own
    events) is applied once, in order, keeping a full copy of the state after each event;
    the answer at (branch, tick) is that copy for the last line event at or below `tick`
    (still no snapshots and no cache across calls). Ticks before the branch existed are
    the parent's answer, named for the branch. If anything is irregular (a conflict, a
    tick clash, a gap, an invalid or looping branch point, an event `apply` rejects) the
    answers of the irregular history come from `reference_rebuild`, position by position
    (only for `positions` when given, which may be any (branch, tick) pairs).
    """
    answers = _replay_all_clean(events)
    if answers is None:
        if positions is None:
            return reference_all(events)
        return {(b, t): reference_rebuild(events, b, t) for b, t in positions}
    if positions is not None:  # outside 1..last or unknown branches: ask the reference
        return {p: answers[p] if p in answers else reference_rebuild(events, *p) for p in positions}
    return answers


def _replay_all_clean(events):
    unique, conflict = _dedupe(events)
    if conflict is not None or not unique:
        return None
    by_tick = {ev.tick: ev for ev in unique}
    last = max(by_tick)
    if len(by_tick) != len(unique) or len(by_tick) != last or min(by_tick) != 1:
        return None  # a clash or a gap

    own: dict[str, list] = defaultdict(list)
    for t in sorted(by_tick):
        own[by_tick[t].branchId].append(by_tick[t])
    root = by_tick[1].branchId
    branch_point = {}
    for b, evs in own.items():
        for i, ev in enumerate(evs):
            if ev.type == "branch_point_created":
                if i != 0 or b == root:
                    return None
                branch_point[b] = ev
        if b != root and b not in branch_point:
            return None

    lines: dict[str, list] = {}
    for b in own:
        chain, x = [], b
        while True:
            if x in chain or x not in own:
                return None
            chain.append(x)
            if x == root:
                break
            x = branch_point[x].payload.parentBranchId
        for i, child in enumerate(chain[:-1]):  # each branch point against its own parent's line
            bp = branch_point[child]
            pt = bp.payload.parentTick
            cap = {chain[i + 1]: pt}
            for c, par in zip(chain[i + 1:], chain[i + 2:]):
                cap[par] = min(cap[c], branch_point[c].payload.parentTick)
            at = by_tick.get(pt)
            if pt >= bp.tick or at is None or at.branchId not in cap or at.tick > cap[at.branchId]:
                return None
        cap = {chain[0]: last}
        for child, parent in zip(chain, chain[1:]):
            cap[parent] = min(cap[child], branch_point[child].payload.parentTick)
        lines[b] = [ev for name in reversed(chain) for ev in own[name] if ev.tick <= cap[name]]

    answers: dict[tuple[str, int], ReferenceResult] = {}
    copies_of: dict[str, list] = {}
    for b, line in lines.items():
        state, copies = None, []
        for ev in line:
            result = apply(state, ev)
            if not isinstance(result, RebuildOk):
                return None
            state = result.state
            copies.append(deepcopy(state))
        copies_of[b] = copies

    def answer(b, t):
        bp = branch_point.get(b)
        if bp is not None and t < bp.tick:
            # the parent's state at that tick, but never beyond the branch point's parentTick
            return answer(bp.payload.parentBranchId, min(t, bp.payload.parentTick))
        line = lines[b]
        n = sum(1 for ev in line if ev.tick <= t)  # events of the line at or below t
        return copies_of[b][n - 1]

    for b in lines:
        for t in range(1, last + 1):
            state = deepcopy(answer(b, t))
            state.position.branchId = b
            state.position.tick = t
            answers[(b, t)] = RebuildOk(status="ok", state=state)
    return answers

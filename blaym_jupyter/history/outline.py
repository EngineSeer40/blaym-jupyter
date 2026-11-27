"""The history outline, made from recorded events.

The outline is the shape of a history without any notebook content: each branch, its
parent and branch point, the ticks of its own events and the runs on it. Navigation uses it to step and place positions; it can always be rebuilt from the events.

It reuses `EventLog` (what is missing, conflicting or clashing) and `Lineage` (each
branch's own ticks and branch point) and never re-derives their rules. Only ticks
below the first missing tick are used. An invalid history gives the outline of what is
valid up to the first problem, with the rebuilder's typed answer beside it.

Run spans: a branch lists the runs started on its own events, plus runs started on an
ancestor that were still open at its branch point (their `startedTick` is on the
parent's part of the line). `endedTick` is the first event on the branch's own line
that closes the run: its `run_finished`, or a `kernel_restarted` (which leaves it
unfinished). A span without `endedTick` is a run still in progress on that line, so a
branch can only start from a settled moment. Runs that had already ended before the
branch point are not listed on the child (they cannot block anything there).
"""

from __future__ import annotations

from dataclasses import dataclass

from blaym_jupyter_types import (
    HistoryOutline,
    OutlineBranch,
    OutlineRunSpan,
    RebuildOk,
)

from .lineage import Lineage
from .log import EventLog
from .rebuild import Rebuilder


@dataclass(frozen=True)
class OutlineResult:
    """`outline` is None when not even tick 1 is usable. `problem` is the rebuilder's
    typed answer for the first problem found (None when the history is sound), so the
    caller can say why the outline stops where it does."""

    outline: HistoryOutline | None
    problem: object | None = None


def _problem_tick(result, by_id: dict[str, int], lin: Lineage) -> int | None:
    """The tick a typed failure points at, or None when it names no event."""
    tick = getattr(result, "tick", None)
    if tick is not None:
        return tick
    event_id = getattr(result, "eventId", None)
    if event_id is not None:
        return by_id.get(event_id)
    branch_ids = getattr(result, "branchIds", None) or [getattr(result, "branchId", None)]
    firsts = [lin.own_ticks[b][0] for b in branch_ids if b in lin.own_ticks]
    return min(firsts) if firsts else None


def _spans(lin: Lineage, branch: str, tip: int) -> list[OutlineRunSpan]:
    line = lin.resolve(branch, tip)
    runs: dict[str, list] = {}  # runId -> [startedTick, endedTick]
    inherited: set[str] = set()
    own = set(lin.own_ticks[branch])
    for name in reversed(line.chain):
        if name == branch:
            inherited = {r for r, (_, end) in runs.items() if end is None}
        for ev in lin.segment(name, line.cutoff[name])[1]:
            if ev.type == "run_started":
                runs[ev.payload.runId] = [ev.tick, None]
            elif ev.type == "run_finished":
                span = runs.get(ev.payload.runId)
                if span is not None and span[1] is None:
                    span[1] = ev.tick
            elif ev.type == "kernel_restarted":
                for span in runs.values():
                    if span[1] is None:
                        span[1] = ev.tick
    keep = [(r, s) for r, s in runs.items() if s[0] in own or r in inherited]
    return [
        OutlineRunSpan(runId=r, startedTick=s[0], endedTick=s[1])
        for r, s in sorted(keep, key=lambda item: item[1][0])
    ]


def build_outline(events) -> OutlineResult:
    """The outline of `events` (any order, duplicates allowed) and the first problem."""
    log = EventLog()
    log.add(events)
    by_tick = dict(log.by_tick)
    by_id = {str(ev.eventId): t for t, ev in by_tick.items()}
    cut = log.first_missing - 1
    problem, problem_tick = None, None

    def note(result, tick: int) -> None:
        nonlocal cut, problem, problem_tick
        cut = min(cut, tick - 1)
        if problem_tick is None or tick < problem_tick:
            problem, problem_tick = result, tick

    if log.conflict is not None:
        # an event that lost a tick clash is not in the tick index: it sits at its own tick,
        # which the clash bounds too
        note(log.conflict, by_id.get(log.conflict.eventId, log.conflict.first.root.tick))
    if log.clash is not None:
        note(log.clash, log.clash.tick)

    while cut >= 1:
        kept = {t: ev for t, ev in by_tick.items() if t <= cut}
        lin = Lineage(kept, cut + 1)
        rebuilder = Rebuilder()
        rebuilder.add([kept[t] for t in sorted(kept)])
        failures = []
        for branch, ticks in lin.own_ticks.items():
            result = rebuilder.state_at(branch, ticks[-1])
            if not isinstance(result, RebuildOk):
                at = _problem_tick(result, by_id, lin)
                failures.append((cut if at is None or at > cut else at, result))
        if not failures:
            return OutlineResult(_assemble(kept, lin, cut), problem)
        tick, result = min(failures, key=lambda f: f[0])
        note(result, max(tick, 1))
    return OutlineResult(None, problem)


def _assemble(kept, lin: Lineage, highest: int) -> HistoryOutline:
    branches = []
    for branch, ticks in lin.own_ticks.items():
        bp = lin.branch_point.get(branch)
        branches.append(
            OutlineBranch(
                branchId=branch,
                name=bp.payload.name if bp is not None else branch,
                parentBranchId=bp.payload.parentBranchId if bp is not None else None,
                parentTick=bp.payload.parentTick if bp is not None else None,
                ownTicks=list(ticks),
                runSpans=_spans(lin, branch, ticks[-1]),
            )
        )
    return HistoryOutline(
        schemaVersion=2,
        explorationId=kept[1].explorationId,
        rootBranchId=lin.root_branch,
        highestTick=highest,
        branches=branches,
    )


def outline(events) -> HistoryOutline | None:
    """Just the outline (None when no event is usable); see `build_outline`."""
    return build_outline(events).outline

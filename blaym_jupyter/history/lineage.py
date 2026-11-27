"""Branch index and line resolution.

Built from the log's events. Follows the same rules as the reference: a branch
point only counts as the first event of its branch; every non-root branch must
start with one; each branch point on a line is checked against its own parent's
line at its own `parentTick`, never with a cut taken from a descendant.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from blaym_jupyter_types import (
    RebuildInvalidEvent,
    RebuildLineageLoop,
    RebuildMissingEvents,
    RebuildUnknownBranch,
)


def invalid_branch_point(ev, detail: str) -> RebuildInvalidEvent:
    return RebuildInvalidEvent(
        status="invalid_event",
        eventId=str(ev.eventId),
        tick=ev.tick,
        reason="invalid_branch_point",
        detail=detail,
    )


@dataclass(frozen=True)
class Line:
    """A resolved line: `chain` is target first, root last; `cutoff` the last tick used per branch."""

    chain: tuple[str, ...]
    cutoff: dict[str, int]


class Lineage:
    def __init__(self, by_tick, first_missing: int) -> None:
        self._by_tick = by_tick
        self._first_missing = first_missing
        self.own: dict[str, list] = {}
        self.own_ticks: dict[str, list[int]] = {}
        self.branch_point: dict[str, object] = {}
        self.stray: dict[str, list[int]] = {}  # branch points that are not their branch's first event
        for t in sorted(by_tick):
            ev = by_tick[t]
            if ev.branchId not in self.own and ev.type == "branch_point_created":
                self.branch_point[ev.branchId] = ev
            elif ev.type == "branch_point_created":
                self.stray.setdefault(ev.branchId, []).append(t)
            self.own.setdefault(ev.branchId, []).append(ev)
            self.own_ticks.setdefault(ev.branchId, []).append(t)
        self.root_branch = by_tick[1].branchId if 1 in by_tick else None

    def resolve(self, branch_id: str, tick: int):
        """A `Line`, or the typed error result for the branch's ancestry."""
        chain: list[str] = []
        b = branch_id
        while True:
            if b in chain:
                return RebuildLineageLoop(status="lineage_loop", branchIds=chain[chain.index(b):])
            if b not in self.own:
                return RebuildUnknownBranch(status="unknown_branch", branchId=b)
            chain.append(b)
            bp = self.branch_point.get(b)
            if bp is None:
                if b != self.root_branch:
                    return invalid_branch_point(
                        self.own[b][0],
                        f"branch {b} does not start with a branch point and is not the root",
                    )
                break
            b = bp.payload.parentBranchId

        bp_of = self.branch_point
        for i, child in enumerate(chain[:-1]):
            bp = bp_of[child]
            pt = bp.payload.parentTick
            if pt >= bp.tick:
                return invalid_branch_point(
                    bp, f"parentTick {pt} is not before the branch point at tick {bp.tick}"
                )
            cap = {chain[i + 1]: pt}
            for c, par in zip(chain[i + 1:], chain[i + 2:]):
                cap[par] = min(cap[c], bp_of[c].payload.parentTick)
            at = self._by_tick.get(pt)
            if at is None:
                # The parent tick is not known (a gap). The reference raises KeyError here
                # (a reference bug, see test_rebuild.py); the typed answer is missing_events.
                return RebuildMissingEvents(
                    status="missing_events", firstMissingTick=self._first_missing
                )
            if at.branchId not in cap or at.tick > cap[at.branchId]:
                return invalid_branch_point(
                    bp, f"parentTick {pt} is not an event on the line of branch {chain[i + 1]}"
                )

        cutoff = {chain[0]: tick}
        for child, parent in zip(chain, chain[1:]):
            cutoff[parent] = min(cutoff[child], bp_of[child].payload.parentTick)
        return Line(chain=tuple(chain), cutoff=cutoff)

    def stray_branch_point(self, line: Line):
        """The first branch point on the line (root first) that is not its branch's first
        event and is on the line, as an error result, else None."""
        for name in reversed(line.chain):
            for t in self.stray.get(name, ()):
                if t <= line.cutoff[name]:
                    ev = self._by_tick[t]
                    return invalid_branch_point(
                        ev,
                        f"branch point at tick {ev.tick} is not the first event of branch {ev.branchId}",
                    )
        return None

    def segment(self, name: str, cutoff: int, after: int = 0):
        """(first own index, events) of `name`'s own events with after < tick <= cutoff."""
        ticks = self.own_ticks[name]
        lo = bisect_right(ticks, after)
        hi = bisect_right(ticks, cutoff)
        return lo, self.own[name][lo:hi]

    def empty(self, line: Line) -> bool:
        return not any(self.segment(n, line.cutoff[n])[1] for n in line.chain)

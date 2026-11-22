"""The real rebuilder (S3a, Task 5).

Gives exactly the reference's answers (`reference_rebuild`), faster: events go into an
append-only log in any order; a jump starts from the nearest cached snapshot on the
line (own branch first, then ancestors up to their cut) and applies the rest with
`step`, the single implementation of event effects, on one private working state.

Rule 7: the answer never depends on cache contents. Snapshots are only a shortcut
and are dropped whenever added events could change them. Snapshots hold their own copies and answers are
independent copies (rule 11).
"""

from __future__ import annotations

from blaym_jupyter_types import RebuildMissingEvents, RebuildOk

from .apply import Failure, step
from .lineage import Lineage
from .log import EventLog
from .reference import ReferenceResult
from .snapshots import SnapshotCache


class Rebuilder:
    def __init__(self, interval: int | None = 200):
        self._log = EventLog()
        self._snapshots = SnapshotCache(interval)
        self._lineage: Lineage | None = None
        self._lineage_version = -1
        self.last_base: tuple[str, int] | None = None  # the snapshot the last jump started from

    @property
    def snapshots(self) -> SnapshotCache:
        return self._snapshots

    def add(self, events) -> None:
        before = (self._log.conflict is not None, self._log.clash is not None)
        lowest = self._log.add(events)
        if lowest is None:
            return
        after = (self._log.conflict is not None, self._log.clash is not None)
        if after != before:
            self._snapshots.clear()
        else:
            self._snapshots.drop_from(lowest)
        self._fill_snapshots()

    def _index(self) -> Lineage:
        if self._lineage is None or self._lineage_version != self._log.version:
            self._lineage = Lineage(self._log.by_tick, self._log.first_missing)
            self._lineage_version = self._log.version
        return self._lineage

    def _fill_snapshots(self) -> None:
        """Take every snapshot the log can currently support (every `interval` own
        events of each branch, below the first missing tick), so a jump never applies
        more than `interval` events. Skipped while a conflict or clash is recorded
        (every answer is that error then). A branch whose line fails is skipped from
        its first failing snapshot on: later snapshots would include the same failure."""
        snaps = self._snapshots
        log = self._log
        if snaps.interval is None or log.conflict is not None or log.clash is not None:
            return
        lin = self._index()
        for name, ticks in lin.own_ticks.items():
            for k in range(snaps.interval, len(ticks) + 1, snaps.interval):
                tick = ticks[k - 1]
                if tick >= log.first_missing:
                    break
                if snaps.has(name, tick):
                    continue
                if not isinstance(self._walk(lin, name, tick, record_base=False), RebuildOk):
                    break

    def state_at(self, branch_id: str, tick: int) -> ReferenceResult:
        self.last_base = None
        log = self._log
        if log.conflict is not None:
            return log.conflict
        if log.clash is not None:
            return log.clash
        if tick >= log.first_missing or tick < 1:
            return RebuildMissingEvents(status="missing_events", firstMissingTick=log.first_missing)
        return self._walk(self._index(), branch_id, tick, record_base=True)

    def _walk(self, lin: Lineage, branch_id: str, tick: int, record_base: bool) -> ReferenceResult:
        line = lin.resolve(branch_id, tick)
        if not hasattr(line, "chain"):
            return line
        if lin.empty(line):
            return RebuildMissingEvents(status="missing_events", firstMissingTick=1)
        stray = lin.stray_branch_point(line)
        if stray is not None:
            return stray

        # Nearest snapshot on the line: own branch first, then ancestors up to their cut.
        # The walk owns one private working state: a copy of the snapshot, changed in
        # place by `step`; it is copied only to store a snapshot, and is the answer.
        state = None
        start = len(line.chain)  # index in chain of the first branch still to walk (walked root first)
        after: dict[str, int] = {}
        for i, name in enumerate(line.chain):
            hit = self._snapshots.latest(name, line.cutoff[name])
            if hit is not None:
                snap_tick, snap = hit
                state = snap.model_copy(deep=True)
                after[name] = snap_tick
                start = i
                if record_base:
                    self.last_base = (name, snap_tick)
                break

        walk = line.chain[: start + 1] if state is not None else line.chain
        try:
            for name in reversed(walk):
                first, events = lin.segment(name, line.cutoff[name], after.get(name, 0))
                for offset, ev in enumerate(events):
                    state = step(state, ev)
                    if self._snapshots.wants(first + offset + 1):
                        self._snapshots.put(name, ev.tick, state.model_copy(deep=True))
        except Failure as f:
            return f.result

        state.position.branchId = branch_id
        state.position.tick = tick
        return RebuildOk(status="ok", state=state)

"""Recovery after a stop or a crash.

Pure rules over the `Service`'s own methods and ports; no SQLite, no files. For every
history, under its lock and through the same commit path as ingest (cached tip state, tick,
rebuilder and notifications all stay right):

 a. leftover blob staging is removed (once, for the whole store);
 b. every stored output that keeps its content in a blob is checked: a missing blob is
    reported with the event's tick and never stops recovery; files with no record and
    records no event references are reported too, and never deleted;
 e. a history whose branches cannot all be rebuilt (a hole, an invalid event) is reported in
    `problems` and left alone: nothing is appended to it (so c and d below do not run for it);
 c. the active branch gets ONE service `recording_gap_started` (reason `server_unavailable`)
    unless one is already open, so restarts with nothing recorded between them do not pile
    up gaps. The next accepted `notebook_snapshot` closes it (the rule in `Service`), not
    any other event;
 d. on EVERY branch, each run still `running` gets a service `run_finished` with status
    `interrupted`, in run start order. A real `run_finished` that the recorder sends later
    is refused as `run_not_running`.

Order in the code: a, then for each history b, e, c, d.

Running it twice in a row changes nothing the second time. Ticks need no rule: the store
allocates MAX+1. The `operations` table is not used by anything in S4, so there is no
operation to complete or roll back; recovery leaves it alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from blaym_jupyter.history.apply import Failure

from .blobrefs import is_inline, items_of


@dataclass(frozen=True)
class RecoveryReport:
    gaps_written: list = field(default_factory=list)  # (exploration_id, branch_id, gap_id)
    runs_closed: list = field(default_factory=list)  # (exploration_id, branch_id, run_id)
    missing_blobs: list = field(default_factory=list)  # (exploration_id, tick, content_hash)
    orphan_blobs: list = field(default_factory=list)  # (exploration_id, content_hash)
    staging_removed: int = 0
    problems: list = field(default_factory=list)  # plain sentences

    @property
    def changed(self) -> bool:
        return bool(self.gaps_written or self.runs_closed or self.staging_removed)


def _is_start_up_gap(g) -> bool:
    return g.source == "service" and g.reason == "server_unavailable" and g.endedTick is None


def recover(service) -> RecoveryReport:
    from .core import _TipFailure  # local: core imports this module's report type lazily

    gaps, runs, missing, orphans, problems = [], [], [], [], []
    staging = service._blobs.cleanup_staging()
    for record in service._events.explorations():
        eid = record.exploration_id
        with service._lock_for(eid):
            events = service._events.events_for(eid)
            referenced: set[str] = set()
            for ev in events:
                for item in items_of(ev):
                    if is_inline(item):
                        continue
                    referenced.add(item.contentHash)
                    if not service._blobs.exists(eid, item.contentHash):
                        missing.append((eid, ev.tick, item.contentHash))
            orphans += [(eid, h) for h in service._blobs.orphans(eid)]
            orphans += [(eid, h) for h in service._blobs.hashes(eid) if h not in referenced]

            exp = service._exp(eid)
            try:
                if exp is None or not exp.branches:
                    raise _TipFailure(None)
                tips = {b: service._tip(exp, b) for b in exp.branches}
                if exp.active not in tips:
                    raise _TipFailure(None)
            except _TipFailure as failure:
                what = getattr(failure.result, "status", "no branches or no active branch")
                problems.append(f"history {eid} cannot be rebuilt ({what}); recovery left it alone")
                continue
            try:
                active = tips[exp.active]
                if not any(_is_start_up_gap(g) for g in active.gaps):
                    gap = service._service_event(exp, eid, exp.active, "recording_gap_started", {
                        "gapId": service._new_id(), "source": "service", "reason": "server_unavailable"})
                    infos = service._dry_run(exp, active, [gap])
                    service._commit(exp, eid, exp.active, [gap], infos)
                    gaps.append((eid, exp.active, gap["payload"]["gapId"]))
                for branch in list(exp.branches):
                    tip = service._tip(exp, branch)
                    open_runs = [r for r in tip.runs if r.status.root == "running"]
                    if not open_runs:
                        continue
                    batch = [service._service_event(exp, eid, branch, "run_finished",
                                                    {"runId": r.runId, "status": "interrupted"}) for r in open_runs]
                    infos = service._dry_run(exp, tip, batch)
                    service._commit(exp, eid, branch, batch, infos)
                    runs += [(eid, branch, r.runId) for r in open_runs]
            except (Failure, _TipFailure) as failure:
                exp.tips.clear()
                problems.append(f"history {eid}: recovery could not append its events ({failure})")
    return RecoveryReport(gaps, runs, missing, orphans, staging, problems)

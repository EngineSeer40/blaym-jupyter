"""Apply one event to one state (S3a, Task 3).

`apply` is pure: the input state is never changed and the returned state shares
nothing with the input state or with the event. `step` is the same effect applied in
place to a state the caller owns (the rebuilder's working copy); `apply` is
copy + `step`, so there is one implementation of event effects. Models come from `blaym_jupyter_types`;
none are defined here.

Readings of the contract made here (each is documented where it applies):

* `outputs_cleared` with `wait=True` sets `clearPending` on the run's output group
  (created empty if the run has none yet) and keeps the items: Jupyter keeps showing
  the old output. The next `output_added` for that run replaces the list with the new
  item and clears the flag. Without the flag, `output_added` inserts at `outputIndex`
  (an index beyond the end is `index_out_of_range`).
* `outputs_cleared` with `wait=False` empties the run's output list (the group stays)
  and clears any pending flag.
* `notebook_copied` changes nothing in this history's notebook (a copy starts a
  new history); only the position moves.
* A snapshot replaces the notebook, keeps runs, chat and gaps, and sets the
  kernel name and language.
* `changed_outside_editor` replaces the notebook and sets `lastOutsideChange`.
* `output_added`, `output_replaced` and `outputs_cleared` for a run that never started
  are `invalid_event` / `unknown_run`.
* `position.tick` is the tick asked for; here it equals the event's tick, and
  `lastEventTick` is the event's tick (the reference sets `tick` to the asked one).
* Outputs follow the run: an output event addresses its run. If the run's cell exists
  the group lives on the cell; if the cell was deleted it lives in `detachedOutputs`
  (running or finished run alike). `cell_deleted` moves the groups of still-running
  runs there; groups of finished runs go with the cell.
* An output event whose `cellId` differs from the run's cell is `invalid_event` /
  `run_cell_mismatch`; it is checked after `unknown_cell` / `unknown_run` (a run that
  never started) and before anything is applied.
* A `notebook_snapshot` or `changed_outside_editor` that drops a cell moves that
  cell's groups of still-running runs to `detachedOutputs`, as `cell_deleted` does.
* `kernel_restarted` marks every `running` run `unfinished` (no `endedTick`).

Events that cannot apply return a typed result (`unknown_cell`, `invalid_revision`
or `invalid_event` with a reason); no exception escapes for an event that validates
against the contract.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from blaym_jupyter_types import (
    BlaymEvent,
    ChatMessageState,
    GapState,
    KernelRestartMarker,
    KernelState,
    KernelStatus,
    NotebookCell,
    NotebookState,
    OutputItem,
    OutsideChangeMarker,
    RebuildInvalidEvent,
    RebuildInvalidRevision,
    RebuildOk,
    RebuildUnknownCell,
    RebuiltState,
    RunOutputs,
    RunState,
    StatePosition,
)

ApplyResult = RebuildOk | RebuildInvalidRevision | RebuildUnknownCell | RebuildInvalidEvent


class Failure(Exception):
    def __init__(self, result: RebuildInvalidRevision | RebuildUnknownCell | RebuildInvalidEvent):
        self.result = result


def _bad(ev, reason: str, detail: str) -> Failure:
    return Failure(
        RebuildInvalidEvent(
            status="invalid_event",
            eventId=str(ev.eventId),
            tick=ev.tick,
            reason=reason,
            detail=detail,
        )
    )


def unwrap(event):
    """Accept either the `BlaymEvent` root model or the concrete event model."""
    return event.root if isinstance(event, BlaymEvent) else event


def _cell_index(state: RebuiltState, cell_id: str, ev) -> int:
    for i, cell in enumerate(state.notebook.cells):
        if cell.cellId == cell_id:
            return i
    raise Failure(
        RebuildUnknownCell(status="unknown_cell", cellId=cell_id, eventId=str(ev.eventId))
    )


def _initial(ev) -> RebuiltState:
    return RebuiltState(
        schemaVersion=2,
        position=StatePosition(branchId=ev.branchId, tick=ev.tick, lastEventTick=ev.tick, branchCount=1),
        notebook=NotebookState(path="", metadata={}, cells=[]),
        runs=[],
        chat=[],
        kernel=KernelState(status=KernelStatus.unknown, kernelName="", language=""),
        gaps=[],
        detachedOutputs=[],
    )


# --- handlers: each mutates the (already copied) state --------------------------


def _replace_notebook(s: RebuiltState, ev) -> None:
    p = ev.payload
    # Like `cell_deleted`: a cell the new notebook drops keeps the output groups of its
    # still-running runs (outputs follow the run); other groups go with the cell.
    kept = {c.cellId for c in p.cells}
    running = {r.runId for r in s.runs if r.status.root == "running"}
    for old in s.notebook.cells:
        if old.cellId not in kept:
            s.detachedOutputs.extend(g for g in old.outputs if g.runId in running)
    s.notebook = NotebookState(
        path=p.path,
        metadata=deepcopy(p.notebookMetadata),
        cells=[c.model_copy(deep=True) for c in p.cells],
    )
    s.kernel.kernelName = p.kernelName
    s.kernel.language = p.language


def _snapshot(s, ev):
    _replace_notebook(s, ev)


def _outside(s, ev):
    _replace_notebook(s, ev)
    s.lastOutsideChange = OutsideChangeMarker(tick=ev.tick)


def _cell_inserted(s, ev):
    p = ev.payload
    if any(c.cellId == p.cellId for c in s.notebook.cells):
        raise _bad(ev, "duplicate_id", f"cell {p.cellId} inserted twice")
    if p.index > len(s.notebook.cells):
        raise _bad(ev, "index_out_of_range", f"insert index {p.index} beyond the end")
    s.notebook.cells.insert(
        p.index,
        NotebookCell(
            cellId=p.cellId,
            cellType=p.cellType,
            code=p.code,
            revision=p.revision,
            metadata=deepcopy(p.metadata) if p.metadata is not None else {},
            # Pasted outputs belong to no run: one group named after this event.
            outputs=(
                [RunOutputs(runId=f"pasted:{ev.eventId}", items=[i.model_copy(deep=True) for i in p.outputs])]
                if p.outputs
                else []
            ),
        ),
    )


def _cell_deleted(s, ev):
    cell = s.notebook.cells.pop(_cell_index(s, ev.payload.cellId, ev))
    # Outputs follow the run: a run still in progress keeps receiving output from
    # the kernel, so its group is kept in `detachedOutputs`. Groups of finished runs
    # go with the cell (they stay in history at earlier positions).
    running = {r.runId for r in s.runs if r.status.root == "running"}
    s.detachedOutputs.extend(g for g in cell.outputs if g.runId in running)


def _cell_moved(s, ev):
    p = ev.payload
    i = _cell_index(s, p.cellId, ev)
    if i != p.fromIndex:
        raise _bad(ev, "position_mismatch", f"cell {p.cellId} is at {i}, not fromIndex {p.fromIndex}")
    cell = s.notebook.cells.pop(i)
    if p.toIndex > len(s.notebook.cells):
        raise _bad(ev, "index_out_of_range", f"toIndex {p.toIndex} beyond the end")
    # toIndex is the cell's index in the list after the move.
    s.notebook.cells.insert(p.toIndex, cell)


def _cell_type_changed(s, ev):
    s.notebook.cells[_cell_index(s, ev.payload.cellId, ev)].cellType = ev.payload.cellType


def _cell_source_changed(s, ev):
    p = ev.payload
    cell = s.notebook.cells[_cell_index(s, p.cellId, ev)]
    if p.revision != cell.revision + 1:
        raise Failure(
            RebuildInvalidRevision(
                status="invalid_revision",
                cellId=p.cellId,
                eventId=str(ev.eventId),
                expectedRevision=cell.revision + 1,
                actualRevision=p.revision,
            )
        )
    cell.code = p.code
    cell.revision = p.revision


def _cell_metadata_changed(s, ev):
    s.notebook.cells[_cell_index(s, ev.payload.cellId, ev)].metadata = deepcopy(ev.payload.metadata)


def _notebook_metadata_changed(s, ev):
    s.notebook.metadata = deepcopy(ev.payload.metadata)


def _run_started(s, ev):
    p = ev.payload
    cell = s.notebook.cells[_cell_index(s, p.cellId, ev)]
    if any(r.runId == p.runId for r in s.runs):
        raise _bad(ev, "duplicate_id", f"run {p.runId} started twice")
    cell.runCount = p.runCount
    s.runs.append(
        RunState(
            runId=p.runId,
            cellId=p.cellId,
            cellRevision=p.cellRevision,
            runCount=p.runCount,
            status="running",
            startedTick=ev.tick,
        )
    )


def _run_finished(s, ev):
    p = ev.payload
    for r in s.runs:
        if r.runId == p.runId:
            if r.status.root != "running":
                raise _bad(ev, "run_not_running", f"run {p.runId} is already {r.status.root}")
            r.status = type(r.status)(p.status)
            r.endedTick = ev.tick
            return
    raise _bad(ev, "unknown_run", f"run_finished for unknown run {p.runId}")


def _group(groups: list[RunOutputs], run_id: str, create: bool) -> RunOutputs | None:
    for g in groups:
        if g.runId == run_id:
            return g
    if not create:
        return None
    g = RunOutputs(runId=run_id, items=[])
    groups.append(g)
    return g


def _run_groups(s, ev) -> list[RunOutputs]:
    """The output groups an output event addresses (ruling A, and "outputs follow the
    run"). A run that never started is `unknown_cell` when the event's cell does not
    exist, else `unknown_run` (S5 records a run's start before its outputs, so this
    means missing data). For a started run the groups are its cell's when the cell
    exists, else `detachedOutputs`: output arriving after the cell was deleted is kept,
    whether the run is still running or has finished (the run still addresses it)."""
    p = ev.payload
    run = next((r for r in s.runs if r.runId == p.runId), None)
    if run is None:
        _cell_index(s, p.cellId, ev)
        raise _bad(ev, "unknown_run", f"{ev.type} for run {p.runId} that never started")
    if run.cellId != p.cellId:
        raise _bad(ev, "run_cell_mismatch",
                   f"{ev.type} names cell {p.cellId} but run {p.runId} belongs to cell {run.cellId}")
    for c in s.notebook.cells:
        if c.cellId == run.cellId:
            return c.outputs
    return s.detachedOutputs


def _output_added(s, ev):
    p = ev.payload
    groups = _run_groups(s, ev)
    g = _group(groups, p.runId, create=True)
    item: OutputItem = p.item.model_copy(deep=True)
    if g.clearPending:
        g.items = [item]  # ends a "clear and wait"
        g.clearPending = None
        return
    if p.outputIndex > len(g.items):
        raise _bad(ev, "index_out_of_range", f"output index {p.outputIndex} beyond the end of run {p.runId}")
    g.items.insert(p.outputIndex, item)


def _output_replaced(s, ev):
    p = ev.payload
    g = _group(_run_groups(s, ev), p.runId, create=False)
    if g is None or p.outputIndex >= len(g.items):
        raise _bad(ev, "no_output_at_index", f"output_replaced at {p.outputIndex} of run {p.runId} has nothing to replace")
    g.items[p.outputIndex] = p.item.model_copy(deep=True)


def _outputs_cleared(s, ev):
    p = ev.payload
    groups = _run_groups(s, ev)
    if p.wait:
        _group(groups, p.runId, create=True).clearPending = True
        return
    g = _group(groups, p.runId, create=False)
    if g is not None:
        g.items = []
        g.clearPending = None


def _kernel_started(s, ev):
    p = ev.payload
    s.kernel.status = KernelStatus.running
    s.kernel.kernelId = p.kernelId
    s.kernel.kernelName = p.kernelName
    s.kernel.language = p.language


def _kernel_restarted(s, ev):
    p = ev.payload
    s.kernel.status = KernelStatus.running
    s.kernel.kernelId = p.kernelId
    s.kernel.lastRestart = KernelRestartMarker(kernelId=p.kernelId, reason=p.reason, tick=ev.tick)
    for r in s.runs:
        if r.status.root == "running":
            r.status = type(r.status)("unfinished")


def _renamed(s, ev):
    s.notebook.path = ev.payload.newPath


def _copied(s, ev):
    return None


def _chat_added(s, ev):
    p = ev.payload
    if any(m.chatSource == p.chatSource and m.messageId == p.messageId for m in s.chat):
        raise _bad(ev, "duplicate_id", f"chat message {p.chatSource}/{p.messageId} added twice")
    s.chat.append(
        ChatMessageState(
            chatSource=p.chatSource,
            messageId=p.messageId,
            sender=p.sender,
            senderKind=p.senderKind,
            body=p.body,
            chatTimeMs=p.chatTimeMs,
            agentSessionId=p.agentSessionId,
            addedTick=ev.tick,
        )
    )


def _chat_updated(s, ev):
    p = ev.payload
    for m in s.chat:
        if m.chatSource == p.chatSource and m.messageId == p.messageId:
            m.body = p.body
            return
    raise _bad(ev, "unknown_message", f"chat_message_updated for unknown message {p.chatSource}/{p.messageId}")


def _branch_point(s, ev):
    return None  # the position moves to the new branch; the state is the parent's


def _gap_started(s, ev):
    p = ev.payload
    if any(g.gapId == p.gapId for g in s.gaps):
        raise _bad(ev, "duplicate_id", f"gap {p.gapId} started twice")
    s.gaps.append(GapState(gapId=p.gapId, source=p.source, reason=p.reason, startedTick=ev.tick))


def _gap_ended(s, ev):
    for g in s.gaps:
        if g.gapId == ev.payload.gapId:
            if g.endedTick is not None:
                raise _bad(ev, "gap_already_ended", f"gap {g.gapId} ended twice")
            g.endedTick = ev.tick
            return
    raise _bad(ev, "unknown_gap", f"recording_gap_ended for unknown gap {ev.payload.gapId}")


_HANDLERS: dict[str, Callable] = {
    "notebook_snapshot": _snapshot,
    "cell_inserted": _cell_inserted,
    "cell_deleted": _cell_deleted,
    "cell_moved": _cell_moved,
    "cell_type_changed": _cell_type_changed,
    "cell_source_changed": _cell_source_changed,
    "cell_metadata_changed": _cell_metadata_changed,
    "notebook_metadata_changed": _notebook_metadata_changed,
    "run_started": _run_started,
    "run_finished": _run_finished,
    "output_added": _output_added,
    "output_replaced": _output_replaced,
    "outputs_cleared": _outputs_cleared,
    "kernel_started": _kernel_started,
    "kernel_restarted": _kernel_restarted,
    "notebook_renamed": _renamed,
    "notebook_copied": _copied,
    "changed_outside_editor": _outside,
    "chat_message_added": _chat_added,
    "chat_message_updated": _chat_updated,
    "branch_point_created": _branch_point,
    "recording_gap_started": _gap_started,
    "recording_gap_ended": _gap_ended,
}


def step(state: RebuiltState | None, event) -> RebuiltState:
    """Apply `event` to `state` IN PLACE (None before the first event) and return the
    state. The one implementation of event effects. The caller owns `state` and must
    discard it if this raises `Failure` (a failed event may leave it half changed)."""
    ev = unwrap(event)
    if state is None:
        if ev.type != "notebook_snapshot":
            raise _bad(ev, "first_event_not_snapshot", f"the first event must be a notebook_snapshot, not {ev.type}")
        state = _initial(ev)
        count = 1
    else:
        count = state.position.branchCount + 1
    _HANDLERS[ev.type](state, ev)
    pos = state.position
    pos.branchId = ev.branchId
    pos.tick = ev.tick
    pos.lastEventTick = ev.tick
    pos.branchCount = count
    return state


def apply(state: RebuiltState | None, event) -> ApplyResult:
    """Apply `event` to `state` (None before the first event). Pure: copies, then steps."""
    try:
        new = step(None if state is None else state.model_copy(deep=True), event)
    except Failure as f:
        return f.result
    return RebuildOk(status="ok", state=new)

"""Sequence generator and delivery problems for the history tests.

Three hypothesis strategies:

* `histories(...)`: valid histories built from random user actions. Every step is
  chosen from what the current state allows and checked with `apply`, so the
  reference rebuilder answers `ok` at every position on every branch.
* `deliveries(history, ...)`: a valid history as it might arrive: duplicated,
  reordered, with dropped events, a same-id-different-content conflict, a tick
  clash, an event naming an unknown branch. Every mutation is labelled so a
  property can state the expected answer.
* `invalid_histories(...)`: deliberately impossible histories, one per reason, with
  the typed answer the reference must give.

Events are built through `Scenario` (no event construction is duplicated here and
no models are defined here).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from hypothesis import strategies as st

from blaym_notebook.history.apply import apply
from blaym_notebook.tests.history.scenario import Scenario

# The 24 event types of the contract (used by the generator self-tests).
ALL_EVENT_TYPES = (
    "notebook_snapshot", "cell_inserted", "cell_deleted", "cell_moved", "cell_type_changed",
    "cell_outputs_cleared",
    "cell_source_changed", "cell_metadata_changed", "notebook_metadata_changed",
    "run_started", "run_finished", "output_added", "output_replaced", "outputs_cleared",
    "kernel_started", "kernel_restarted", "notebook_renamed", "notebook_copied",
    "changed_outside_editor", "chat_message_added", "chat_message_updated",
    "branch_point_created", "recording_gap_started", "recording_gap_ended",
)

SHAPES = ("none", "one", "nested", "siblings", "before_parent", "first", "mixed")

INVALID_KINDS = (
    "first_event_not_snapshot", "tick_clash", "duplicate_id_cell", "duplicate_id_run",
    "duplicate_id_chat", "duplicate_id_gap", "unknown_run_output", "unknown_run_finish",
    "unknown_run_clear", "unknown_run_replace", "run_not_running", "unknown_message",
    "unknown_gap", "gap_already_ended", "no_output_at_index", "index_out_of_range_insert",
    "index_out_of_range_output", "index_out_of_range_move", "position_mismatch",
    "invalid_branch_point_future", "invalid_branch_point_sibling_line",
    "invalid_branch_point_stray_branch", "run_cell_mismatch_output", "run_cell_mismatch_replace",
    "run_cell_mismatch_clear", "unknown_cell_edit", "unknown_cell_delete",
    "unknown_cell_run", "invalid_revision",
)

MUTATIONS = ("duplicate", "reorder", "drop", "conflict", "tick_clash", "unknown_branch",
             "orphan_parent")


@dataclass(frozen=True)
class History:
    events: tuple  # valid, in tick order
    branches: tuple[str, ...]
    shape: str
    tags: frozenset[str] = frozenset()

    @property
    def last_tick(self) -> int:
        return self.events[-1].tick


@dataclass(frozen=True)
class Delivery:
    history: History
    events: tuple  # as delivered
    labels: tuple[str, ...]
    dropped_ticks: tuple[int, ...] = ()
    conflict_event_ids: tuple[str, ...] = ()
    clash_ticks: tuple[int, ...] = ()
    unknown_branch: str | None = None  # a branch id that is not in the history
    orphan_branches: tuple[str, ...] = ()  # branches whose branch point names a missing parent

    @property
    def first_missing_tick(self) -> int | None:
        return min(self.dropped_ticks) if self.dropped_ticks else None


@dataclass(frozen=True)
class InvalidHistory:
    events: tuple
    kind: str
    expected_status: str  # invalid_event, unknown_cell or invalid_revision
    expected_reason: str | None  # InvalidEventReason for invalid_event
    branch: str  # ask here ...
    tick: int  # ... at this tick
    event_id: str


# --- the builder ----------------------------------------------------------------


class _Builder:
    """Builds a valid history step by step, tracking the state of every branch line."""

    def __init__(self, shape: str, service_safe: bool = False):
        self.shape = shape
        self.service_safe = service_safe
        self.s = Scenario()
        self.lines: dict[str, list] = {}  # branch -> [(tick, state)] along its whole line
        self.bp_tick: dict[str, int] = {}
        self.order: list[str] = [self.s.root]
        self.tags: set[str] = set()
        self.counter = 0
        self.max_branches = {"none": 0, "one": 1, "nested": 3, "siblings": 3,
                             "before_parent": 3, "first": 2, "mixed": 4}[shape]

    # -- plumbing

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    @property
    def cur(self) -> str:
        return self.s.current

    @property
    def state(self):
        return self.lines[self.cur][-1][1]

    def track(self):
        """Apply the event the scenario just emitted to its branch line."""
        ev = self.s.events[-1]
        prev = self.lines[self.cur][-1][1] if self.cur in self.lines else None
        res = apply(prev, ev)
        assert res.status == "ok", f"generator built an invalid step {ev.type}: {res}"
        self.lines.setdefault(self.cur, []).append((ev.tick, res.state))
        return ev

    def create_branch(self, parent: str, tick: int, name: str):
        base = [e for e in self.lines[parent] if e[0] <= tick]
        parent_state = base[-1][1]
        self.s.branch_from(tick=tick, name=name, parent=parent)
        ev = self.s.events[-1]
        res = apply(parent_state, ev)
        assert res.status == "ok", res
        self.lines[name] = base + [(ev.tick, res.state)]
        self.bp_tick[name] = tick
        self.order.append(name)

    def cells(self):
        return list(self.state.notebook.cells)

    def runs_with_cell(self):
        ids = {c.cellId for c in self.state.notebook.cells}
        return [r for r in self.state.runs if r.cellId in ids]

    def group(self, run):
        """The run's output group: detached if it ever moved there, else on its cell
        (outputs follow the run)."""
        if any(g.runId == run.runId for g in self.state.detachedOutputs):
            groups = self.state.detachedOutputs
        else:
            groups = next((c.outputs for c in self.state.notebook.cells if c.cellId == run.cellId),
                          self.state.detachedOutputs)
        return next((g for g in groups if g.runId == run.runId), None)

    def cell_gone(self, run) -> bool:
        return all(c.cellId != run.cellId for c in self.state.notebook.cells)


def _code(draw) -> str:
    return draw(st.text(alphabet="xyz =+1\n", max_size=10))


def _applicable(b: _Builder, step: int) -> list[str]:
    st_ = b.state
    cells, runs = st_.notebook.cells, st_.runs
    running = [r for r in runs if r.status.root == "running"]
    with_cell = b.runs_with_cell()
    names = ["insert", "insert", "chat", "kernel_start", "gap_start", "rename", "copy",
             "nb_meta", "outside"]
    if cells:
        names += ["delete", "move", "move", "type", "user_clear", "edit", "edit", "edit", "cell_meta",
                  "run", "run", "restart"]
    else:
        names += ["restart"]
    if running:
        names += ["finish", "finish", "restart_running"]
        if any(r in with_cell for r in running):
            names += ["edit_running", "edit_running"]
    if runs:
        names += ["output", "output", "output", "clear", "clear_wait"]
    if any((g := b.group(r)) and g.items for r in runs):
        names += ["replace", "replace"]
    if st_.chat:
        names += ["chat_update", "chat_update"]
    if any(g.endedTick is None for g in st_.gaps):
        names += ["gap_end", "gap_end", "recover"]
    if b.service_safe and running:  # the service refuses an outside change during a run
        names = [n for n in names if n != "outside"]
    if len(b.order) > 1:
        names += ["switch", "switch"]
    if len(b.order) - 1 < b.max_branches and step >= 1:
        names += ["branch"] * 3
    return names


def _tag_gone(b, run) -> None:
    """Output for a run whose cell was deleted (running or finished) stays on the run."""
    if b.cell_gone(run):
        b.tags.add("output_for_deleted_cell" if run.status.root != "running"
                   else "output_for_deleted_running_cell")


def _surviving(b, draw, cells):
    """The cells a snapshot or outside change keeps; sometimes it drops one, which
    detaches the output of a run still in progress on it."""
    out = []
    for c in cells:
        if draw(st.sampled_from(["keep", "keep", "keep", "drop"])) == "drop":
            if any(r.cellId == c.cellId and r.status.root == "running" for r in b.state.runs):
                b.tags.add("snapshot_drops_running_cell")
            continue
        out.append(c)
    return out


def _pick_cell(b, draw):
    return draw(st.sampled_from(b.cells()))


def _act(b: _Builder, draw, name: str) -> None:
    s = b.s
    state = b.state
    if name == "insert":
        n = len(state.notebook.cells)
        cell_type = draw(st.sampled_from(["code", "code", "markdown", "raw"]))
        s.insert(b.fresh("c"), _code(draw), index=draw(st.integers(0, n)), cell_type=cell_type)
    elif name == "delete":
        s.delete(_pick_cell(b, draw).cellId)
    elif name == "move":
        cell = _pick_cell(b, draw)
        i = [c.cellId for c in state.notebook.cells].index(cell.cellId)
        s.move(cell.cellId, i, draw(st.integers(0, len(state.notebook.cells) - 1)))
    elif name == "type":
        s.change_type(_pick_cell(b, draw).cellId, draw(st.sampled_from(["code", "markdown", "raw"])))
    elif name == "user_clear":
        s.clear_cell_outputs(_pick_cell(b, draw).cellId)
    elif name == "edit":
        cell = _pick_cell(b, draw)
        s.edit(cell.cellId, _code(draw), revision=cell.revision + 1)
    elif name == "edit_running":
        run = draw(st.sampled_from([r for r in b.runs_with_cell() if r.status.root == "running"]))
        cell = next(c for c in state.notebook.cells if c.cellId == run.cellId)
        s.edit(cell.cellId, _code(draw), revision=cell.revision + 1)
        b.tags.add("edit_while_running")
    elif name == "cell_meta":
        s.cell_metadata(_pick_cell(b, draw).cellId, {"k": draw(st.integers(0, 5))})
    elif name == "nb_meta":
        s.notebook_metadata({"nb": draw(st.integers(0, 5))})
    elif name == "run":
        cell = _pick_cell(b, draw)
        s.run(cell.cellId, revision=cell.revision)
    elif name == "finish":
        running = [r for r in state.runs if r.status.root == "running"]
        s.finish(draw(st.sampled_from(running)).runId,
                 draw(st.sampled_from(["ok", "ok", "error", "interrupted"])))
    elif name == "output":
        run = draw(st.sampled_from(state.runs))
        _tag_gone(b, run)
        g = b.group(run)
        index = 0 if g is None or g.clearPending else draw(st.integers(0, len(g.items)))
        kind = draw(st.sampled_from(["stream", "display_data", "execute_result", "error"]))
        if g is not None and g.clearPending:
            b.tags.add("output_after_clear_wait")
        s.output(run.runId, _code(draw), index=index, kind=kind)
    elif name == "replace":
        cands = [r for r in state.runs if (g := b.group(r)) and g.items]
        run = draw(st.sampled_from(cands))
        _tag_gone(b, run)
        s.replace_output(run.runId, _code(draw), index=draw(st.integers(0, len(b.group(run).items) - 1)))
    elif name in ("clear", "clear_wait"):
        run = draw(st.sampled_from(state.runs))
        _tag_gone(b, run)
        s.clear_outputs(run.runId, wait=name == "clear_wait")
        b.tags.add("clear_wait" if name == "clear_wait" else "clear_nowait")
    elif name == "kernel_start":
        s.kernel_start(b.fresh("k"))
    elif name in ("restart", "restart_running"):
        if any(r.status.root == "running" for r in state.runs):
            b.tags.add("restart_with_unfinished_run")
        s.kernel_restart(b.fresh("k"), reason=draw(st.sampled_from(["requested", "crash", "unknown"])))
    elif name == "rename":
        s.rename(state.notebook.path, draw(st.sampled_from(["a.ipynb", "b.ipynb", "d/c.ipynb"])))
    elif name == "copy":
        s.copy(state.notebook.path, draw(st.sampled_from(["copy.ipynb", "d/copy.ipynb"])))
    elif name == "outside":
        keep = [(c.cellId, c.code + draw(st.sampled_from(["", "", "#"])))
                for c in _surviving(b, draw, state.notebook.cells)]
        if draw(st.booleans()):
            keep.append((b.fresh("c"), _code(draw)))
        s.outside_change(keep, path=state.notebook.path)
    elif name == "chat":
        s.chat(b.fresh("m"), _code(draw), sender=draw(st.sampled_from(["u1", "ai"])),
               kind=draw(st.sampled_from(["person", "agent"])),
               chat_time_ms=draw(st.integers(0, 10**6)),
               source=draw(st.sampled_from(["jupyter-ai", "vscode-copilot"])))
    elif name == "chat_update":
        msg = draw(st.sampled_from(state.chat))
        s.chat_update(msg.messageId, source=msg.chatSource, body=msg.body + draw(st.sampled_from(["a", " more", "\n"])))
        b.tags.add("streamed_chat_update")
    elif name == "gap_start":
        s.gap_start(b.fresh("g"), source=draw(st.sampled_from(["browser", "kernel", "chat", "files", "service"])),
                    reason=draw(st.sampled_from(["no_client", "kernel_restart", "server_unavailable",
                                                 "source_closed", "unknown"])))
    elif name == "gap_end":
        s.gap_end(draw(st.sampled_from([g for g in state.gaps if g.endedTick is None])).gapId)
    elif name == "recover":
        keep = [(c.cellId, c.code) for c in _surviving(b, draw, state.notebook.cells)]
        if draw(st.booleans()):
            keep.append((b.fresh("c"), _code(draw)))
        s.snapshot(keep, path=state.notebook.path)
        b.tags.add("gap_recovery_snapshot")
    elif name == "switch":
        s.on(draw(st.sampled_from([x for x in b.order if x != b.cur])))
        return
    elif name == "branch":
        _branch(b, draw)
        return
    else:  # pragma: no cover
        raise AssertionError(name)
    b.track()


def _branch(b: _Builder, draw) -> None:
    others = [x for x in b.order]
    made = len(b.order) - 1
    shape = b.shape
    if shape == "nested":
        parent = b.order[-1]
    elif shape == "siblings":
        parent = b.s.root
    elif shape == "before_parent" and made >= 1:
        parent = b.order[-1]
    elif shape == "first":
        parent = draw(st.sampled_from(others))
    else:
        parent = draw(st.sampled_from(others))
    line = b.lines[parent]
    if shape == "first":
        tick = line[0][0]
        b.tags.add("branch_from_first_event")
    elif shape == "before_parent" and parent in b.bp_tick:
        early = [t for t, _ in line if t <= b.bp_tick[parent]]
        tick = draw(st.sampled_from(early))
        b.tags.add("branch_before_parent_existed")
    else:
        tick = draw(st.sampled_from([t for t, _ in line]))
    if parent in b.bp_tick and tick < b.bp_tick[parent]:
        b.tags.add("branch_before_parent_existed")
    if tick == line[-1][0]:
        b.tags.add("branch_from_parent_latest")
    depth, p = 0, parent
    while p != b.s.root:
        depth, p = depth + 1, b.s._parent[p][0]
    if depth >= 1:
        b.tags.add("nested_branch")
    if depth >= 2:
        b.tags.add("nested_3_levels")  # the new branch is the third level
    siblings = [x for x in b.order if x != b.s.root and b.s._parent[x][0] == parent]
    if siblings:
        b.tags.add("sibling_branches")
    b.create_branch(parent, tick, b.fresh("b"))


def _build(draw, shape: str, n: int, service_safe: bool = False) -> _Builder:
    b = _Builder(shape, service_safe)
    cells = [(f"s{i}", _code(draw)) for i in range(draw(st.integers(0, 3)))]
    b.s.start(cells=cells)
    b.track()
    for step in range(n):
        _act(b, draw, draw(st.sampled_from(_applicable(b, step))))
    return b


def _finish(b: _Builder) -> History:
    return History(events=tuple(b.s.events), branches=tuple(b.order), shape=b.shape,
                   tags=frozenset(b.tags))


@st.composite
def histories(draw, shapes=SHAPES, max_events: int = 80, service_safe: bool = False) -> History:
    """A valid history: the reference answers `ok` at every tick on every branch.
    `service_safe` (opt-in) never emits an outside change while a run is
    running on the current branch: the service refuses those; the rebuilder accepts them."""
    shape = draw(st.sampled_from(shapes))
    bands = [st.integers(0, min(8, max_events))]
    if max_events > 9:
        bands.append(st.integers(9, min(30, max_events)))
    if max_events > 31:
        bands.append(st.integers(31, max_events))
    b = _build(draw, shape, draw(st.one_of(*bands)), service_safe)
    return _finish(b)


# --- delivery problems ----------------------------------------------------------


@st.composite
def deliveries(draw, history: History | None = None, mutations=MUTATIONS) -> Delivery:
    """A valid history as delivered, with any of the delivery problems and a label for each."""
    h = draw(histories()) if history is None else history
    chosen = set(draw(st.lists(st.sampled_from(mutations), unique=True, max_size=len(mutations))))
    events = list(h.events)
    labels: list[str] = []
    dropped: list[int] = []
    conflicts: list[str] = []
    clashes: list[int] = []
    orphans: list[str] = []
    unknown = None

    if "drop" in chosen and len(events) > 1:
        idx = sorted(draw(st.lists(st.integers(0, len(events) - 1), min_size=1,
                                   max_size=min(3, len(events) - 1), unique=True)))
        dropped = [events[i].tick for i in idx]
        events = [e for i, e in enumerate(events) if i not in set(idx)]
        labels.append("drop:" + ",".join(map(str, dropped)))

    if "orphan_parent" in chosen:
        bps = [i for i, e in enumerate(events) if e.type == "branch_point_created"]
        if bps:
            i = draw(st.sampled_from(bps))
            e = events[i]
            events[i] = e.model_copy(update={"payload": e.payload.model_copy(
                update={"parentBranchId": "no-such-parent"})})
            orphans.append(e.branchId)
            labels.append(f"orphan_parent:{e.branchId}")

    if "conflict" in chosen and events:
        e = draw(st.sampled_from(events))
        events.insert(draw(st.integers(0, len(events))),
                      e.model_copy(update={"wallTimeMs": e.wallTimeMs + 1}))
        conflicts.append(str(e.eventId))
        labels.append(f"conflict:{e.eventId}")

    if "tick_clash" in chosen and events:
        e = draw(st.sampled_from(events))
        twin = e.model_copy(update={"eventId": uuid.uuid5(uuid.NAMESPACE_URL, f"clash:{e.eventId}")})
        events.insert(draw(st.integers(0, len(events))), twin)
        clashes.append(e.tick)
        labels.append(f"tick_clash:{e.tick}")

    if "unknown_branch" in chosen:
        unknown = "ghost-branch"
        labels.append(f"unknown_branch:{unknown}")

    if "duplicate" in chosen and events:
        pairs = draw(st.lists(st.tuples(st.integers(0, len(events) - 1), st.integers(0, len(events))),
                              min_size=1, max_size=5))
        src = list(events)
        for i, pos in pairs:
            events.insert(min(pos, len(events)), src[i])
        labels.append(f"duplicate:{len(pairs)}")

    if "reorder" in chosen and len(events) > 1:
        events = list(draw(st.permutations(events)))
        labels.append("reorder")

    return Delivery(history=h, events=tuple(events), labels=tuple(labels),
                    dropped_ticks=tuple(dropped), conflict_event_ids=tuple(conflicts),
                    clash_ticks=tuple(clashes), unknown_branch=unknown,
                    orphan_branches=tuple(orphans))


# --- deliberately invalid histories ---------------------------------------------


def _ensure_cell(b: _Builder) -> str:
    cells = b.cells()
    if cells:
        return cells[0].cellId
    cid = b.fresh("c")
    b.s.insert(cid, "x", index=0)
    b.track()
    return cid


def _ensure_run(b: _Builder):
    cid = _ensure_cell(b)
    cell = next(c for c in b.cells() if c.cellId == cid)
    run_id = b.s.run(cid, revision=cell.revision)
    b.track()
    return cid, run_id


@st.composite
def invalid_histories(draw, kinds=INVALID_KINDS) -> InvalidHistory:
    """A short valid prefix plus one deliberately impossible step of the chosen kind."""
    kind = draw(st.sampled_from(kinds))
    if kind == "first_event_not_snapshot":
        s = Scenario()
        first = draw(st.sampled_from(["insert", "chat", "kernel", "gap"]))
        if first == "insert":
            s.insert("c1", "x")
        elif first == "chat":
            s.chat("m1", "hi")
        elif first == "kernel":
            s.kernel_start()
        else:
            s.gap_start()
        for i in range(draw(st.integers(0, 3))):
            s.chat(f"m{i + 2}", "more")
        return InvalidHistory(tuple(s.events), kind, "invalid_event", "first_event_not_snapshot",
                              s.root, 1, str(s.events[0].eventId))

    b = _build(draw, draw(st.sampled_from(["none", "mixed"])), draw(st.integers(0, 15)))
    s = b.s
    reason = None
    status = "invalid_event"

    if kind == "tick_clash":
        e = draw(st.sampled_from(s.events))
        twin = e.model_copy(update={"eventId": uuid.uuid5(uuid.NAMESPACE_URL, f"clash:{e.eventId}")})
        return InvalidHistory(tuple(s.events) + (twin,), kind, status, "tick_clash",
                              e.branchId, e.tick, str(twin.eventId))
    elif kind == "duplicate_id_cell":
        cid = _ensure_cell(b)
        s.insert(cid, "dup", index=0)
        reason = "duplicate_id"
    elif kind == "duplicate_id_run":
        cid, run_id = _ensure_run(b)
        s._emit("run_started", {"runId": run_id, "cellId": cid, "cellRevision": 1, "runCount": 9})
        reason = "duplicate_id"
    elif kind == "duplicate_id_chat":
        s.chat("dup-m", "a")
        b.track()
        s.chat("dup-m", "b")
        reason = "duplicate_id"
    elif kind == "duplicate_id_gap":
        s.gap_start("dup-g")
        b.track()
        s.gap_start("dup-g")
        reason = "duplicate_id"
    elif kind.startswith("unknown_run_"):
        cid = _ensure_cell(b)
        s._run_cell["ghost-run"] = cid
        reason = "unknown_run"
        {"output": lambda: s.output("ghost-run", "x"),
         "finish": lambda: s.finish("ghost-run"),
         "clear": lambda: s.clear_outputs("ghost-run", wait=draw(st.booleans())),
         "replace": lambda: s.replace_output("ghost-run", "x")}[kind.split("_")[-1]]()
    elif kind == "run_not_running":
        cid, run_id = _ensure_run(b)
        if draw(st.booleans()):
            s.finish(run_id)  # finished once ...
        else:
            s.kernel_restart(b.fresh("k"))  # ... or made unfinished by a restart ...
        b.track()
        s.finish(run_id)  # ... and then finished (again)
        reason = "run_not_running"
    elif kind == "unknown_message":
        s.chat_update("ghost-message", "x")
        reason = "unknown_message"
    elif kind == "unknown_gap":
        s.gap_end("ghost-gap")
        reason = "unknown_gap"
    elif kind == "gap_already_ended":
        s.gap_start("g-ended")
        b.track()
        s.gap_end("g-ended")
        b.track()
        s.gap_end("g-ended")
        reason = "gap_already_ended"
    elif kind == "no_output_at_index":
        cid, run_id = _ensure_run(b)
        s.replace_output(run_id, "x", index=draw(st.integers(0, 2)))
        reason = "no_output_at_index"
    elif kind == "index_out_of_range_insert":
        s.insert(b.fresh("c"), "x", index=len(b.cells()) + draw(st.integers(1, 3)))
        reason = "index_out_of_range"
    elif kind == "index_out_of_range_output":
        cid, run_id = _ensure_run(b)
        s.output(run_id, "x", index=draw(st.integers(1, 3)))
        reason = "index_out_of_range"
    elif kind == "index_out_of_range_move":
        cid = _ensure_cell(b)
        n = len(b.cells())
        i = [c.cellId for c in b.cells()].index(cid)
        s.move(cid, i, n + draw(st.integers(0, 2)))
        reason = "index_out_of_range"
    elif kind == "position_mismatch":
        cid = _ensure_cell(b)
        i = [c.cellId for c in b.cells()].index(cid)
        s.move(cid, i + draw(st.integers(1, 3)), 0)
        reason = "position_mismatch"
    elif kind == "invalid_branch_point_future":
        s.branch_from(tick=s.tick + 1 + draw(st.integers(0, 2)), name=b.fresh("bad"))
        reason = "invalid_branch_point"
    elif kind == "invalid_branch_point_sibling_line":
        root = s.root
        s.on(root)
        a = b.fresh("a")
        b.create_branch(root, b.lines[root][-1][0], a)
        s.chat(b.fresh("m"), "on a")
        b.track()
        a_tick = s.tick
        s.on(root)
        s.branch_from(tick=a_tick, name=b.fresh("bad"), parent=root)
        reason = "invalid_branch_point"
    elif kind == "invalid_branch_point_stray_branch":
        s._parent["stray"] = (s.root, 1)
        s._revs["stray"] = []
        s.on("stray")
        s.chat(b.fresh("m"), "no branch point")
        reason = "invalid_branch_point"
    elif kind.startswith("run_cell_mismatch_"):
        cid, run_id = _ensure_run(b)
        s._run_cell[run_id] = "other-" + cid
        reason = "run_cell_mismatch"
        {"output": lambda: s.output(run_id, "x"),
         "replace": lambda: s.replace_output(run_id, "x"),
         "clear": lambda: s.clear_outputs(run_id, wait=draw(st.booleans()))}[kind.split("_")[-1]]()
    elif kind == "unknown_cell_edit":
        s.edit("ghost-cell", "x", revision=1)
        status = "unknown_cell"
    elif kind == "unknown_cell_delete":
        s.delete("ghost-cell")
        status = "unknown_cell"
    elif kind == "unknown_cell_run":
        s.run("ghost-cell", revision=1)
        status = "unknown_cell"
    elif kind == "invalid_revision":
        cid = _ensure_cell(b)
        cell = next(c for c in b.cells() if c.cellId == cid)
        s.edit(cid, "x", revision=cell.revision + draw(st.sampled_from([0, 2, 3])))
        status = "invalid_revision"
    else:  # pragma: no cover
        raise AssertionError(kind)

    bad = s.events[-1]
    return InvalidHistory(tuple(s.events), kind, status, reason, bad.branchId, bad.tick,
                          str(bad.eventId))

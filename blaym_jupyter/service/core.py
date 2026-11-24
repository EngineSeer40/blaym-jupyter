"""The service: every rule between a recorder and the store (S4, Task 6).

Built from ports only (an `EventStore`, a `BlobStore`, an `Authorizer`, a `Clock`); it knows
nothing of SQLite, files or any web framework. It covers ingest, blobs, state, outline,
notifications, branch creation, the active-branch switch, `events_since`, the recorder lease
(in memory only: it is lost when the process stops, and the next request from an editor claims
it again), search (with `rebuild_search`) and recovery (`recover`, rules in `recovery.py`).
`info` and `get_blob` answer the contract's `ServiceInfo` and blob reads.

Public calls take the `credential` first and check it before touching anything.
`ingest` returns the contract's `IngestResponse` (a failed credential refuses every event as
`unauthorized`). The other calls raise `Unauthorized`, because their contract answers have no
such outcome. `ingest` raises `ServiceBusy` when the store stays locked after the retries; the
exception carries the outcomes of the events that were already committed.

Rules in one place (decisions B, C, F, G of the spec):

* redaction comes first, then the size rules (a chat body over `chatMessageMaxBytes` is cut and
  ends with `[truncated: N bytes not stored]`; every preview and searchText is cut to its limit;
  an inline output whose redacted preview is over the inline limit moves to a blob), then blob
  and duplicate checks, then a dry run of the event on the
  cached tip state of the target branch with `history.apply.step`, then one commit per event;
* locks, always taken in this order and never the other way round: the history's recording
  lock (`_lock_for`: ingest, branch creation, the active-branch switch, recovery, search
  rebuild), then the history's hydration lock (loading it from disk), then its small rebuilder
  lock (`exp.rlock`, held only to add committed events to the rebuilder and to read from it).
  Reads (`state_at`, `outline`, `events_since`, `resolve_cell_revision`, `cell_revisions`)
  never take the recording lock and never touch the cached tip (`exp.tips`, changed in place by
  a write's dry run): they see only what the rebuilder and the revision index hold, which is
  filled after a commit. Heavy derived reads copy the committed event list under the small
  lock and walk it outside;
* an invalid event is never stored: it is refused with the typed rebuild result and one
  service gap is opened on the branch (the next `notebook_snapshot` closes it in its own commit);
* `HistoryAdvanced` is computed from the committed event and the tip state before it, and
  sent only after the commit (and after the rebuilder has the event).
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Callable, Mapping

from blaym_jupyter_types import (
    BlaymEvent,
    ClaimRecorderGranted,
    ClaimRecorderRefused,
    ClaimRecorderRequest,
    ClaimRecorderResponse,
    CreateBranchOk,
    CreateBranchRefused,
    CreateBranchRequest,
    CreateBranchResponse,
    EventsSinceRequest,
    EventsSinceResponse,
    HistoryAdvanced,
    HistoryOutline,
    IngestAccepted,
    IngestRefused,
    IngestRequest,
    IngestResponse,
    OutlineBranch,
    OutlineRequest,
    PutBlobResponse,
    ResolveCellRevisionCodeDiffers,
    ResolveCellRevisionOk,
    ResolveCellRevisionRequest,
    ResolveCellRevisionResponse,
    ResolveCellRevisionUnknownCell,
    ResolveCellRevisionUnknownHistory,
    RebuildMissingEvents,
    RebuildOk,
    RecorderEvent,
    SetActiveBranchOk,
    SetActiveBranchRefused,
    SetActiveBranchRequest,
    SetActiveBranchResponse,
    GetBlobRequest,
    SearchRequest,
    SearchResponse,
    ServiceInfo,
    StateAtRequest,
    StoreLimits,
)

from blaym_jupyter.history import build_outline
from blaym_jupyter.history.apply import Failure, step
from blaym_jupyter.ports.auth import Authorizer
from blaym_jupyter.ports.blobs import BlobLimits, BlobStore, check_exploration_id, content_hash_of
from blaym_jupyter.ports.clock import Clock
from blaym_jupyter.ports.search import SearchIndex
from blaym_jupyter.ports.events import (
    BranchRecord,
    DuplicateEventId,
    EventStore,
    InvalidEvent,
    StoreBusy,
    canonical_event_json,
)
from blaym_jupyter.privacy.redact import redact, redact_event

from .auth import Unauthorized
from .blobrefs import is_inline, items_of
from .hydrate import HydratedRebuilder
from .indexing import TEXT_LIMIT, cut_text, documents_for, needs_state, truncate_body
from .lease import RecorderLease
from .notify import Callback, Notifier
from .recovery import RecoveryReport, recover
from .revisions import RevisionIndex, code_hash

RETRY_SLEEPS = (0.05, 0.1, 0.2)
CHAT_MAX_BYTES = 1_048_576
SERVICE_ORIGIN = "blaym-service"
_NOT_FOR_RECORDERS = ("branch_point_created", "notebook_copied")


class ServiceBusy(Exception):
    """The store stayed locked through every retry. Nothing was written for the event in
    progress; `outcomes` holds the answers for the events before it, which are committed."""

    def __init__(self, message: str, outcomes: list | None = None):
        super().__init__(message)
        self.outcomes = outcomes or []


class UnknownHistory(LookupError):
    """`events_since` was asked about a history or branch the service does not have."""


class _TipFailure(Exception):
    def __init__(self, result):
        self.result = result


class _Exp:
    """What the service keeps for one open history (hydrated once, never reloaded)."""

    def __init__(self, user_id: str, workspace_id: str, active: str | None, interval: int | None):
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.active = active
        self.rebuilder = HydratedRebuilder(interval)
        self.branches: dict[str, BranchRecord] = {}
        self.last_tick: dict[str, int] = {}  # per branch: the tick of its last event
        self.tips: dict[str, Any] = {}  # per branch: the state at that last event (lazy)
        self.tip_tick = 0
        self.rlock = threading.Lock()  # the small rebuilder lock: rebuilder, committed, last_tick, tip_tick, branches, active
        self.committed: list = []  # every committed event, as given to the rebuilder (copied for heavy reads)
        self.outline_cache: tuple[int, Any] | None = None  # (len(committed), OutlineResult)
        self.revisions = RevisionIndex()
        self.ilock = threading.Lock()  # guards `revisions`


def _comparable(ev) -> dict:
    d = json.loads(canonical_event_json(ev))
    d.pop("tick", None)
    d.pop("branchId", None)
    return d


def _short(exc: ValueError) -> str:
    errors = exc.errors(include_input=False) if hasattr(exc, "errors") else []  # a pydantic ValidationError
    if not errors:
        return str(exc)[:200]
    err = errors[0]
    where = ".".join(str(p) for p in err["loc"])
    return f"{where}: {err['msg']}"[:200]


def _failure_detail(result) -> str:
    detail = getattr(result, "detail", None)
    if detail:
        return detail
    if result.status == "invalid_revision":
        return f"cell {result.cellId} expected revision {result.expectedRevision}, got {result.actualRevision}"
    if result.status == "unknown_cell":
        return f"cell {result.cellId} does not exist"
    return f"the event cannot be applied ({result.status})"


def _refused(event_id: str, reason: str, detail: str, rebuild=None) -> IngestRefused:
    return IngestRefused(status="refused", eventId=event_id, reason=reason, detail=detail, rebuild=rebuild)


def _accepted(event_id: str, branch_id: str, tick: int, duplicate: bool) -> IngestAccepted:
    return IngestAccepted(status="accepted", eventId=event_id, branchId=branch_id, tick=tick, duplicate=duplicate)


def _raw_event(event: Any) -> dict:
    if isinstance(event, RecorderEvent):
        return event.model_dump(mode="json", exclude_none=True)
    return dict(event)


def _open_service_gaps(state) -> list[str]:
    return [g.gapId for g in state.gaps if g.source == "service" and g.endedTick is None]


class Service:
    def __init__(
        self,
        event_store: EventStore,
        blob_store: BlobStore,
        authorizer: Authorizer,
        clock: Clock,
        *,
        limits: StoreLimits | None = None,
        interval: int | None = 200,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        lease_timeout_ms: int = 60_000,
        search_index: SearchIndex | None = None,
        index_every_edit: bool = False,
    ):
        self._events = event_store
        self._blobs = blob_store
        self._auth = authorizer
        self._clock = clock
        self._new_id = new_id
        self._lease = RecorderLease(clock, lease_timeout_ms)
        self._limits = self._validated(limits)
        if limits is not None and limits.snapshotInterval is not None:
            interval = limits.snapshotInterval
        if interval is not None and interval < 1:
            raise ValueError("the snapshot interval must be at least 1")
        self._interval = interval
        self._chat_max = (limits.chatMessageMaxBytes if limits and limits.chatMessageMaxBytes is not None
                          else CHAT_MAX_BYTES)
        self._index_bytes = (limits.indexedTextBytesPerItem if limits and limits.indexedTextBytesPerItem is not None
                             else TEXT_LIMIT)
        self._notifier = Notifier()
        self._search = search_index
        self._index_every_edit = index_every_edit
        self.search_errors: list[BaseException] = []
        self._exps: dict[str, _Exp] = {}
        self._lock = threading.Lock()  # guards the table of per-history locks only
        self._locks: dict[str, threading.RLock] = {}
        self._hydration_locks: dict[str, threading.Lock] = {}
        self.recovery_report: RecoveryReport | None = None

    def _lock_for(self, exploration_id: str) -> threading.RLock:
        """One lock per history: ingest, branch creation and the active-branch switch of one
        history never interleave; different histories do not wait for each other."""
        with self._lock:
            return self._locks.setdefault(exploration_id, threading.RLock())

    def _hydration_lock_for(self, exploration_id: str) -> threading.Lock:
        with self._lock:
            return self._hydration_locks.setdefault(exploration_id, threading.Lock())

    def recover(self) -> RecoveryReport:
        """Run the recovery rules of `service/recovery.py` over every history and keep the
        report in `recovery_report`. Safe to call again: the second run changes nothing."""
        self.recovery_report = recover(self)
        return self.recovery_report

    def close(self) -> None:
        """Close the stores through their ports."""
        self._events.close()
        self._blobs.close()
        if self._search is not None:
            self._search.close()

    @staticmethod
    def _validated(limits: StoreLimits | None) -> BlobLimits:
        blob = BlobLimits.from_contract(limits)  # raises ValueError for a bad gzip level or a negative size
        if blob.truncated_preview_bytes > blob.max_stored_bytes:
            raise ValueError("the truncated preview cannot be larger than the largest output stored in full")
        if blob.inline_bytes > blob.max_stored_bytes:
            raise ValueError("the inline output size cannot be larger than the largest output stored in full")
        return blob

    @property
    def notifier(self) -> Notifier:
        return self._notifier

    def subscribe(self, exploration_id: str, callback: Callback) -> Callable[[], None]:
        """Callbacks run inside the history lock, after the commit: they must return quickly
        (hand the work to a queue). A callback that raises is recorded in `notifier.errors`."""
        return self._notifier.subscribe(exploration_id, callback)

    # -- reads and blobs ----------------------------------------------------------

    def put_blob(self, credential: Any, exploration_id: str, data) -> PutBlobResponse:
        self._require(credential)
        return self._blobs.put(exploration_id, data, self._limits, lambda text: redact(text)[0])

    def info(self, credential: Any) -> ServiceInfo:
        self._require(credential)
        return ServiceInfo(apiVersion=1, schemaVersion=2)

    def get_blob(self, credential: Any, request: GetBlobRequest) -> bytes:
        """The stored bytes of a blob. `BlobNotFound` / `BlobCorrupt` propagate."""
        self._require(credential)
        return self._blobs.read(request.explorationId, request.contentHash)

    def state_at(self, credential: Any, request: StateAtRequest):
        self._require(credential)
        exp = self._exp(request.explorationId)
        if exp is None:
            return RebuildMissingEvents(status="missing_events", firstMissingTick=1)
        with exp.rlock:
            return exp.rebuilder.state_at(request.branchId, request.tick)

    def _outline_of(self, exp: _Exp, committed: list):
        """The outline result of a copied event list, walked outside every lock; cached by length."""
        cached = exp.outline_cache
        if cached is not None and cached[0] == len(committed):
            return cached[1]
        result = build_outline(committed)
        with exp.rlock:
            if exp.outline_cache is None or exp.outline_cache[0] < len(committed):
                exp.outline_cache = (len(committed), result)
        return result

    def outline(self, credential: Any, request: OutlineRequest) -> HistoryOutline | None:
        self._require(credential)
        exp = self._exp(request.explorationId)
        if exp is None:
            return None
        with exp.rlock:
            committed = list(exp.committed)
        return self._outline_of(exp, committed).outline

    # -- search -------------------------------------------------------------------

    def search(self, credential: Any, request: SearchRequest) -> SearchResponse:
        self._require(credential)
        if self._search is None:
            return SearchResponse(apiVersion=1, hits=[])
        hits = self._search.query(request.explorationId, request.query, request.kinds,
                                  20 if request.limit is None else request.limit)
        return SearchResponse(apiVersion=1, hits=hits)

    def _documents(self, exp: _Exp, events) -> list:
        docs: list = []
        for ev in events:
            state = None
            if needs_state(ev, self._index_every_edit):
                with exp.rlock:
                    result = exp.rebuilder.state_at(ev.branchId, ev.tick)
                state = result.state if isinstance(result, RebuildOk) else None
            docs.extend(documents_for(ev, state, index_every_edit=self._index_every_edit,
                                      text_limit=self._index_bytes))
        return docs

    def _index(self, exp: _Exp, stored: list) -> None:
        if self._search is None:
            return
        try:
            self._search.add(self._documents(exp, stored))
        except Exception as exc:  # noqa: BLE001 - the index is derived data and never fails ingest
            self.search_errors.append(exc)

    def rebuild_search(self, credential: Any, exploration_id: str) -> int:
        """Drop the exploration's documents and index every stored event again, through the
        same policy as the incremental path. Returns the number of documents."""
        self._require(credential)
        if self._search is None:
            return 0
        with self._lock_for(exploration_id):
            exp = self._exp(exploration_id)
            self._search.remove_exploration(exploration_id)
            if exp is None:
                return 0
            docs = self._documents(exp, self._events.events_for(exploration_id))
            self._search.add(docs)
            return len(docs)

    def _require(self, credential: Any) -> None:
        if not self._auth.check(credential):
            raise Unauthorized("the sign-in check failed")

    def _exp(self, exploration_id: str) -> _Exp | None:
        exp = self._exps.get(exploration_id)
        if exp is not None:
            return exp
        with self._hydration_lock_for(exploration_id):  # loading does SQLite I/O: its own lock
            exp = self._exps.get(exploration_id)
            if exp is not None:
                return exp
            record = self._events.exploration(exploration_id)
            if record is None:
                return None
            exp = _Exp(record.user_id, record.workspace_id, record.active_branch_id, self._interval)
            events = self._events.events_for(exploration_id)
            exp.rebuilder.add(events)
            exp.committed.extend(events)
            for ev in events:  # tick order, so the last one seen is the branch's last
                exp.last_tick[ev.branchId] = ev.tick
            exp.tip_tick = self._events.tip_tick(exploration_id)
            exp.branches = {b.branch_id: b for b in self._events.branches(exploration_id)}
            for branch in exp.branches:
                self._replay_revisions(exp, branch)
            self._exps[exploration_id] = exp
            return exp

    @staticmethod
    def _replay_revisions(exp: _Exp, branch_id: str) -> None:
        """Rebuild one branch's revision index from its line of committed events."""
        with exp.rlock:
            line = exp.rebuilder.line_events(branch_id)
        with exp.ilock:
            exp.revisions.replay(branch_id, line or [])

    def _tip(self, exp: _Exp, branch_id: str):
        state = exp.tips.get(branch_id)
        if state is None:
            last = exp.last_tick.get(branch_id)
            if last is None:
                raise _TipFailure(RebuildMissingEvents(status="missing_events", firstMissingTick=1))
            with exp.rlock:
                result = exp.rebuilder.state_at(branch_id, last)
            if not isinstance(result, RebuildOk):
                raise _TipFailure(result)
            state = exp.tips[branch_id] = result.state
        return state

    # -- ingest -------------------------------------------------------------------

    def ingest(self, credential: Any, request: IngestRequest | Mapping) -> IngestResponse:
        if not self._auth.check(credential):
            events = request.events if isinstance(request, IngestRequest) else request.get("events", [])
            detail = "the sign-in check failed"
            ids = [str(_raw_event(e).get("eventId", "")) for e in events]
            return IngestResponse(apiVersion=1, outcomes=[_refused(i, "unauthorized", detail) for i in ids])
        if isinstance(request, IngestRequest):
            envelope, raws = request, [_raw_event(e) for e in request.events]
        else:
            envelope = IngestRequest.model_validate({**request, "events": []})  # ValueError when malformed
            raws = [_raw_event(e) for e in request.get("events", [])]
        outcomes: list = []
        try:
            check_exploration_id(envelope.explorationId)
        except ValueError as exc:
            bad = [_refused(str(r.get("eventId", "")), "invalid_event", str(exc)) for r in raws]
            return IngestResponse(apiVersion=1, outcomes=bad)
        with self._lock_for(envelope.explorationId):
            granted, _ = self._lease.claim(envelope.explorationId, envelope.editor.value, envelope.instanceId)
            if not granted:  # another editor is recording this notebook
                detail = "another editor is recording this notebook"
                return IngestResponse(apiVersion=1, outcomes=[
                    _refused(str(r.get("eventId", "")), "not_active_recorder", detail) for r in raws])
            for raw in raws:
                try:
                    outcomes.append(self._ingest_one(envelope, raw))
                except ServiceBusy as busy:
                    busy.outcomes = outcomes
                    raise
        return IngestResponse(apiVersion=1, outcomes=outcomes)

    def _ingest_one(self, req: IngestRequest, raw: dict):
        eid = str(raw.get("eventId", ""))
        if raw.get("schemaVersion") != 2:
            return _refused(eid, "unsupported_schema_version", "this service understands schemaVersion 2 only")
        try:
            clean = RecorderEvent.model_validate(raw).model_dump(mode="json", exclude_none=True)
        except ValueError as exc:
            return _refused(eid, "invalid_event", _short(exc))
        exploration_id = req.explorationId
        if clean["type"] == "recording_gap_started" and clean["payload"].get("source") == "service":
            return _refused(eid, "invalid_event", "service gaps belong to the service")
        exp = self._exp(exploration_id)
        target = req.targetBranchId or (exp.active if exp else None)
        if (exp is None and req.targetBranchId) or (exp is not None and target not in exp.branches):
            return _refused(eid, "unknown_branch", f"this history has no branch {target}")
        if clean["explorationId"] != exploration_id:
            return _refused(eid, "invalid_event", "the event belongs to another history than the request")
        if clean["type"] in _NOT_FOR_RECORDERS:
            return _refused(eid, "invalid_event", "branch points are made by the service")

        # S5-T, before any side effect (no blob, no event, no gap): replacing the notebook would orphan the
        # output of the run in progress. An event id already stored goes on to the duplicate check instead.
        if (exp is not None and clean["type"] in ("notebook_snapshot", "changed_outside_editor")
                and self._events.find_event(exploration_id, eid) is None):
            try:
                running = any(r.status.root == "running" for r in self._tip(exp, target).runs)
            except _TipFailure:
                running = False  # the normal path reports an unreadable tip
            if running:
                return _refused(eid, "run_in_progress",
                                "a cell is running; the notebook cannot be replaced until it ends")

        # redaction first: everything compared or stored below is the hidden text
        try:
            original = BlaymEvent.model_validate({**clean, "branchId": target or "pending", "tick": 1}).root
        except ValueError as exc:
            return _refused(eid, "invalid_event", _short(exc))
        event = redact_event(original)
        if event.type in ("chat_message_added", "chat_message_updated"):
            event.payload.body = truncate_body(event.payload.body, self._chat_max)
        for before, after in zip(items_of(original), items_of(event)):
            if after.searchText is not None:
                after.searchText = cut_text(after.searchText, self._index_bytes)
            inline_limit = self._limits.inline_bytes
            if is_inline(before):
                content = after.preview.encode("utf-8")  # inline: the preview holds all the content
                if len(content) <= inline_limit:
                    after.contentHash = content_hash_of(content)
                    after.sizeBytes = len(content)
                else:  # too big for the event: the redacted content moves to a blob
                    put = self._blobs.put(exploration_id, content, self._limits, lambda text: redact(text)[0])
                    after.preview = cut_text(after.preview, inline_limit)
                    after.contentHash = put.contentHash
                    after.sizeBytes = put.originalBytes
                    after.truncated = put.truncated
            elif not self._blobs.exists(exploration_id, after.contentHash):
                return _refused(eid, "missing_blob", f"output content {after.contentHash} was not stored first")
            else:
                after.preview = cut_text(after.preview, inline_limit)

        if exp is not None:
            existing = self._events.find_event(exploration_id, eid)
            if existing is not None:
                if _comparable(existing) == _comparable(event):
                    return _accepted(eid, existing.branchId, existing.tick, True)
                return _refused(eid, "conflict", "an event with this id was recorded earlier with different content")

        try:
            if exp is None:
                return self._ingest_first(exploration_id, event, eid)
            return self._ingest_next(exp, exploration_id, target, event, eid)
        except DuplicateEventId:
            return _refused(eid, "conflict", "this event id is already used by another history")
        except InvalidEvent as exc:
            return _refused(eid, "invalid_event", str(exc)[:200])

    def _mapping(self, event, branch_id: str) -> dict:
        d = event.model_dump(mode="json", exclude_none=True)
        d.pop("tick", None)
        d["branchId"] = branch_id
        return d

    def _ingest_first(self, exploration_id: str, event, eid: str):
        if event.type != "notebook_snapshot":
            return _refused(eid, "bad_first_event", "a new history must start with a notebook_snapshot")
        root = self._new_id()
        mapping = self._mapping(event, root)
        try:
            state = step(None, BlaymEvent.model_validate({**mapping, "tick": 1}).root)
        except Failure as failure:
            return _refused(eid, "invalid_event", _failure_detail(failure.result), failure.result)
        with self._hydration_lock_for(exploration_id):  # a read must not hydrate a half-made history
            stored = self._append(exploration_id, [mapping], branches=[BranchRecord(root)], active_branch_id=root)
            exp = _Exp(event.userId, event.workspaceId, root, self._interval)
            exp.branches[root] = BranchRecord(root)
            exp.tips[root] = state
            self._record(exp, exploration_id, root, stored, [(None, None)] * len(stored), register=True)
        return _accepted(eid, root, stored[0].tick, False)

    def _ingest_next(self, exp: _Exp, exploration_id: str, target: str, event, eid: str):
        try:
            tip = self._tip(exp, target)
        except _TipFailure as failure:
            return _refused(eid, "invalid_event", _failure_detail(failure.result), failure.result)
        if event.type == "recording_gap_ended" and any(
            g.gapId == event.payload.gapId and g.source == "service" and g.endedTick is None for g in tip.gaps
        ):
            return _refused(eid, "invalid_event", "service gaps belong to the service")
        batch = [self._mapping(event, target)]
        if event.type == "notebook_snapshot":  # a snapshot is the recovery point: close service gaps first
            closing = [self._service_event(exp, exploration_id, target, "recording_gap_ended", {"gapId": g})
                       for g in _open_service_gaps(tip)]
            batch = closing + batch
        try:
            infos = self._dry_run(exp, tip, batch)
        except Failure as failure:
            exp.tips.pop(target, None)  # half changed: rebuilt from the rebuilder when next needed
            self._open_gap(exp, exploration_id, target)
            return _refused(eid, "invalid_event", _failure_detail(failure.result), failure.result)
        stored = self._commit(exp, exploration_id, target, batch, infos)
        return _accepted(eid, target, stored[-1].tick, False)

    def _service_event(self, exp: _Exp, exploration_id: str, branch_id: str, type_: str, payload: dict) -> dict:
        return {
            "schemaVersion": 2,
            "eventId": self._new_id(),
            "explorationId": exploration_id,
            "branchId": branch_id,
            "wallTimeMs": self._clock.now_ms(),
            "userId": exp.user_id,
            "workspaceId": exp.workspace_id,
            "originId": SERVICE_ORIGIN,
            "authorHint": "blaym",
            "authorConfidence": "known",
            "type": type_,
            "payload": payload,
        }

    def _open_gap(self, exp: _Exp, exploration_id: str, branch_id: str) -> None:
        try:
            tip = self._tip(exp, branch_id)
        except _TipFailure:
            return
        if _open_service_gaps(tip):
            return
        gap = self._service_event(
            exp, exploration_id, branch_id, "recording_gap_started",
            {"gapId": self._new_id(), "source": "service", "reason": "unknown"},
        )
        infos = self._dry_run(exp, tip, [gap])
        self._commit(exp, exploration_id, branch_id, [gap], infos)

    @staticmethod
    def _dry_run(exp: _Exp, tip, batch: list[dict]) -> list[tuple]:
        """Apply the batch in place to the tip state, with the ticks the store will give.
        Returns (runStarted, runsEnded) per event, read from the state BEFORE each step."""
        infos = []
        tick = exp.tip_tick
        for mapping in batch:
            tick += 1
            ev = BlaymEvent.model_validate({**mapping, "tick": tick}).root
            started = ended = None
            if ev.type == "run_started":
                started = ev.payload.runId
            elif ev.type == "run_finished":
                if any(r.runId == ev.payload.runId and r.status.root == "running" for r in tip.runs):
                    ended = [ev.payload.runId]
            elif ev.type == "kernel_restarted":
                ended = [r.runId for r in tip.runs if r.status.root == "running"] or None
            step(tip, ev)
            infos.append((started, ended))
        return infos

    def _append(self, exploration_id: str, batch: list[dict], **kwargs) -> list:
        for delay in (*RETRY_SLEEPS, None):
            try:
                return self._events.append(exploration_id, batch, **kwargs)
            except StoreBusy as exc:
                if delay is None:
                    raise ServiceBusy("the history database is busy; try again shortly") from exc
                self._clock.sleep(delay)
        raise AssertionError("unreachable")

    def _commit(self, exp: _Exp, exploration_id: str, branch_id: str, batch: list[dict], infos: list) -> list:
        expected = [exp.tip_tick + 1 + i for i in range(len(batch))]
        try:
            stored = self._append(exploration_id, batch)
        except BaseException:
            exp.tips.pop(branch_id, None)  # the dry run already moved it past what is stored
            raise
        if [ev.tick for ev in stored] != expected:
            exp.tips.clear()
        self._record(exp, exploration_id, branch_id, stored, infos)
        return stored

    def _record(self, exp: _Exp, exploration_id: str, branch_id: str, stored: list, infos: list,
                register: bool = False) -> None:
        """After a commit: the rebuilder gets the events, then (and only then) subscribers hear.
        A first event also makes the history known (`register`), in the same step."""
        with exp.rlock:  # the only work done under the small lock
            exp.rebuilder.add(stored)
            exp.committed.extend(stored)
            for ev in stored:
                exp.last_tick[ev.branchId] = ev.tick
            exp.tip_tick = stored[-1].tick
            if register:
                self._exps[exploration_id] = exp
        with exp.ilock:  # the revision index follows what is committed, never a dry run
            for ev in stored:
                exp.revisions.observe(ev.branchId, ev)
        self._index(exp, stored)
        advances = []
        for ev, (started, ended) in zip(stored, infos):
            extra: dict = {}
            if started is not None:
                extra["runStarted"] = started
            if ended:
                extra["runsEnded"] = ended
            advances.append(HistoryAdvanced(explorationId=exploration_id, branchId=ev.branchId, tick=ev.tick, **extra))
        self._notifier.publish(exploration_id, advances)

    # -- branches, the active branch, catching up -----------------------------------

    def create_branch(self, credential: Any, request: CreateBranchRequest) -> CreateBranchResponse:
        """Branch from a settled moment: one transaction writes the branch point event, the
        branch record and the active-branch change. The new branch becomes the active one."""

        def refuse(reason: str, detail: str) -> CreateBranchResponse:
            return CreateBranchResponse(CreateBranchRefused(status="refused", reason=reason, detail=detail))

        if not self._auth.check(credential):
            return refuse("unauthorized", "the sign-in check failed")

        with self._lock_for(request.explorationId):
            exp = self._exp(request.explorationId)
            if exp is None or request.fromBranchId not in exp.branches:
                return refuse("unknown_moment", "this history or branch does not exist")
            tick = request.fromTick
            with exp.rlock:
                has_tick = exp.rebuilder.has_tick(tick)
                line = exp.rebuilder.line_events(request.fromBranchId)
                result = exp.rebuilder.state_at(request.fromBranchId, tick) if line is not None else None
            if tick < 1 or tick > exp.tip_tick or not has_tick:
                return refuse("unknown_moment", f"there is no recorded moment at tick {tick}")
            if line is None:
                return refuse("moment_not_rebuildable", "this branch cannot be rebuilt")
            if tick not in {ev.tick for ev in line}:
                return refuse("not_on_line", f"tick {tick} is not part of the history of branch {request.fromBranchId}")
            if not isinstance(result, RebuildOk):
                return refuse("moment_not_rebuildable", "the notebook at this moment cannot be rebuilt")
            if any(r.status.root == "running" for r in result.state.runs):
                return refuse("unsettled_moment", "a cell was still running at this moment; branch from a settled one")
            new = self._new_id()
            record = BranchRecord(new, request.fromBranchId, tick, request.name)
            event = self._service_event(exp, request.explorationId, new, "branch_point_created", {
                "parentBranchId": request.fromBranchId, "parentTick": tick, "name": request.name})
            stored = self._append(request.explorationId, [event], branches=[record], active_branch_id=new)
            with exp.rlock:
                exp.rebuilder.add(stored)
                exp.committed.extend(stored)
                exp.last_tick[new] = stored[0].tick
                exp.tip_tick = stored[0].tick
                exp.branches[new] = record
                exp.active = new
            self._replay_revisions(exp, new)  # the new branch starts from the cells of its branch point
            branch = OutlineBranch(branchId=new, name=request.name, parentBranchId=request.fromBranchId,
                                   parentTick=tick, ownTicks=[stored[0].tick], runSpans=[])
            self._notifier.publish(request.explorationId, [HistoryAdvanced(
                explorationId=request.explorationId, branchId=new, tick=stored[0].tick, newBranch=branch)])
            return CreateBranchResponse(CreateBranchOk(status="ok", branch=branch))

    def set_active_branch(self, credential: Any, request: SetActiveBranchRequest) -> SetActiveBranchResponse:
        """Make a branch the one recorders write to by default. Durable; no notification."""
        if not self._auth.check(credential):
            return SetActiveBranchResponse(SetActiveBranchRefused(
                status="refused", reason="unauthorized", detail="the sign-in check failed"))
        with self._lock_for(request.explorationId):
            exp = self._exp(request.explorationId)
            if exp is None or request.branchId not in exp.branches:
                return SetActiveBranchResponse(SetActiveBranchRefused(
                    status="refused", reason="unknown_branch", detail="this history has no such branch"))
            self._events.set_active_branch(request.explorationId, request.branchId)
            with exp.rlock:
                exp.active = request.branchId
            return SetActiveBranchResponse(SetActiveBranchOk(status="ok", branchId=request.branchId))

    def events_since(self, credential: Any, request: EventsSinceRequest) -> EventsSinceResponse:
        """Events on the line of a branch after `afterTick`, in tick order, with the current
        outline. Raises `UnknownHistory` for an unknown history or branch."""
        self._require(credential)
        exp = self._exp(request.explorationId)
        if exp is None:
            raise UnknownHistory("this history or branch does not exist")
        with exp.rlock:
            if request.branchId not in exp.branches:
                raise UnknownHistory("this history or branch does not exist")
            line = exp.rebuilder.line_events(request.branchId)
            committed = list(exp.committed)
        outline = self._outline_of(exp, committed).outline
        if line is None or outline is None:
            raise UnknownHistory("this branch cannot be read")
        events = [BlaymEvent(root=ev) for ev in line if ev.tick > request.afterTick]
        return EventsSinceResponse(apiVersion=1, events=events, outline=outline)

    def resolve_cell_revision(self, credential: Any, request: ResolveCellRevisionRequest) -> ResolveCellRevisionResponse:
        """Which committed revision of a cell held this code (S5-D). Reads the committed revision
        index only; never raises for an unknown history."""
        self._require(credential)
        exp = self._exp(request.explorationId)
        if exp is None:
            return ResolveCellRevisionResponse(ResolveCellRevisionUnknownHistory(status="unknown_history"))
        hashed = code_hash(redact(request.code)[0])  # the same hiding ingest applies to cell code
        with exp.rlock:
            branch = request.branchId or exp.active
            known = branch in exp.branches
        answer = None
        if known:
            with exp.ilock:
                answer = exp.revisions.resolve(branch, request.cellId, hashed)
        if answer is None:
            return ResolveCellRevisionResponse(ResolveCellRevisionUnknownHistory(status="unknown_history"))
        if answer[0] == "ok":
            return ResolveCellRevisionResponse(ResolveCellRevisionOk(status="ok", cellRevision=answer[1]))
        if answer[0] == "code_differs":
            return ResolveCellRevisionResponse(
                ResolveCellRevisionCodeDiffers(status="code_differs", currentRevision=answer[1]))
        return ResolveCellRevisionResponse(ResolveCellRevisionUnknownCell(status="unknown_cell"))

    def cell_revisions(self, credential: Any, exploration_id: str) -> dict[str, int]:
        """The tip revision of every cell on the active branch, from committed state ({} if unknown)."""
        self._require(credential)
        exp = self._exp(exploration_id)
        if exp is None:
            return {}
        with exp.rlock:
            branch = exp.active
        with exp.ilock:
            return exp.revisions.tip_revisions(branch) if branch else {}

    def claim_recorder(self, credential: Any, request: ClaimRecorderRequest) -> ClaimRecorderResponse:
        """Take or share the one-recorder lease (see `lease.py`)."""
        self._require(credential)
        with self._lock_for(request.explorationId):
            granted, holder = self._lease.claim(request.explorationId, request.editor.value, request.instanceId)
        if granted:
            return ClaimRecorderResponse(ClaimRecorderGranted(status="granted"))
        return ClaimRecorderResponse(ClaimRecorderRefused(
            status="refused", holderEditor=holder, detail=f"{holder} is recording this notebook now"))

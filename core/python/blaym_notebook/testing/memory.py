"""In-memory implementations of the ports (S4). Used to test the rules without SQLite."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from blaym_notebook_types import BlaymEvent, PutBlobResponse, SearchHit

from blaym_notebook.ports.blobs import (
    BlobCorrupt,
    BlobLimits,
    BlobNotFound,
    check_exploration_id,
    content_hash_of,
    hash_hex,
    prepare_content,
)

from blaym_notebook.ports.search import WORD_RE, SearchDocument, plain_snippet
from blaym_notebook.ports.events import (
    BranchRecord,
    DuplicateEventId,
    ExplorationRecord,
    InvalidEvent,
    canonical_event_json,
)


def parse_new_event(exploration_id: str, raw: Mapping[str, Any]):
    """Validate an event without a tick; return its stored (tick-less) JSON form."""
    try:
        event = BlaymEvent.model_validate({**raw, "tick": 1}).root
    except (TypeError, ValueError) as exc:
        raise InvalidEvent(str(exc)) from exc
    if event.explorationId != exploration_id:
        raise InvalidEvent(f"event belongs to {event.explorationId}, not {exploration_id}")
    return event


class MemoryEventStore:
    """Keeps the same JSON text the SQLite store keeps, so both return identical events."""

    def __init__(self) -> None:
        self._events: dict[str, dict[int, str]] = {}
        self._ids: dict[str, tuple[str, int]] = {}  # event id -> (exploration, tick), global
        self._explorations: dict[str, ExplorationRecord] = {}
        self._branches: dict[str, list[BranchRecord]] = {}

    def append(self, exploration_id, events: Sequence[Mapping[str, Any]], *, branches=(), active_branch_id=None):
        parsed = [parse_new_event(exploration_id, raw) for raw in events]
        seen: set[str] = set()
        for ev in parsed:
            key = str(ev.eventId)
            if key in self._ids or key in seen:
                raise DuplicateEventId(key)
            seen.add(key)
        known = {b.branch_id for b in self._branches.get(exploration_id, [])}
        for b in branches:
            if b.branch_id in known:
                raise InvalidEvent(f"branch already exists: {b.branch_id}")
            known.add(b.branch_id)
        exists = exploration_id in self._explorations or bool(parsed)
        if (branches or active_branch_id is not None) and not exists:
            raise InvalidEvent(f"unknown exploration {exploration_id}")
        if active_branch_id is not None and active_branch_id not in known:
            raise InvalidEvent(f"unknown branch {active_branch_id}")
        # nothing below can fail: commit
        tick = self.tip_tick(exploration_id)
        stored = []
        for ev in parsed:
            tick += 1
            ev = ev.model_copy(update={"tick": tick})
            text = canonical_event_json(ev)
            self._events.setdefault(exploration_id, {})[tick] = text
            self._ids[str(ev.eventId)] = (exploration_id, tick)
            stored.append(_parse(text))
        if parsed and exploration_id not in self._explorations:
            first = parsed[0]
            self._explorations[exploration_id] = ExplorationRecord(
                exploration_id, first.userId, first.workspaceId
            )
        if exploration_id in self._explorations:
            self._branches.setdefault(exploration_id, []).extend(branches)
            if active_branch_id is not None:
                self._explorations[exploration_id] = _with_active(
                    self._explorations[exploration_id], active_branch_id
                )
        return stored

    def events_for(self, exploration_id):
        by_tick = self._events.get(exploration_id, {})
        return [_parse(by_tick[t]) for t in sorted(by_tick)]

    def find_event(self, exploration_id, event_id):
        where = self._ids.get(str(event_id))
        if where is None or where[0] != exploration_id:
            return None
        return _parse(self._events[exploration_id][where[1]])

    def tip_tick(self, exploration_id):
        by_tick = self._events.get(exploration_id)
        return max(by_tick) if by_tick else 0

    def exploration(self, exploration_id):
        return self._explorations.get(exploration_id)

    def explorations(self):
        return list(self._explorations.values())

    def branches(self, exploration_id):
        return list(self._branches.get(exploration_id, []))

    def set_active_branch(self, exploration_id, branch_id):
        known = {b.branch_id for b in self._branches.get(exploration_id, [])}
        if exploration_id not in self._explorations or branch_id not in known:
            raise InvalidEvent(f"unknown branch {branch_id}")
        self._explorations[exploration_id] = _with_active(self._explorations[exploration_id], branch_id)

    def close(self) -> None:
        pass


def _parse(text: str):
    return BlaymEvent.model_validate_json(text).root


def _with_active(rec: ExplorationRecord, branch_id: str) -> ExplorationRecord:
    return ExplorationRecord(rec.exploration_id, rec.user_id, rec.workspace_id, branch_id)


class MemoryBlobStore:
    """Keeps stored bytes in a dict, keyed by exploration and hash."""

    def __init__(self) -> None:
        self._blobs: dict[tuple[str, str], bytes] = {}

    def put(
        self,
        exploration_id: str,
        data: bytes | Iterable[bytes],
        limits: BlobLimits = BlobLimits(),
        redact: Callable[[str], str] | None = None,
    ) -> PutBlobResponse:
        check_exploration_id(exploration_id)
        prepared = prepare_content(data, limits, redact)
        content_hash = content_hash_of(prepared.stored)
        self._blobs[(exploration_id, content_hash)] = prepared.stored
        return PutBlobResponse(
            contentHash=content_hash,
            originalBytes=prepared.original_bytes,
            storedBytes=len(prepared.stored),
            truncated=prepared.truncated,
        )

    def read(self, exploration_id: str, content_hash: str) -> bytes:
        check_exploration_id(exploration_id)
        stored = self._blobs.get((exploration_id, content_hash))
        if stored is None:
            raise BlobNotFound(content_hash)
        if content_hash_of(stored) != content_hash:
            raise BlobCorrupt(content_hash)
        return stored

    def exists(self, exploration_id: str, content_hash: str) -> bool:
        check_exploration_id(exploration_id)
        return hash_hex(content_hash) is not None and (exploration_id, content_hash) in self._blobs

    def cleanup_staging(self) -> int:
        return 0  # nothing is staged in memory

    def orphans(self, exploration_id: str) -> list[str]:
        check_exploration_id(exploration_id)
        return []  # a file without a record cannot exist in memory

    def hashes(self, exploration_id: str) -> list[str]:
        check_exploration_id(exploration_id)
        return sorted(h for (e, h) in self._blobs if e == exploration_id)

    def close(self) -> None:
        pass


__all__ = ["MemoryBlobStore", "MemoryEventStore", "canonical_event_json", "parse_new_event"]


def _word_match(text: str, words: list[str]) -> tuple[int, int]:
    """(start, length) of the first place where `words` occur consecutively, each a
    prefix; (-1, 0) when none."""
    if not words:
        return -1, 0
    found = [(m.group().lower(), m.start(), m.end()) for m in WORD_RE.finditer(text)]
    n = len(words)
    for i in range(len(found) - n + 1):
        if all(f[0].startswith(w) for f, w in zip(found[i:i + n], words)):
            return found[i][1], found[i + n - 1][2] - found[i][1]
    return -1, 0


class MemorySearchIndex:
    """A simple search index with the port's rules: substring for code, word prefix otherwise."""

    def __init__(self) -> None:
        self._docs: list[SearchDocument] = []

    def add(self, documents) -> None:
        self._docs.extend(documents)

    def remove_exploration(self, exploration_id: str) -> None:
        self._docs = [d for d in self._docs if d.exploration_id != exploration_id]

    def query(self, exploration_id: str, text: str, kinds=None, limit: int = 20) -> list[SearchHit]:
        needle = text.strip().lower()
        wanted = None if kinds is None else {getattr(k, "value", k) for k in kinds}
        if not needle or limit < 1:
            return []
        words = [w.lower() for w in WORD_RE.findall(needle)]
        hits = []
        for d in self._docs:
            if d.exploration_id != exploration_id or (wanted is not None and d.kind not in wanted):
                continue
            if d.kind == "code":
                at, length = d.text.lower().find(needle.lower()), len(needle)
            else:
                at, length = _word_match(d.text, words)
            if at >= 0:
                hits.append(SearchHit(branchId=d.branch_id, tick=d.tick, kind=d.kind,
                                      snippet=plain_snippet(d.text, at, length)))
        hits.sort(key=lambda h: -h.tick)  # stable: ties keep insertion order
        return hits[:limit]

    def close(self) -> None:
        pass

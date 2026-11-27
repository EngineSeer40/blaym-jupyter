"""`/stream`: server-sent frames for `HistoryAdvanced`, from `Service.subscribe`. Blocking and
framework-free: a host pulls frames from the iterator and writes them. Closing it unsubscribes.

Frames: `event: advanced` + `data: <json>` per advance, and a `: keep-alive` comment when nothing
arrived for `keepalive_s`. When `after_tick` is below the tip, one catch-up frame with the tip comes first.
"""

from __future__ import annotations

import asyncio
import queue
from typing import Any, Iterator

from blaym_notebook_types import HistoryAdvanced, OutlineRequest

from blaym_notebook.service.core import Service

KEEPALIVE_S = 15.0
KEEP_ALIVE = b": keep-alive\n\n"
MAX_PENDING = 1000  # a stalled client keeps at most this many frames; the oldest go first


def offer(pending: Any, item: Any) -> None:
    """Non-blocking put on a `queue.Queue` or `asyncio.Queue` made with `maxsize=MAX_PENDING`;
    when full, the oldest is dropped (callers must not block: they run inside the history lock)."""
    for _ in range(2):
        try:
            return pending.put_nowait(item)
        except (queue.Full, asyncio.QueueFull):
            try:
                pending.get_nowait()
            except (queue.Empty, asyncio.QueueEmpty):
                pass


def advanced_frame(advance: HistoryAdvanced) -> bytes:
    return b"event: advanced\ndata: " + advance.model_dump_json(exclude_none=True).encode() + b"\n\n"


def catch_up_frame(service: Service, credential: Any, exploration_id: str, after_tick: int | None) -> bytes | None:
    """One frame with the tip when `after_tick` is below it; none otherwise (or with no history yet)."""
    if after_tick is None:
        return None
    outline = service.outline(credential, OutlineRequest(explorationId=exploration_id))
    if outline is None or outline.highestTick <= after_tick:
        return None
    owner = next((b for b in outline.branches if outline.highestTick in b.ownTicks), None)
    branch_id = owner.branchId if owner else outline.rootBranchId
    return advanced_frame(HistoryAdvanced(explorationId=exploration_id, branchId=branch_id, tick=outline.highestTick))


class AdvancedStream:
    """Raises `Unauthorized` from the constructor, so a host can refuse before it sends headers."""

    def __init__(self, service: Service, credential: Any, exploration_id: str,
                 after_tick: int | None = None, keepalive_s: float = KEEPALIVE_S) -> None:
        self._queue: queue.Queue[HistoryAdvanced] = queue.Queue(maxsize=MAX_PENDING)
        self._keepalive_s = keepalive_s
        self._closed = False
        service.info(credential)  # the sign-in check
        self._unsubscribe = service.subscribe(exploration_id, lambda advance: offer(self._queue, advance))
        try:  # subscribed first, so nothing between the tip read and the first wait is lost
            self._catch_up = catch_up_frame(service, credential, exploration_id, after_tick)
        except BaseException:
            self._unsubscribe()
            raise

    def __iter__(self) -> Iterator[bytes]:
        if self._catch_up is not None:
            first, self._catch_up = self._catch_up, None
            yield first
        while not self._closed:
            try:
                advance = self._queue.get(timeout=self._keepalive_s)
            except queue.Empty:
                yield KEEP_ALIVE
                continue
            if advance is None:  # closed while waiting
                return
            yield advanced_frame(advance)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._unsubscribe()
            offer(self._queue, None)  # type: ignore[arg-type]  # wakes a reader blocked in get()

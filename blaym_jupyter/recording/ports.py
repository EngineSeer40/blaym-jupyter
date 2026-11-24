"""The small interfaces the recording layer meets (S5, section 3 of the spec).

Typing protocols only. Everything impure (the clock, ids, timers, storage, the service) comes
in through these, so the layer is tested with fakes and reused by another editor's adapter.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Protocol

from blaym_jupyter_types import (
    IngestRequest,
    IngestResponse,
    OpenGap,
    PutBlobResponse,
    ResolveCellRevisionRequest,
    ResolveCellRevisionResponse,
)

Cancel = Callable[[], None]


class Ingestor(Protocol):
    """What the recorder needs of the service, with the credential already bound."""

    def ingest(self, request: IngestRequest) -> IngestResponse: ...

    def put_blob(self, exploration_id: str, data: bytes | Iterable[bytes]) -> PutBlobResponse: ...

    def resolve_cell_revision(self, request: ResolveCellRevisionRequest) -> ResolveCellRevisionResponse: ...

    def cell_revisions(self, exploration_id: str) -> dict[str, int]:
        """The tip revision of every cell."""
        ...


class RegistryStore(Protocol):
    """Where the notebook registry keeps its records (one small record per notebook)."""

    def load_all(self) -> list[dict[str, Any]]: ...

    def save(self, record: dict[str, Any]) -> None: ...

    def delete(self, notebook_id: str) -> None: ...


class Clock(Protocol):
    def now_ms(self) -> int: ...


class Scheduler(Protocol):
    def call_later(self, ms: int, fn: Callable[[], None]) -> Cancel:
        """Run `fn` once after `ms`; the returned function cancels it."""
        ...


class Ids(Protocol):
    def new(self) -> str: ...


class StateProbe(Protocol):
    """What the registry's heartbeat reads of the recorded history (bound by the host)."""

    def runs_open(self, exploration_id: str) -> int: ...

    def cell_revisions(self, exploration_id: str) -> dict[str, int]: ...

    def open_gaps(self, exploration_id: str) -> list[OpenGap]: ...

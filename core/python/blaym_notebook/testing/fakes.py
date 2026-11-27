"""Small test doubles for the service: a manual clock and deterministic ids."""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("5c1f0a6e-0000-4000-8000-00000000d0d0")


class FakeClock:
    """Manual time. `sleep` advances it and records the request."""

    def __init__(self, start_ms: int = 1_770_000_000_000):
        self._ms = start_ms
        self.sleeps: list[float] = []

    def now_ms(self) -> int:
        return self._ms

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._ms += int(seconds * 1000)

    def advance(self, seconds: float) -> None:
        self._ms += int(seconds * 1000)


class IdFactory:
    """Deterministic uuid strings: the same prefix and call order give the same ids."""

    def __init__(self, prefix: str = "id"):
        self._prefix = prefix
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return str(uuid.uuid5(_NAMESPACE, f"{self._prefix}:{self._n}"))

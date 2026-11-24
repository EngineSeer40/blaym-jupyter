"""The clock port: wall time and sleeping, so tests can control both."""

from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def sleep(self, seconds: float) -> None: ...

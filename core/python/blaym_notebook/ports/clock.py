"""The clock port: wall time and sleeping, so tests can control both."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

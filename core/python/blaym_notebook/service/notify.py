"""In-process notifications (S4, decision section 3).

Callbacks run only after the commit that made the event durable, in tick order. An
exception in a callback never reaches the caller of ingest: it is collected in `errors`.
"""

from __future__ import annotations

from typing import Callable, Iterable

from blaym_notebook_types import HistoryAdvanced

Callback = Callable[[HistoryAdvanced], None]


class Notifier:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = {}
        self.errors: list[BaseException] = []

    def subscribe(self, exploration_id: str, callback: Callback) -> Callable[[], None]:
        self._subscribers.setdefault(exploration_id, []).append(callback)

        def unsubscribe() -> None:
            subscribers = self._subscribers.get(exploration_id, [])
            if callback in subscribers:
                subscribers.remove(callback)

        return unsubscribe

    def publish(self, exploration_id: str, advances: Iterable[HistoryAdvanced]) -> None:
        for advance in advances:
            for callback in list(self._subscribers.get(exploration_id, [])):
                try:
                    callback(advance)
                except Exception as exc:  # noqa: BLE001 - a subscriber must never break ingest
                    self.errors.append(exc)

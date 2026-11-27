"""SQLite and file implementations of the ports (S4). Holds no product rules."""

from .events import SqliteEventStore

__all__ = ["SqliteEventStore"]

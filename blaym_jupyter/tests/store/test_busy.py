"""A held write lock from a second connection gives StoreBusy after the timeout."""

from __future__ import annotations

import sqlite3
import time

import pytest

from blaym_jupyter.ports.events import StoreBusy
from blaym_jupyter.store.blobs import FileBlobStore
from blaym_jupyter.store.events import SqliteEventStore
from blaym_jupyter.tests.history.scenario import Scenario


def _holder(root):
    conn = sqlite3.connect(root / "metadata.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def test_append_and_put_give_store_busy_while_another_connection_holds_the_write_lock(tmp_path):
    events = SqliteEventStore(tmp_path, busy_timeout=0.1)
    blobs = FileBlobStore(tmp_path, busy_timeout=0.1)
    s = Scenario()
    s.start(cells=[("c1", "x")])
    first = s.events[0].model_dump(mode="json", exclude_none=True)
    first.pop("tick")
    holder = _holder(tmp_path)
    try:
        t0 = time.monotonic()
        with pytest.raises(StoreBusy):
            events.append("exploration-1", [first])
        assert time.monotonic() - t0 < 3
        with pytest.raises(StoreBusy):
            blobs.put("exploration-1", b"hello")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert events.append("exploration-1", [first])[0].tick == 1  # nothing was half written
    blobs.put("exploration-1", b"hello")

"""A held write lock from a second connection gives StoreBusy after the timeout."""

from __future__ import annotations

import sqlite3
import time

import pytest

from blaym_notebook.ports.events import StoreBusy
from blaym_notebook.store.blobs import FileBlobStore
from blaym_notebook.store.events import SqliteEventStore
from blaym_notebook.tests.ports.harness import EXP, make_event


def _holder(root):
    conn = sqlite3.connect(root / "metadata.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def test_append_and_put_give_store_busy_while_another_connection_holds_the_write_lock(tmp_path):
    events = SqliteEventStore(tmp_path, busy_timeout=0.1)
    blobs = FileBlobStore(tmp_path, busy_timeout=0.1)
    first = make_event(1)
    holder = _holder(tmp_path)
    try:
        t0 = time.monotonic()
        with pytest.raises(StoreBusy):
            events.append(EXP, [first])
        assert time.monotonic() - t0 < 3
        with pytest.raises(StoreBusy):
            blobs.put(EXP, b"hello")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert events.append(EXP, [first])[0].tick == 1  # nothing was half written
    blobs.put(EXP, b"hello")

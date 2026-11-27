"""The SQLite search index: its own file, restarts, and a busy database."""

from __future__ import annotations

import sqlite3

import pytest

from blaym_jupyter.ports.events import StoreBusy
from blaym_jupyter.ports.search import SearchDocument
from blaym_jupyter.store.search import INDEX_NAME, SqliteSearchIndex

E = "exp-1"


def doc(text):
    return SearchDocument(E, "b", 1, "chat", text)


def test_separate_file_survives_reopen_starts_empty_if_dropped_and_busy_is_translated(tmp_path):
    idx = SqliteSearchIndex(tmp_path, busy_timeout=0.05)
    idx.add([doc("hello world")])
    assert (tmp_path / INDEX_NAME).exists() and not (tmp_path / "metadata.sqlite3").exists()
    other = sqlite3.connect(tmp_path / INDEX_NAME, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(StoreBusy):
        idx.add([doc("x")])
    with pytest.raises(StoreBusy):
        idx.remove_exploration(E)
    other.execute("ROLLBACK")
    other.close()
    idx.close()
    idx = SqliteSearchIndex(tmp_path)
    assert len(idx.query(E, "hello")) == 1
    idx.close()
    for f in tmp_path.glob(INDEX_NAME + "*"):
        f.unlink()
    idx = SqliteSearchIndex(tmp_path)
    assert idx.query(E, "hello") == []
    idx.add([doc("hello again")])
    assert len(idx.query(E, "hello")) == 1
    idx.close()

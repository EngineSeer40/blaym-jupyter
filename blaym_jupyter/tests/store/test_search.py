"""The SQLite search index (S4, Task 8)."""

from __future__ import annotations

import sqlite3

import pytest

from blaym_jupyter.ports.events import StoreBusy
from blaym_jupyter.ports.search import SearchDocument
from blaym_jupyter.store.search import INDEX_NAME, SqliteSearchIndex

E = "exp-1"


def test_separate_file_is_created(tmp_path):
    idx = SqliteSearchIndex(tmp_path)
    idx.close()
    assert (tmp_path / INDEX_NAME).exists()
    assert INDEX_NAME != "metadata.sqlite3"
    assert not (tmp_path / "metadata.sqlite3").exists()


def test_trigram_partial_match_inside_code(tmp_path):
    idx = SqliteSearchIndex(tmp_path)
    idx.add([SearchDocument(E, "b", 2, "code", "ax.plot(model_residuals_2024)")])
    assert [h.tick for h in idx.query(E, "resid")] == [2]
    assert [h.tick for h in idx.query(E, "RESIDUALS_20")] == [2]
    assert idx.query(E, "nothere") == []
    idx.close()


def test_survives_reopen_and_dropped_file_is_empty_and_works(tmp_path):
    idx = SqliteSearchIndex(tmp_path)
    idx.add([SearchDocument(E, "b", 1, "chat", "hello world")])
    idx.close()
    idx = SqliteSearchIndex(tmp_path)
    assert len(idx.query(E, "hello")) == 1
    idx.close()
    for f in tmp_path.glob(INDEX_NAME + "*"):
        f.unlink()
    idx = SqliteSearchIndex(tmp_path)
    assert idx.query(E, "hello") == []
    idx.add([SearchDocument(E, "b", 1, "chat", "hello again")])
    assert len(idx.query(E, "hello")) == 1
    idx.close()


def test_busy_is_translated(tmp_path):
    idx = SqliteSearchIndex(tmp_path, busy_timeout=0.05)
    other = sqlite3.connect(tmp_path / INDEX_NAME, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(StoreBusy):
        idx.add([SearchDocument(E, "b", 1, "chat", "x")])
    with pytest.raises(StoreBusy):
        idx.remove_exploration(E)
    other.execute("ROLLBACK")
    other.close()
    idx.add([SearchDocument(E, "b", 1, "chat", "now fine")])
    idx.close()

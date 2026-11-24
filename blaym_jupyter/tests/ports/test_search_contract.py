"""The search index contract, run against both implementations (S4, Task 8)."""

from __future__ import annotations

import shutil
import tempfile

import pytest

from blaym_jupyter.ports.search import SearchDocument
from blaym_jupyter.store.search import SqliteSearchIndex
from blaym_jupyter.testing.memory import MemorySearchIndex

E1, E2 = "exp-1", "exp-2"


@pytest.fixture(params=("memory", "sqlite"))
def index(request):
    if request.param == "memory":
        yield MemorySearchIndex()
        return
    d = tempfile.mkdtemp(prefix="blaym-search-")
    idx = SqliteSearchIndex(d)
    yield idx
    idx.close()
    shutil.rmtree(d, ignore_errors=True)


def doc(tick, kind, text, exp=E1, branch="b1"):
    return SearchDocument(exp, branch, tick, kind, text)


def test_add_and_query_returns_branch_tick_kind_snippet(index):
    index.add([doc(3, "code", "plot(residuals)", branch="b2"), doc(4, "chat", "please explain the residuals")])
    hits = index.query(E1, "residuals")
    assert [(h.tick, h.branchId, h.kind) for h in hits] == [(4, "b1", "chat"), (3, "b2", "code")]
    assert all("residuals" in h.snippet for h in hits)


def test_kinds_filter_limit_and_order(index):
    index.add([doc(t, "chat", f"alpha message {t}") for t in range(1, 8)] + [doc(9, "output_text", "alpha out")])
    assert [h.kind for h in index.query(E1, "alpha", kinds=["output_text"])] == ["output_text"]
    assert index.query(E1, "alpha", kinds=["code"]) == []
    hits = index.query(E1, "alpha", limit=3)
    assert [h.tick for h in hits] == [9, 7, 6]


def test_isolation_remove_and_case(index):
    index.add([doc(1, "chat", "Shared Words"), doc(2, "chat", "shared words", exp=E2)])
    assert [h.tick for h in index.query(E1, "SHARED words")] == [1]
    assert [h.tick for h in index.query(E2, "shared")] == [2]
    index.remove_exploration(E1)
    assert index.query(E1, "shared") == []
    assert [h.tick for h in index.query(E2, "shared")] == [2]


def test_short_code_query(index):
    index.add([doc(1, "code", "x = df.a + 1"), doc(2, "code", "y = 2")])
    assert [h.tick for h in index.query(E1, "df")] == [1]
    assert [h.tick for h in index.query(E1, "=")] == [2, 1]


@pytest.mark.parametrize("q", ['say "hi"', '"', '""', "(a OR b)", "a AND b", "NEAR(a b)", "foo*", "col:val",
                               "a OR", "-x", "^y", "'", "%", "_", "\\", "{}", ""])
def test_hostile_queries_never_error_and_stay_plain(index, q):
    index.add([doc(1, "code", 'say "hi" (a OR b) a AND b NEAR(a b) foo* col:val'),
               doc(2, "chat", 'say "hi" (a OR b) a AND b NEAR(a b) foo* col:val'),
               doc(3, "chat", "a b c")])
    hits = index.query(E1, q)
    assert 3 not in [h.tick for h in hits] or q.strip() == ""  # "a OR b" is never read as an operator
    again = index.query(E1, q)
    assert hits == again


def test_operator_words_are_plain_text(index):
    index.add([doc(1, "chat", "apples and oranges"), doc(2, "chat", "apples only")])
    assert [h.tick for h in index.query(E1, "apples OR oranges")] == []
    assert [h.tick for h in index.query(E1, "apples and oranges")] == [1]


@pytest.mark.parametrize("kind", ["chat", "output_text", "chart_text", "table_columns"])
def test_non_code_kinds_match_word_prefixes_not_fragments(index, kind):
    index.add([doc(1, kind, "Residuals plot of y")])
    assert [h.tick for h in index.query(E1, "resid")] == [1]
    assert [h.tick for h in index.query(E1, "RESIDUALS")] == [1]
    assert index.query(E1, "sidual") == []
    assert index.query(E1, "plots") == []


def test_multi_word_queries_are_consecutive_word_prefixes(index):
    index.add([doc(1, "chat", "Residual plot of y"), doc(2, "chat", "residual then plot")])
    assert [h.tick for h in index.query(E1, "residual plot")] == [1]
    assert [h.tick for h in index.query(E1, "resid plo")] == [1]
    assert [h.tick for h in index.query(E1, "resi plot")] == [1]  # every word is a prefix
    assert index.query(E1, "sidual plot") == []
    assert index.query(E1, "plot residual") == []  # in order


def test_code_still_matches_any_substring(index):
    index.add([doc(1, "code", "plot(model_residuals)")])
    assert [h.tick for h in index.query(E1, "sidual")] == [1]
    assert [h.tick for h in index.query(E1, "L_RES")] == [1]

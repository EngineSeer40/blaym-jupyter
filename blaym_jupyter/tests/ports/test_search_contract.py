"""The search index contract (SQLite). The in-memory index is checked by the tests that use it."""

from __future__ import annotations

import pytest

from blaym_jupyter.ports.search import SearchDocument
from blaym_jupyter.store.search import SqliteSearchIndex

E1, E2 = "exp-1", "exp-2"


@pytest.fixture
def index(tmp_path):
    idx = SqliteSearchIndex(tmp_path)
    yield idx
    idx.close()


def doc(tick, kind, text, exp=E1, branch="b1"):
    return SearchDocument(exp, branch, tick, kind, text)


def test_hits_carry_branch_tick_kind_and_snippet_and_respect_filters_isolation_and_removal(index):
    index.add([doc(3, "code", "plot(residuals)", branch="b2"), doc(4, "chat", "please explain the residuals"),
               doc(2, "chat", "Shared Words", exp=E2)] + [doc(t, "chat", f"alpha message {t}") for t in range(5, 9)])
    hits = index.query(E1, "residuals")
    assert [(h.tick, h.branchId, h.kind) for h in hits] == [(4, "b1", "chat"), (3, "b2", "code")]
    assert all("residuals" in h.snippet for h in hits)
    assert [h.kind for h in index.query(E1, "residuals", kinds=["code"])] == ["code"]
    assert [h.tick for h in index.query(E1, "alpha", limit=2)] == [8, 7]
    assert [h.tick for h in index.query(E2, "SHARED")] == [2] and index.query(E1, "shared") == []
    index.remove_exploration(E2)
    assert index.query(E2, "shared") == [] and index.query(E1, "residuals")


def test_text_kinds_match_word_prefixes_in_order_and_code_matches_any_substring(index):
    index.add([doc(1, "chat", "Residual plot of y"), doc(2, "output_text", "residual then plot"),
               doc(3, "code", "plot(model_residuals) + df.a"), doc(4, "code", "y = 2")])
    assert [h.tick for h in index.query(E1, "RESID")] == [3, 2, 1]
    assert [h.tick for h in index.query(E1, "resi plo")] == [1]  # consecutive word prefixes
    assert index.query(E1, "plot residual") == [] and index.query(E1, "plots") == []
    assert [h.tick for h in index.query(E1, "L_RES")] == [3]  # code: substring, even short ones
    assert [h.tick for h in index.query(E1, "df")] == [3] and [h.tick for h in index.query(E1, "=")] == [4]


def test_hostile_queries_never_error_and_operator_words_are_plain_text(index):
    text = 'say "hi" (a OR b) a AND b NEAR(a b) foo* col:val'
    index.add([doc(1, "code", text), doc(2, "chat", text), doc(3, "chat", "a b c"),
               doc(4, "chat", "apples and oranges"), doc(5, "chat", "apples only")])
    for q in ['say "hi"', '"', "(a OR b)", "a AND b", "NEAR(a b)", "foo*", "col:val", "a OR", "-x", "'", "\\", ""]:
        hits = index.query(E1, q)
        assert 3 not in [h.tick for h in hits] or q == ""  # "a OR b" is never read as an operator
        assert index.query(E1, q) == hits
    assert index.query(E1, "apples OR oranges") == []
    assert [h.tick for h in index.query(E1, "apples and oranges")] == [4]

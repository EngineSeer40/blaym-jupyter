"""Search through the service (S4, Task 8): policy, redaction first, rebuild, failures."""

from __future__ import annotations

import random

import pytest
from hypothesis import given, strategies as st
from hypothesis import settings as hsettings

from blaym_jupyter_types import SearchRequest, SetActiveBranchRequest

from blaym_jupyter.local import open_service
from blaym_jupyter.service.auth import FixedTokenAuthorizer, Unauthorized
from blaym_jupyter.tests.history.generate import histories
from blaym_jupyter.tests.history.scenario import Scenario, output_item
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create
from blaym_jupyter.tests.service.test_branches import run_generated
from blaym_jupyter.testing.memory import MemorySearchIndex

SECRET = "ghp_aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"


def make(**kw):
    idx = MemorySearchIndex()
    return Rig(search_index=idx, **kw), idx


def find(rig, text, kinds=None, limit=None):
    return rig.service.search(TOKEN, SearchRequest(explorationId=rig.exp, query=text, kinds=kinds, limit=limit)).hits


def item(mimes, preview, search=None):
    d = output_item(preview)
    d["mimeTypes"] = mimes
    if search:
        d["searchText"] = search
    return d


def story():
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "never_run_version()")
    s.edit("c1", "residual_plot()")
    run = s.run("c1")
    s.output(run, "stdout line")
    for i, it in enumerate([item(["application/vnd.plotly.v1+json"], "{}", "Residual chart title"),
                            item(["application/vnd.dataresource+json"], "{}", "col_alpha col_beta")], start=1):
        s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": i, "item": it},
                revs=s._current_revisions())
    s.chat("m1", "why is the fit poor?")
    return s


def test_run_code_chat_outputs_found_with_kind_and_tick():
    rig, idx = make()
    s = story()
    rig.send(s.events)
    stored = rig.stored()
    run_tick = next(e.tick for e in stored if e.type == "run_started")
    code = find(rig, "residual_plot")
    assert [(h.tick, h.branchId, h.kind) for h in code] == [(run_tick, rig.root, "code")]
    assert find(rig, "never_run_version") == []  # edited, never run: not indexed
    assert find(rig, "x = 1") == []
    assert [h.kind for h in find(rig, "fit poor")] == ["chat"]
    assert [h.kind for h in find(rig, "stdout")] == ["output_text"]
    assert [h.kind for h in find(rig, "chart title")] == ["chart_text"]
    assert [h.kind for h in find(rig, "col_beta")] == ["table_columns"]
    assert find(rig, "residual", kinds=["chart_text"])[0].kind == "chart_text"
    assert len(find(rig, "residual", limit=1)) == 1


def test_every_edit_setting_indexes_unrun_versions():
    rig, idx = make(index_every_edit=True)
    rig.send(story().events)
    assert find(rig, "never_run_version")


def test_planted_key_is_not_findable_anywhere():
    rig, idx = make(index_every_edit=True)
    s = Scenario()
    s.start(cells=[("c1", f"token = '{SECRET}'")])
    s.edit("c1", f"k = '{SECRET}'")
    run = s.run("c1")
    s.output(run, f"printed {SECRET}")
    s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": 1,
                             "item": item(["application/vnd.plotly.v1+json"], "{}", f"title {SECRET}")},
            revs=s._current_revisions())
    s.chat("m1", f"my key is {SECRET}")
    rig.send(s.events)
    assert idx._docs
    assert all(SECRET not in d.text and "ghp_" not in d.text for d in idx._docs)
    assert find(rig, SECRET) == [] and find(rig, "ghp_aB3dE5") == []
    assert find(rig, "hidden")  # the marker itself is searchable text


def test_search_on_a_branch_returns_that_branch():
    rig, idx = make()
    s = Scenario()
    s.start(cells=[("c1", "base = 1")])
    rig.send(s.events)
    root = rig.root
    out = create(rig, root, 1)
    branch = out.branch.branchId
    t = Scenario()
    t.start(cells=[("c1", "base = 1")])
    t.edit("c1", "branch_only_code()")
    t.run("c1")
    rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(
        explorationId=rig.exp, branchId=branch))
    rig.send([t.events[1], t.events[2]])
    hits = find(rig, "branch_only_code")
    assert len(hits) == 1 and hits[0].branchId == branch
    assert hits[0].tick == rig.stored()[-1].tick


def test_duplicate_resend_indexes_once_and_create_branch_indexes_nothing():
    rig, idx = make()
    s = story()
    rig.send(s.events)
    n = len(idx._docs)
    rig.send(s.events)
    assert len(idx._docs) == n
    create(rig, rig.root, 1)
    assert len(idx._docs) == n


class FailingIndex(MemorySearchIndex):
    def add(self, documents):
        raise RuntimeError("index is broken")


def test_index_failure_never_fails_ingest():
    rig = Rig(search_index=FailingIndex())
    out = rig.outcomes(story().events)
    assert all(o.status == "accepted" for o in out)
    assert rig.service.search_errors and isinstance(rig.service.search_errors[0], RuntimeError)
    assert len(rig.stored()) == len(out)


def test_unauthorized_and_no_index():
    rig, idx = make()
    with pytest.raises(Unauthorized):
        rig.service.search("wrong", SearchRequest(explorationId=rig.exp, query="x"))
    with pytest.raises(Unauthorized):
        rig.service.rebuild_search("wrong", rig.exp)
    plain = Rig()
    plain.send(story().events)
    assert plain.service.search(TOKEN, SearchRequest(explorationId=plain.exp, query="x")).hits == []


def snapshot_of(idx):
    return sorted((d.exploration_id, d.branch_id, d.tick, d.kind, d.text) for d in idx._docs)


DOCS_SEEN = {"n": 0}


@hsettings(deadline=None)
@given(h=histories(shapes=("one", "nested", "siblings", "before_parent", "first", "mixed"), max_events=40, service_safe=True),
       seed=st.integers(0, 10_000), every=st.booleans())
def test_rebuild_equals_incremental_for_generated_histories(h, seed, every):
    rig, idx = make(index_every_edit=every, interval=5)
    run_generated(rig, h, random.Random(seed), sample=False)
    before = snapshot_of(idx)
    DOCS_SEEN["n"] += len(before)
    count = rig.service.rebuild_search(TOKEN, rig.exp)
    assert snapshot_of(idx) == before and count == len(before)
    assert not rig.service.search_errors


def test_the_generated_histories_produced_documents():
    assert DOCS_SEEN["n"] > 20


def test_sqlite_service_end_to_end_and_rebuild(tmp_path):
    svc = open_service(tmp_path, FixedTokenAuthorizer(TOKEN))
    try:
        rig = Rig()
        rig.service = svc
        rig.events = svc._events
        rig.send(story().events)
        assert (tmp_path / "search.sqlite3").exists()
        hits = find(rig, "residual")
        assert {h.kind for h in hits} == {"code", "chart_text"}
        assert find(rig, "col_beta")[0].kind == "table_columns"
        assert not svc.search_errors
        before = [(h.tick, h.kind) for h in find(rig, "residual")]
        svc.rebuild_search(TOKEN, rig.exp)
        assert [(h.tick, h.kind) for h in find(rig, "residual")] == before
        assert find(rig, SECRET) == []
    finally:
        svc.close()

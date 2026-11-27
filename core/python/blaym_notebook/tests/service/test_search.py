"""Search through the service: policy, redaction first, rebuild, failures."""

from __future__ import annotations


from blaym_notebook_types import SearchRequest, SetActiveBranchRequest

from blaym_notebook.tests.history.scenario import Scenario, output_item
from blaym_notebook.tests.service.helpers import TOKEN, Rig, create
from blaym_notebook.testing.memory import MemorySearchIndex


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


def test_rebuilding_the_index_from_history_gives_the_same_hits():
    rig, idx = make()
    rig.send(story().events)
    terms = ["residual", "fit poor", "stdout", "chart title", "col_beta", "never_run_version"]
    before = {q: [(h.kind, h.tick) for h in find(rig, q)] for q in terms}
    assert any(before.values())
    assert rig.service.rebuild_search(TOKEN, rig.exp) > 0
    assert {q: [(h.kind, h.tick) for h in find(rig, q)] for q in terms} == before


class FailingIndex(MemorySearchIndex):
    def add(self, documents):
        raise RuntimeError("index is broken")


def test_index_failure_never_fails_ingest():
    rig = Rig(search_index=FailingIndex())
    out = rig.outcomes(story().events)
    assert all(o.status == "accepted" for o in out)
    assert rig.service.search_errors and isinstance(rig.service.search_errors[0], RuntimeError)
    assert len(rig.stored()) == len(out)



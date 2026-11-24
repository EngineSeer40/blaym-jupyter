"""The indexing policy: a pure function (S4, Task 8, decision J)."""

from __future__ import annotations

from blaym_jupyter_types import BlaymEvent, StateAtRequest

from blaym_jupyter.service.indexing import cut_text, documents_for, needs_state, output_kind
from blaym_jupyter.tests.history.scenario import Scenario, output_item
from blaym_jupyter.privacy.redact import MARKER_RE


def history():
    s = Scenario()
    s.start(cells=[("c1", "a = 1")])
    s.edit("c1", "residual_plot()")
    s.insert("c2", "later = 2")
    run = s.run("c1")
    s.output(run, "stdout text")
    s.replace_output(run, "replaced text")
    s.chat("m1", "why the residuals?")
    s.chat_update("m1", "updated chat body")
    s.kernel_start()
    s.gap_start()
    s.gap_end()
    s.rename("analysis.ipynb", "other.ipynb")
    return s


def docs(s, **kw):
    out = {}
    for ev in s.events:
        state = None
        if needs_state(ev, kw.get("index_every_edit", False)):
            state = s.rebuild(ev.branchId, ev.tick).state
        out[ev.tick] = [(d.kind, d.text) for d in documents_for(ev, state, **kw)]
    return {t: v for t, v in out.items() if v}


def test_default_policy_run_chat_outputs_only():
    s = history()
    got = docs(s)
    flat = [x for v in got.values() for x in v]
    assert ("code", "residual_plot()") in flat  # the code at run_started
    assert ("code", "a = 1") not in flat and ("code", "later = 2") not in flat
    assert ("chat", "why the residuals?") in flat and ("chat", "updated chat body") in flat
    assert ("output_text", "stdout text") in flat and ("output_text", "replaced text") in flat
    run_tick = next(e.tick for e in s.events if e.type == "run_started")
    assert got[run_tick] == [("code", "residual_plot()")]
    kinds = {e.type for e in s.events if e.tick in got}
    assert kinds == {"run_started", "output_added", "output_replaced", "chat_message_added", "chat_message_updated"}


def test_every_edit_setting_adds_never_run_versions():
    s = history()
    flat = [x for v in docs(s, index_every_edit=True).values() for x in v]
    for code in ("a = 1", "residual_plot()", "later = 2"):
        assert ("code", code) in flat


def test_output_kinds_and_search_text():
    def item(mimes, preview, search=None):
        d = output_item(preview)
        d["mimeTypes"] = mimes
        if search:
            d["searchText"] = search
        return d
    s = Scenario()
    s.start(cells=[("c1", "x")])
    run = s.run("c1")
    items = [item(["application/vnd.plotly.v1+json"], "{...}", "Residual chart title"),
             item(["application/vnd.dataresource+json"], "{...}", "col_a col_b"),
             item(["text/html", "text/plain"], "<table>", "html cols"),
             item(["image/png"], "binary-ish"),
             item(["text/plain"], "plain preview")]
    for i, it in enumerate(items):
        s._emit("output_added", {"runId": run, "cellId": "c1", "outputIndex": i, "item": it},
                revs=s._current_revisions())
    got = [(d.kind, d.text) for ev in s.events if ev.type == "output_added" for d in documents_for(ev, None)]
    assert got == [("chart_text", "Residual chart title"), ("table_columns", "col_a col_b"),
                   ("table_columns", "html cols"), ("output_text", "plain preview")]
    assert output_kind(["text/plain"]) == "output_text"


def test_inserted_cell_and_snapshot_outputs_are_indexed():
    s = Scenario()
    s.start(cells=[("c1", "snap_code")])
    s.insert("c2", "ins_code", outputs=["pasted result"])
    s.outside_change([("c1", "outside_code")])
    plain = [(d.kind, d.text) for ev in s.events for d in documents_for(ev, None)]
    assert plain == [("output_text", "pasted result")]
    every = [(d.kind, d.text) for ev in s.events for d in documents_for(ev, None, index_every_edit=True)]
    assert ("code", "snap_code") in every and ("code", "ins_code") in every and ("code", "outside_code") in every


def test_text_limit_cuts_at_character_and_before_a_marker():
    assert cut_text("é" * 10, 5) == "éé"
    text = "key=[hidden: api key] tail"
    for limit in range(0, len(text) + 1):
        cut = cut_text(text, limit)
        assert text.startswith(cut) and len(cut.encode()) <= limit
        assert "[hidden" not in cut or MARKER_RE.search(cut)
    assert cut_text(text, 10) == "key="
    s = Scenario()
    s.start(cells=[("c1", "x")])
    s.chat("m1", "ab" * 100)
    ev = s.events[-1]
    assert len(documents_for(ev, None, text_limit=10)[0].text) == 10


def test_nothing_for_other_events():
    s = history()
    for ev in s.events:
        if ev.type in ("recording_gap_started", "recording_gap_ended", "kernel_started", "notebook_renamed"):
            assert documents_for(ev, None) == []

"""Neutral output building (S5, Task 5): items, the assembler, and Jupyter's output area as oracle."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blaym_jupyter.history.apply import step
from blaym_jupyter.recording.outputs import AssembledEvent, OutputAssembler, OutputLimits, item_from_output
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter_types import BlaymEvent

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "recorder" / "outputs"


def is_inline(item) -> bool:  # the S4 rule, written out again so the tests do not borrow the code under test
    return not item.truncated and item.sizeBytes == len(item.preview.encode("utf-8"))


# -- items ----------------------------------------------------------------------------------------

def _cases():
    for name in ("real.json", "handwritten.json"):
        doc = json.loads((FIXTURES / name).read_text())
        for case in doc["cases"]:
            yield pytest.param(case, doc["limits"], id=f"{name.split('.')[0]}: {case['name']}")


@pytest.mark.parametrize("case,doc_limits", list(_cases()))
def test_item_cases(case, doc_limits):
    limits = OutputLimits(inline_bytes={**doc_limits, **case.get("limits", {})}["inlineBytes"])
    before = json.dumps(case["output"], sort_keys=True)
    prepared = item_from_output(case["output"], limits)
    assert json.dumps(case["output"], sort_keys=True) == before  # the input is not changed
    item, exp = prepared.item, case["expect"]
    assert prepared.inline is exp["inline"]
    assert is_inline(item) is exp["inline"]  # the flag and the service's rule agree
    assert (prepared.blob is None) is exp["inline"]
    if exp["inline"]:
        assert item.sizeBytes == len(item.preview.encode())
    else:
        assert item.sizeBytes == len(prepared.blob)
    dumped = item.model_dump(mode="json")
    for field in ("outputType", "mimeTypes", "truncated", "sizeBytes"):
        if field in exp:
            assert dumped[field] == exp[field], field
    if "preview" in exp:
        assert item.preview == exp["preview"]
    if "previewJoinedTraceback" in exp:
        assert item.preview == "\n".join(case["output"]["traceback"])
    if "searchText" in exp:
        assert item.searchText == exp["searchText"]
    if "searchTextStartsWith" in exp:
        assert item.searchText.startswith(exp["searchTextStartsWith"])
    if exp.get("searchTextHasNoEscapes"):
        assert "\x1b" not in item.searchText
    if prepared.blob is not None:  # the blob is the whole output as JSON, with the same hash the item names
        whole = json.loads(prepared.blob)
        assert whole["output_type"] == case["output"]["output_type"]
        assert item.contentHash == "sha256:" + hashlib.sha256(prepared.blob).hexdigest()


def test_unknown_output_type_is_refused():
    with pytest.raises(ValueError):
        item_from_output({"output_type": "mystery"})


def test_blob_is_canonical_and_free_of_transient():
    a = {"output_type": "display_data", "data": {"image/png": "AA", "text/plain": ["x\n", "y"]}, "metadata": {},
         "transient": {"display_id": "d"}}
    b = {"metadata": {}, "data": {"text/plain": "x\ny", "image/png": "AA"}, "output_type": "display_data"}
    pa, pb = item_from_output(a), item_from_output(b)
    assert pa.blob == pb.blob and b"transient" not in pa.blob
    assert pa.item.contentHash == pb.item.contentHash


def test_a_big_image_is_a_blob_with_a_label_and_a_text_preview_is_cut():
    big = {"output_type": "display_data", "data": {"image/png": "A" * 50_000, "text/plain": "T" * 30_000}, "metadata": {}}
    p = item_from_output(big)
    assert not p.inline and p.item.truncated and len(p.item.preview) == 16384 and p.item.sizeBytes == len(p.blob)


# -- the assembler: a fake scheduler, applied to the real rebuilder ---------------------------------

class FakeScheduler:
    def __init__(self):
        self.now = 0
        self.timers: list[list] = []

    def call_later(self, ms, fn):
        entry = [self.now + ms, fn, False]
        self.timers.append(entry)

        def cancel():
            entry[2] = True
        return cancel

    def advance(self, ms):
        end = self.now + ms
        while True:
            due = [t for t in self.timers if not t[2] and t[0] <= end]
            if not due:
                break
            t = min(due, key=lambda x: x[0])
            t[2] = True
            self.now = max(self.now, t[0])
            t[1]()
        self.now = end

    @property
    def pending(self):
        return [t for t in self.timers if not t[2]]


class Rig:
    """An assembler whose events go to the real `history.apply.step` through a Scenario's event builder."""

    def __init__(self, limits=OutputLimits(), flush_ms=250):
        self.scen = Scenario()
        self.scen.start(cells=[("c1", "print(1)")])
        self.run_id = self.scen.run("c1")
        self.state = None
        for ev in self.scen.events:
            self.state = step(self.state, ev)
        self.sched = FakeScheduler()
        self.seen: list[AssembledEvent] = []
        self.asm = OutputAssembler(self.run_id, "c1", self._take, self.sched, limits, flush_ms)

    def _take(self, events):
        for e in events:
            self.seen.append(e)
            ev = self.scen._emit(e.type, e.payload(), revs=self.scen._current_revisions())
            self.state = step(self.state, ev)

    def items(self):
        cell = next(c for c in self.state.notebook.cells if c.cellId == "c1")
        group = next((g for g in cell.outputs if g.runId == self.run_id), None)
        return [] if group is None else [(i.outputType.value, i.preview) for i in group.items]

    def group(self):
        cell = next(c for c in self.state.notebook.cells if c.cellId == "c1")
        return next((g for g in cell.outputs if g.runId == self.run_id), None)


def test_stream_chunks_become_one_item_and_coalesce():
    r = Rig()
    r.asm.message("stream", {"name": "stdout", "text": "a"})
    assert [e.type for e in r.seen] == ["output_added"]
    for t in "bcd":
        r.asm.message("stream", {"name": "stdout", "text": t})
    assert len(r.seen) == 1 and len(r.sched.pending) == 1  # growth waits for the flush interval
    r.sched.advance(249)
    assert len(r.seen) == 1
    r.sched.advance(1)
    assert [e.type for e in r.seen] == ["output_added", "output_replaced"]
    assert r.items() == [("stream", "abcd")]
    r.asm.message("stream", {"name": "stdout", "text": "e"})
    r.asm.finish()  # always flushed at the end
    assert r.items() == [("stream", "abcde")] and not r.sched.pending


def test_stdout_and_stderr_are_separate_items_in_order():
    r = Rig()
    for name, text in [("stdout", "o1"), ("stderr", "e1"), ("stdout", "o2"), ("stdout", "o3")]:
        r.asm.message("stream", {"name": name, "text": text})
    r.asm.finish()
    assert r.items() == [("stream", "o1"), ("stream", "e1"), ("stream", "o2o3")]


def test_display_update_replaces_in_place_and_unknown_ids_are_counted():
    r = Rig()
    r.asm.message("display_data", {"data": {"text/plain": "v1"}, "metadata": {}, "transient": {"display_id": "d"}})
    r.asm.message("stream", {"name": "stdout", "text": "between"})
    r.asm.message("display_data", {"data": {"text/plain": "other"}, "metadata": {}, "transient": {"display_id": "d"}})
    r.asm.message("update_display_data", {"data": {"text/plain": "v2"}, "metadata": {}, "transient": {"display_id": "d"}})
    r.asm.message("update_display_data", {"data": {"text/plain": "?"}, "metadata": {}, "transient": {"display_id": "nope"}})
    r.asm.finish()
    assert r.items() == [("display_data", "v2"), ("stream", "between"), ("display_data", "v2")]
    assert r.asm.unmatched_updates == 1
    assert [e.output_index for e in r.seen if e.type == "output_replaced"] == [0, 2]


def test_clear_without_wait_empties_at_once():
    r = Rig()
    r.asm.message("stream", {"name": "stdout", "text": "old"})
    r.asm.message("clear_output", {"wait": False})
    assert r.items() == [] and r.group().clearPending is None
    r.asm.message("stream", {"name": "stdout", "text": "new"})
    r.asm.finish()
    assert r.items() == [("stream", "new")]


def test_clear_with_wait_keeps_the_old_items_until_the_next_output_then_replaces_the_list():
    r = Rig()
    r.asm.message("stream", {"name": "stdout", "text": "old1"})
    r.asm.message("stream", {"name": "stderr", "text": "old2"})
    r.asm.message("clear_output", {"wait": True})
    assert r.items() == [("stream", "old1"), ("stream", "old2")] and r.group().clearPending is True
    r.asm.message("stream", {"name": "stderr", "text": "new"})  # same name as the last old item: still a new item
    assert r.items() == [("stream", "new")] and r.group().clearPending is None
    assert r.seen[-1].type == "output_added" and r.seen[-1].output_index == 0
    r.asm.message("stream", {"name": "stderr", "text": "er"})
    r.asm.finish()
    assert r.items() == [("stream", "newer")]


def test_clear_wait_then_nothing_leaves_the_flag_set():
    r = Rig()
    r.asm.message("display_data", {"data": {"text/plain": "x"}, "metadata": {}})
    r.asm.message("clear_output", {"wait": True})
    r.asm.finish()
    assert r.group().clearPending is True and r.items() == [("display_data", "x")]


def test_update_while_a_wait_clear_is_pending_still_updates_the_old_item():
    r = Rig()
    r.asm.message("display_data", {"data": {"text/plain": "a"}, "metadata": {}, "transient": {"display_id": "d"}})
    r.asm.message("clear_output", {"wait": True})
    r.asm.message("update_display_data", {"data": {"text/plain": "b"}, "metadata": {}, "transient": {"display_id": "d"}})
    assert r.items() == [("display_data", "b")]
    r.asm.message("display_data", {"data": {"text/plain": "c"}, "metadata": {}, "transient": {"display_id": "d"}})
    r.asm.message("update_display_data", {"data": {"text/plain": "c2"}, "metadata": {}, "transient": {"display_id": "d"}})
    assert r.items() == [("display_data", "c2")]  # the old id's index is forgotten with the old list


def test_pending_stream_growth_is_flushed_before_a_clear():
    r = Rig()
    r.asm.message("stream", {"name": "stdout", "text": "a"})
    r.asm.message("stream", {"name": "stdout", "text": "b"})
    r.asm.message("clear_output", {"wait": False})
    assert [e.type for e in r.seen] == ["output_added", "output_replaced", "outputs_cleared"]
    assert r.items() == [] and not r.sched.pending


def test_error_and_execute_result_are_appended_and_unknown_messages_ignored():
    r = Rig()
    r.asm.message("execute_result", {"execution_count": 2, "data": {"text/plain": "5"}, "metadata": {}})
    r.asm.message("error", {"ename": "E", "evalue": "v", "traceback": ["tb"]})
    r.asm.message("status", {"state": "busy"})
    r.asm.finish()
    r.asm.message("stream", {"name": "stdout", "text": "late"})  # after the end: ignored, counted
    assert r.items() == [("execute_result", "5"), ("error", "tb")]
    assert r.asm.ignored_messages == 2


def test_stream_stops_growing_at_the_preview_limit_and_is_marked_truncated():
    r = Rig(limits=OutputLimits(inline_bytes=8, preview_bytes=20))
    for _ in range(10):
        r.asm.message("stream", {"name": "stdout", "text": "0123456789"})
    r.asm.finish()
    item = r.group().items[0]
    last_blob = [e.prepared.blob for e in r.seen if e.prepared][-1]
    assert json.loads(last_blob)["text"] == "01234567890123456789"  # 20 bytes kept, 80 counted
    assert item.truncated and item.sizeBytes == len(last_blob) + 80 and item.preview == "01234567"
    assert not is_inline(item)


def test_size_only_growth_after_the_limit_is_written_once_at_the_end():
    r = Rig(limits=OutputLimits(inline_bytes=4, preview_bytes=5))
    r.asm.message("stream", {"name": "stdout", "text": "abcde"})
    r.sched.advance(300)
    n = len(r.seen)
    for _ in range(50):
        r.asm.message("stream", {"name": "stdout", "text": "zz"})
        r.sched.advance(300)
    assert len(r.seen) == n  # the kept text did not change: nothing to rewrite
    r.asm.finish()
    assert len(r.seen) == n + 1 and r.group().items[0].sizeBytes == len(r.seen[-1].prepared.blob) + 100


# -- an independent oracle: Jupyter's output area as a plain list ------------------------------------

def oracle(messages):
    """The final outputs (type, text) the way JupyterLab's output area builds them. No events, no indexes."""
    outputs: list[dict] = []
    clear_next = False
    for kind, a, b in messages:
        if kind == "clear":
            if a:
                clear_next = True
            else:
                outputs, clear_next = [], False
            continue
        if kind == "update":
            for o in outputs:
                if o.get("id") == a:
                    o["text"] = b
            continue
        if clear_next:
            outputs, clear_next = [], False
        if kind == "stream":
            if outputs and outputs[-1]["type"] == "stream" and outputs[-1]["name"] == a:
                outputs[-1]["text"] += b
            else:
                outputs.append({"type": "stream", "name": a, "text": b})
        elif kind == "display":
            outputs.append({"type": "display_data", "id": a, "text": b})
        elif kind == "result":
            outputs.append({"type": "execute_result", "text": b})
        elif kind == "error":
            outputs.append({"type": "error", "text": b})
    return [(o["type"], o["text"]) for o in outputs]


def to_message(m):
    kind, a, b = m
    if kind == "stream":
        return "stream", {"name": a, "text": b}
    if kind == "display":
        c = {"data": {"text/plain": b}, "metadata": {}}
        if a:
            c["transient"] = {"display_id": a}
        return "display_data", c
    if kind == "update":
        return "update_display_data", {"data": {"text/plain": b}, "metadata": {}, "transient": {"display_id": a}}
    if kind == "result":
        return "execute_result", {"execution_count": 1, "data": {"text/plain": b}, "metadata": {}}
    if kind == "error":
        return "error", {"ename": "E", "evalue": b, "traceback": [b]}
    return "clear_output", {"wait": a}


text = st.text(alphabet="abcxyz \nüé", min_size=1, max_size=6)
display_ids = st.sampled_from(["d1", "d2"])
message = st.one_of(
    st.tuples(st.just("stream"), st.sampled_from(["stdout", "stderr"]), text),
    st.tuples(st.just("display"), st.one_of(st.just(""), display_ids), text),
    st.tuples(st.just("update"), display_ids, text),
    st.tuples(st.just("result"), st.just(""), text),
    st.tuples(st.just("error"), st.just(""), text),
    st.tuples(st.just("clear"), st.booleans(), st.just("")),
)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(message, st.integers(0, 400)), max_size=40), st.sampled_from([0, 250]))
def test_assembler_matches_the_oracle_on_generated_sequences(steps, flush_ms):
    r = Rig(flush_ms=flush_ms)
    msgs = []
    for m, wait_ms in steps:
        msgs.append(m)
        r.asm.message(*to_message(m))
        r.sched.advance(wait_ms)  # timers fire in between, at random
    r.asm.finish()
    assert r.items() == oracle(msgs)
    # the clear-pending flag at the end equals the oracle's
    clear_next = False
    for kind, a, _ in msgs:
        if kind == "clear":
            clear_next = bool(a)
        elif kind not in ("update",):
            clear_next = False
    group = r.group()
    flag = bool(group and group.clearPending)
    assert flag == clear_next, (msgs, flag)
    assert not r.sched.pending


# -- bounded memory ---------------------------------------------------------------------------------

def test_a_100mb_stream_stays_bounded():
    chunk = "x" * 65536
    sched = FakeScheduler()
    asm = OutputAssembler("run", "c", lambda events: None, sched, OutputLimits())
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        for i in range(1600):  # 100 MiB
            asm.message("stream", {"name": "stdout", "text": chunk})
            if i % 100 == 0:
                sched.advance(300)
        asm.finish()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak - base < 40 * 1024 * 1024, peak - base
    assert asm._stream.total == 1600 * 65536 and asm._stream.kept <= OutputLimits().preview_bytes


def test_flush_interval_zero_still_orders_events():
    r = Rig(flush_ms=0)
    r.asm.message("stream", {"name": "stdout", "text": "a"})
    r.asm.message("stream", {"name": "stdout", "text": "b"})
    r.sched.advance(0)
    assert r.items() == [("stream", "ab")]

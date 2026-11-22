"""Readable scenario builder for S3a history tests.

A scenario reads like the sequence it describes::

    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    s.edit("c1", "x = 2")
    run = s.run("c1")
    s.output(run, "2")
    s.finish(run)
    child = s.branch_from(tick=3, name="try-y")   # now working on the child
    s.edit("c1", "x = 3")
    s.on("main")
    s.expect_at("main", 5, cells=[("c1", "x = 2")])

Every event is built as plain data, validated through the generated contract
models, given a dense tick (1, 2, 3 ... across all branches) and the right
branch. Cell revisions and run counts are tracked per branch line so an edit gets
the next revision by default; pass `revision=` to make a deliberately wrong one.
Models are never defined here.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Callable

from blaym_jupyter_types import BlaymEvent

from blaym_jupyter.history.reference import reference_rebuild

NAMESPACE = uuid.UUID("5c1f0a6e-0000-4000-8000-00000000b1a7")
EXPLORATION = "exploration-1"


def output_item(text: str, kind: str = "stream") -> dict:
    mime = {"stream": "text/plain", "error": "text/plain"}.get(kind, "text/plain")
    return {
        "outputType": kind,
        "mimeTypes": [mime],
        "contentHash": "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16],
        "preview": text,
        "truncated": False,
        "sizeBytes": len(text.encode()),
    }


class Scenario:
    def __init__(self, rebuild: Callable | None = None, root: str = "main"):
        self.root = root
        self.current = root
        self.events: list = []
        self._tick = 0
        self._runs = 0
        self._run_cell: dict[str, str] = {}
        self._run_count: dict[str, int] = {}
        # per branch: parent, parentTick, and (tick, revisions) entries
        self._parent: dict[str, tuple[str, int] | None] = {root: None}
        self._revs: dict[str, list[tuple[int, dict[str, int]]]] = {root: []}
        self._rebuild = rebuild or reference_rebuild

    # -- plumbing ---------------------------------------------------------------

    @property
    def tick(self) -> int:
        """The tick of the last event made."""
        return self._tick

    def _revisions_at(self, branch: str, tick: int) -> dict[str, int]:
        for t, revs in reversed(self._revs[branch]):
            if t <= tick:
                return dict(revs)
        parent = self._parent[branch]
        if parent is None:
            return {}
        return self._revisions_at(parent[0], min(tick, parent[1]))

    def _current_revisions(self) -> dict[str, int]:
        return self._revisions_at(self.current, self._tick)

    def _emit(self, type_: str, payload: dict, *, branch: str | None = None,
              revs: dict[str, int] | None = None, **extra):
        branch = branch or self.current
        self._tick += 1
        raw = {
            "schemaVersion": 2,
            "eventId": str(uuid.uuid5(NAMESPACE, f"{EXPLORATION}:{self._tick}")),
            "explorationId": EXPLORATION,
            "branchId": branch,
            "tick": self._tick,
            "wallTimeMs": 1_760_000_000_000 + self._tick,
            "userId": "local",
            "workspaceId": "local",
            "originId": "origin-1",
            "type": type_,
            "payload": payload,
            **extra,
        }
        event = BlaymEvent.model_validate(raw).root
        self.events.append(event)
        if revs is not None:
            self._revs[branch].append((self._tick, revs))
        return event

    @staticmethod
    def _cell(cell_id, code, cell_type="code", revision=1) -> dict:
        return {"cellId": cell_id, "cellType": cell_type, "code": code,
                "revision": revision, "metadata": {}, "outputs": []}

    # -- notebook ---------------------------------------------------------------

    def start(self, cells=(), path="analysis.ipynb", kernel="python3", language="python"):
        """First event of the root branch: a snapshot. `cells` are (id, code) pairs."""
        cell_list = [self._cell(c, code) for c, code in cells]
        revs = {c["cellId"]: 1 for c in cell_list}
        return self._emit("notebook_snapshot", {
            "path": path, "notebookMetadata": {}, "kernelName": kernel,
            "language": language, "cells": cell_list}, revs=revs)

    def snapshot(self, cells, path="analysis.ipynb"):
        """A recovery snapshot after a gap (same event as `start`)."""
        return self.start(cells=cells, path=path)

    def insert(self, cell_id, code="", index=None, cell_type="code"):
        revs = self._current_revisions()
        index = len(revs) if index is None else index
        revs[cell_id] = 1
        return self._emit("cell_inserted", {
            "cellId": cell_id, "index": index, "cellType": cell_type,
            "code": code, "revision": 1}, revs=revs)

    def delete(self, cell_id):
        revs = self._current_revisions()
        revs.pop(cell_id, None)
        return self._emit("cell_deleted", {"cellId": cell_id}, revs=revs)

    def move(self, cell_id, from_index, to_index):
        return self._emit("cell_moved", {
            "cellId": cell_id, "fromIndex": from_index, "toIndex": to_index},
            revs=self._current_revisions())

    def change_type(self, cell_id, cell_type):
        return self._emit("cell_type_changed", {"cellId": cell_id, "cellType": cell_type},
                          revs=self._current_revisions())

    def edit(self, cell_id, code, revision=None):
        revs = self._current_revisions()
        revision = revs.get(cell_id, 0) + 1 if revision is None else revision
        revs[cell_id] = revision
        return self._emit("cell_source_changed", {
            "cellId": cell_id, "code": code, "revision": revision}, revs=revs)

    def cell_metadata(self, cell_id, metadata):
        return self._emit("cell_metadata_changed", {"cellId": cell_id, "metadata": metadata},
                          revs=self._current_revisions())

    def notebook_metadata(self, metadata):
        return self._emit("notebook_metadata_changed", {"metadata": metadata},
                          revs=self._current_revisions())

    def rename(self, old_path, new_path):
        return self._emit("notebook_renamed", {"oldPath": old_path, "newPath": new_path},
                          revs=self._current_revisions())

    def copy(self, old_path, new_path, new_exploration="exploration-2"):
        return self._emit("notebook_copied", {
            "oldPath": old_path, "newPath": new_path, "newExplorationId": new_exploration},
            revs=self._current_revisions())

    def outside_change(self, cells, path="analysis.ipynb"):
        cell_list = [self._cell(c, code) for c, code in cells]
        return self._emit("changed_outside_jupyter", {
            "path": path, "notebookMetadata": {}, "kernelName": "python3",
            "language": "python", "cells": cell_list},
            revs={c["cellId"]: 1 for c in cell_list}, authorHint="outside")

    # -- runs and outputs -------------------------------------------------------

    def run(self, cell_id, revision=None) -> str:
        """Start a run of the cell's current revision (or `revision`). Returns the run id."""
        revs = self._current_revisions()
        self._runs += 1
        run_id = f"run-{self._runs}"
        count = self._run_count.get(f"{self.current}/{cell_id}", 0) + 1
        self._run_count[f"{self.current}/{cell_id}"] = count
        self._run_cell[run_id] = cell_id
        self._emit("run_started", {
            "runId": run_id, "cellId": cell_id,
            "cellRevision": revs.get(cell_id, 1) if revision is None else revision,
            "runCount": count}, revs=revs)
        return run_id

    def finish(self, run_id, status="ok"):
        return self._emit("run_finished", {"runId": run_id, "status": status},
                          revs=self._current_revisions())

    def output(self, run_id, text, index=0, kind="stream"):
        return self._emit("output_added", {
            "runId": run_id, "cellId": self._run_cell[run_id],
            "outputIndex": index, "item": output_item(text, kind)},
            revs=self._current_revisions())

    def replace_output(self, run_id, text, index=0, kind="stream"):
        return self._emit("output_replaced", {
            "runId": run_id, "cellId": self._run_cell[run_id],
            "outputIndex": index, "item": output_item(text, kind)},
            revs=self._current_revisions())

    def clear_outputs(self, run_id, wait=False):
        return self._emit("outputs_cleared", {
            "runId": run_id, "cellId": self._run_cell[run_id], "wait": wait},
            revs=self._current_revisions())

    # -- kernel, chat, gaps -----------------------------------------------------

    def kernel_start(self, kernel_id="kernel-1"):
        return self._emit("kernel_started", {
            "kernelId": kernel_id, "kernelName": "python3", "language": "python"},
            revs=self._current_revisions())

    def kernel_restart(self, kernel_id="kernel-2", reason="requested"):
        return self._emit("kernel_restarted", {"kernelId": kernel_id, "reason": reason},
                          revs=self._current_revisions())

    def chat(self, message_id, body, sender="user-1", kind="person", chat_time_ms=0):
        return self._emit("chat_message_added", {
            "messageId": message_id, "sender": sender, "senderKind": kind,
            "body": body, "chatTimeMs": chat_time_ms}, revs=self._current_revisions())

    def chat_update(self, message_id, body):
        return self._emit("chat_message_updated", {"messageId": message_id, "body": body},
                          revs=self._current_revisions())

    def gap_start(self, gap_id="gap-1", source="browser", reason="no_client"):
        return self._emit("recording_gap_started", {
            "gapId": gap_id, "source": source, "reason": reason},
            revs=self._current_revisions())

    def gap_end(self, gap_id="gap-1"):
        return self._emit("recording_gap_ended", {"gapId": gap_id},
                          revs=self._current_revisions())

    # -- branches ---------------------------------------------------------------

    def branch_from(self, tick: int, name: str, parent: str | None = None,
                    branch_id: str | None = None) -> str:
        """Create a branch starting after `tick` of `parent` (default: the branch
        being worked on) and switch to it. Returns the new branch id."""
        parent = parent or self.current
        branch_id = branch_id or name
        self._parent[branch_id] = (parent, tick)
        self._revs[branch_id] = []
        revs = self._revisions_at(parent, tick)
        self._emit("branch_point_created", {
            "parentBranchId": parent, "parentTick": tick, "name": name},
            branch=branch_id, revs=revs)
        self.current = branch_id
        return branch_id

    def on(self, branch: str) -> "Scenario":
        """Work on `branch` from here on."""
        self.current = branch
        return self

    # -- expectations -----------------------------------------------------------

    def rebuild(self, branch: str, tick: int):
        return self._rebuild(self.events, branch, tick)

    def state_at(self, branch: str, tick: int):
        result = self.rebuild(branch, tick)
        assert result.status == "ok", f"expected a state, got {result}"
        return result.state

    def expect_at(self, branch: str, tick: int, *, cells=None, chat=None, path=None,
                  runs=None, outputs=None, kernel_status=None, status="ok",
                  position=None):
        """Assert what the state at (branch, tick) holds.

        cells: [(id, code)] in order; chat: [(message id, body)]; runs: {run id: status};
        outputs: {cell id: [previews of the latest run]}; position: (branchId, tick asked, lastEventTick, branchCount);
        status: expected result kind (default "ok").
        """
        result = self.rebuild(branch, tick)
        assert result.status == status, f"at {branch}@{tick}: got {result}"
        if status != "ok":
            return result
        s = result.state
        if cells is not None:
            assert [(c.cellId, c.code) for c in s.notebook.cells] == list(cells)
        if chat is not None:
            assert [(m.messageId, m.body) for m in s.chat] == list(chat)
        if path is not None:
            assert s.notebook.path == path
        if runs is not None:
            assert {r.runId: r.status.root for r in s.runs} == runs
        if outputs is not None:
            got = {c.cellId: [i.preview for i in (c.outputs[-1].items if c.outputs else [])]
                   for c in s.notebook.cells}
            for cell_id, previews in outputs.items():
                assert got[cell_id] == previews, f"{cell_id}: {got[cell_id]} != {previews}"
        if kernel_status is not None:
            assert s.kernel.status.value == kernel_status
        if position is not None:
            p = s.position
            assert (p.branchId, p.tick, p.lastEventTick, p.branchCount) == position
        return result

"""The same scripted session recorded in JupyterLab and in VS Code rebuilds alike.

The fixtures hold each editor's stored events (`jupyter/ui-tests/tests/cross-editor.spec.ts` and
`BLAYM_EXPORT=1 vscode/test/run.sh`). Ids, `editor`, `originId`, times, `codeLink` and the `wait` flag are
ignored.

Compared: the kinds of event; the rebuilt notebook and runs after the last step both sessions share (the
rename); and the one outside write, as written to disk. The final states are not compared: after an outside
write JupyterLab keeps showing the old content (its resync snapshot records what the editor shows), while
VS Code reloads a clean editor. Gaps are left out of the kinds: the JupyterLab session reloaded its page
after the kernel restart (a headless-browser quirk), so only it records a gap.
"""
from __future__ import annotations

import json
from pathlib import Path

from blaym_notebook.history import Rebuilder
from blaym_notebook_types import BlaymEvent

FIXTURES = Path(__file__).resolve().parents[5] / "fixtures" / "recorder" / "cross-editor"
GAPS = {"recording_gap_started", "recording_gap_ended"}


def load(name: str):
    events = [BlaymEvent.model_validate(e).root for e in json.loads((FIXTURES / name).read_text())]
    rebuilder = Rebuilder()
    rebuilder.add(events)
    branch = events[0].branchId
    renamed = next(e.tick for e in events if e.type == "notebook_renamed")
    result = rebuilder.state_at(branch, renamed)
    assert result.status == "ok", result
    state = result.state
    cells = [(c.cellType, c.code, [i.mimeTypes for g in c.outputs for i in g.items]) for c in state.notebook.cells]
    runs = [r.status for r in sorted(state.runs, key=lambda r: r.startedTick)]
    outside = [e for e in events if e.type == "changed_outside_editor"]
    assert len(outside) == 1, name
    written = [(c.cellType, c.code) for c in outside[0].payload.cells]
    return {e.type for e in events} - GAPS, cells, runs, written


def test_jupyterlab_and_vscode_sessions_rebuild_alike():
    lab, code = load("jupyterlab.json"), load("vscode.json")
    assert lab[0] == code[0], ("event kinds", lab[0] ^ code[0])
    assert lab[1] == code[1], "notebook after the rename"
    assert lab[2] == code[2], "run statuses"
    assert lab[3] == code[3], "the outside write"

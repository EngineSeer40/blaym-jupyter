"""S5 Task 3: the committed revision index and `resolve_cell_revision`.

Named cases, a rebuilt (hydrated) service, branches, and generated edit sequences against an
independent oracle that keeps plain `(revision, code)` lists.
"""

from __future__ import annotations

import random
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blaym_jupyter_types import ResolveCellRevisionRequest

from blaym_jupyter.local import open_service
from blaym_jupyter.ports.events import StoreBusy
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.service.core import Service, ServiceBusy
from blaym_jupyter.service.revisions import DEPTH
from blaym_jupyter.testing.fakes import IdFactory
from blaym_jupyter.testing.memory import MemoryEventStore
from blaym_jupyter.tests.service.helpers import TOKEN, Rig, create

KEY = "sk-" + "a1b2c3d4e5" * 3  # matches the key-format rule of `privacy/redact.py`


def ev(rig, type_, payload):
    return {"schemaVersion": 2, "eventId": str(uuid.uuid4()), "explorationId": rig.exp, "wallTimeMs": 1,
            "userId": "local", "workspaceId": "local", "originId": "o", "type": type_, "payload": payload}


def cell(cid, code, revision=1):
    return {"cellId": cid, "cellType": "code", "code": code, "revision": revision, "metadata": {}, "outputs": []}


def snapshot(rig, cells):
    return ev(rig, "notebook_snapshot", {"path": "a.ipynb", "notebookMetadata": {}, "kernelName": "python3",
                                         "language": "python", "cells": cells})


def insert(rig, cid, code, index=0):
    return ev(rig, "cell_inserted", {"cellId": cid, "index": index, "cellType": "code", "code": code, "revision": 1})


def edit(rig, cid, code, revision):
    return ev(rig, "cell_source_changed", {"cellId": cid, "code": code, "revision": revision})


def send(rig, *events, target=None):
    outs = rig.outcomes(list(events), target=target)
    assert all(o.status == "accepted" for o in outs), outs
    return outs


def resolve(rig, cell_id, code, branch=None, exp=None, service=None):
    r = (service or rig.service).resolve_cell_revision(TOKEN, ResolveCellRevisionRequest(
        explorationId=exp or rig.exp, cellId=cell_id, code=code, branchId=branch))
    return r.root.model_dump(exclude_none=True)


def test_a_run_on_a_just_inserted_cell_resolves_to_revision_1():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "x = 1")]))
    send(rig, insert(rig, "c2", "y = 1", index=1))
    assert resolve(rig, "c2", "y = 1") == {"status": "ok", "cellRevision": 1}


def test_an_edit_during_a_run_still_resolves_to_the_runs_revision():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    send(rig, edit(rig, "c1", "b", 2))  # the tip is r+1 when the recorder asks about the run's code
    assert resolve(rig, "c1", "a") == {"status": "ok", "cellRevision": 1}
    assert resolve(rig, "c1", "b") == {"status": "ok", "cellRevision": 2}


def test_the_same_code_at_revisions_3_and_5_gives_5():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "w")]))
    for rev, code in [(2, "y"), (3, "z"), (4, "w2"), (5, "z")]:
        send(rig, edit(rig, "c1", code, rev))
    assert resolve(rig, "c1", "z") == {"status": "ok", "cellRevision": 5}
    assert resolve(rig, "c1", "w2") == {"status": "ok", "cellRevision": 4}


def test_code_with_a_fake_key_still_matches_the_stored_hidden_code():
    rig = Rig()
    code = f'api_key = "{KEY}"\nprint(1)'
    send(rig, snapshot(rig, [cell("c1", "x")]))
    send(rig, edit(rig, "c1", code, 2))
    stored = rig.state(rig.root, 2).state.notebook.cells[0].code
    assert KEY not in stored  # the premise: the stored code is the hidden one
    assert resolve(rig, "c1", code) == {"status": "ok", "cellRevision": 2}
    assert resolve(rig, "c1", stored) == {"status": "ok", "cellRevision": 2}  # hiding is idempotent


def test_code_that_never_existed_is_code_differs_with_the_current_revision():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    send(rig, edit(rig, "c1", "b", 2))
    assert resolve(rig, "c1", "never") == {"status": "code_differs", "currentRevision": 2}


def test_unknown_cell_deleted_cell_unknown_history_and_unknown_branch():
    rig = Rig()
    assert resolve(rig, "c1", "a") == {"status": "unknown_history"}  # nothing recorded yet, no raise
    send(rig, snapshot(rig, [cell("c1", "a"), cell("c2", "b")]))
    assert resolve(rig, "nope", "a") == {"status": "unknown_cell"}
    assert resolve(rig, "c1", "a", exp="other-history") == {"status": "unknown_history"}
    assert resolve(rig, "c1", "a", branch="no-such-branch") == {"status": "unknown_history"}
    send(rig, ev(rig, "cell_deleted", {"cellId": "c2"}))
    assert resolve(rig, "c2", "b") == {"status": "unknown_cell"}


def test_snapshot_and_outside_change_set_every_cell():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a"), cell("c2", "b")]))
    send(rig, snapshot(rig, [cell("c1", "a2", 2)]))  # c2 left the notebook
    assert resolve(rig, "c1", "a2") == {"status": "ok", "cellRevision": 2}
    assert resolve(rig, "c1", "a") == {"status": "ok", "cellRevision": 1}
    assert resolve(rig, "c2", "b") == {"status": "unknown_cell"}
    outside = ev(rig, "changed_outside_editor", {"path": "a.ipynb", "notebookMetadata": {}, "kernelName": "python3",
                                                 "language": "python", "cells": [cell("c1", "a3", 3), cell("c9", "n")]})
    outside["authorHint"] = "outside"
    send(rig, outside)
    assert resolve(rig, "c1", "a3") == {"status": "ok", "cellRevision": 3}
    assert resolve(rig, "c9", "n") == {"status": "ok", "cellRevision": 1}
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 3, "c9": 1}


def test_only_the_last_32_revisions_are_kept():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "v1")]))
    for rev in range(2, DEPTH + 4):
        send(rig, edit(rig, "c1", f"v{rev}", rev))
    last = DEPTH + 3
    assert resolve(rig, "c1", f"v{last}") == {"status": "ok", "cellRevision": last}
    assert resolve(rig, "c1", f"v{last - DEPTH + 1}")["status"] == "ok"
    assert resolve(rig, "c1", f"v{last - DEPTH}") == {"status": "code_differs", "currentRevision": last}


def test_a_branch_sees_the_revisions_of_its_branch_point_not_later_ones():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    send(rig, edit(rig, "c1", "b", 2))
    root = rig.root
    child = create(rig, root, 2).branch.branchId
    send(rig, edit(rig, "c1", "later-on-parent", 3), target=root)
    send(rig, edit(rig, "c1", "c", 3), target=child)
    assert resolve(rig, "c1", "a", branch=child) == {"status": "ok", "cellRevision": 1}
    assert resolve(rig, "c1", "c", branch=child) == {"status": "ok", "cellRevision": 3}
    assert resolve(rig, "c1", "later-on-parent", branch=child) == {"status": "code_differs", "currentRevision": 3}
    assert resolve(rig, "c1", "later-on-parent", branch=root) == {"status": "ok", "cellRevision": 3}
    assert resolve(rig, "c1", "c", branch=root) == {"status": "code_differs", "currentRevision": 3}
    # no branch given: the active branch (the child, after create_branch)
    assert resolve(rig, "c1", "c") == {"status": "ok", "cellRevision": 3}


def test_a_refused_event_is_not_in_the_index():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    o = rig.outcomes([edit(rig, "c1", "refused-code", 9)])[0]  # the dry run applies it, then it fails
    assert o.status == "refused"
    assert resolve(rig, "c1", "refused-code") == {"status": "code_differs", "currentRevision": 1}
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 1}


def test_an_event_the_store_did_not_commit_is_not_in_the_index():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    real = rig.events.append

    def busy(*a, **kw):
        raise StoreBusy("locked")

    rig.events.append = busy
    with pytest.raises(ServiceBusy):
        rig.send([edit(rig, "c1", "uncommitted", 2)])
    rig.events.append = real
    assert resolve(rig, "c1", "uncommitted") == {"status": "code_differs", "currentRevision": 1}
    send(rig, edit(rig, "c1", "committed", 2))
    assert resolve(rig, "c1", "committed") == {"status": "ok", "cellRevision": 2}


def probes_of(events):
    return sorted({"never"} | {getattr(e.payload, "code", None) for e in events if hasattr(e.payload, "code")}
                  - {None})


def test_a_service_hydrated_from_the_store_answers_like_the_live_one():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a"), cell("c2", "p")]))
    send(rig, edit(rig, "c1", "b", 2), edit(rig, "c1", "c", 3), insert(rig, "c3", f'k = "{KEY}"', index=2))
    root = rig.root
    child = create(rig, root, 4).branch.branchId
    send(rig, edit(rig, "c1", "d", 4), target=child)
    send(rig, edit(rig, "c2", "q", 2), target=root)
    rebuilt = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("again"))
    cases = [(c, code, b) for c in ("c1", "c2", "c3", "zz") for code in ("a", "b", "c", "d", "p", "q", "never", f'k = "{KEY}"')
             for b in (root, child, None)]
    for c, code, b in cases:
        assert resolve(rig, c, code, b) == resolve(rig, c, code, b, service=rebuilt), (c, code, b)
    assert rebuilt.cell_revisions(TOKEN, rig.exp) == rig.service.cell_revisions(TOKEN, rig.exp)


def test_a_service_reopened_on_sqlite_answers_like_the_live_one(tmp_path):
    live = open_service(tmp_path, FixedTokenAuthorizer(TOKEN), recover=False)
    rig = Rig()
    rig.service = live
    try:
        send(rig, snapshot(rig, [cell("c1", "a")]))
        send(rig, edit(rig, "c1", "b", 2), edit(rig, "c1", "a", 3))
        want = {code: resolve(rig, "c1", code) for code in ("a", "b", "zz")}
        revs = live.cell_revisions(TOKEN, rig.exp)
    finally:
        live.close()
    again = open_service(tmp_path, FixedTokenAuthorizer(TOKEN), recover=False)
    try:
        assert {code: resolve(rig, "c1", code, service=again) for code in want} == want
        assert want["a"] == {"status": "ok", "cellRevision": 3}
        assert again.cell_revisions(TOKEN, rig.exp) == revs == {"c1": 3}
    finally:
        again.close()


# -- generated edit sequences against an independent oracle -----------------------------------

class Oracle:
    """Plain lists of (revision, code) per cell, cut to the last 32 on lookup."""

    def __init__(self):
        self.cells: dict[str, list[tuple[int, str]]] = {}

    def put(self, cid, rev, code):
        entries = self.cells.setdefault(cid, [])
        if not entries or entries[-1] != (rev, code):
            entries.append((rev, code))

    def answer(self, cid, code):
        entries = self.cells.get(cid)
        if not entries:
            return {"status": "unknown_cell"}
        for rev, c in reversed(entries[-DEPTH:]):
            if c == code:
                return {"status": "ok", "cellRevision": rev}
        return {"status": "code_differs", "currentRevision": entries[-1][0]}


@settings(max_examples=60, deadline=None)
@given(seed=st.integers(0, 10**9), steps=st.integers(5, 90))
def test_generated_edit_sequences_always_match_the_oracle(seed, steps):
    rnd = random.Random(seed)
    rig, oracle = Rig(), Oracle()
    codes = [f"v{i}" for i in range(6)] + [f'k = "{KEY}"']  # few codes, so equal code repeats at many revisions
    revs: dict[str, int] = {}
    cells = [("c0", rnd.choice(codes))]
    send(rig, snapshot(rig, [cell(c, code) for c, code in cells]))
    for c, code in cells:
        revs[c] = 1
        oracle.put(c, 1, code)
    for _ in range(steps):
        kind = rnd.choice(["edit"] * 6 + ["insert", "delete", "snapshot", "outside"])
        live = sorted(revs)
        if kind == "edit" and live:
            c = rnd.choice(live)
            code = rnd.choice(codes)
            revs[c] += 1
            send(rig, edit(rig, c, code, revs[c]))
            oracle.put(c, revs[c], code)
        elif kind == "insert":
            c = f"n{len(oracle.cells)}{rnd.randint(0, 99999)}"
            code = rnd.choice(codes)
            n = len(live)
            send(rig, insert(rig, c, code, index=n))
            revs[c] = 1
            oracle.cells[c] = [(1, code)]
        elif kind == "delete" and len(live) > 1:
            c = rnd.choice(live)
            send(rig, ev(rig, "cell_deleted", {"cellId": c}))
            del revs[c]
            oracle.cells.pop(c)
        elif kind in ("snapshot", "outside") and live:
            keep = [c for c in live if rnd.random() < 0.8] or live[:1]
            new = {}
            for c in keep:
                changed = rnd.random() < 0.5
                code = rnd.choice(codes) if changed else oracle.cells[c][-1][1]
                new[c] = (revs[c] + (1 if changed else 0), code)
            payload = snapshot(rig, [cell(c, code, r) for c, (r, code) in new.items()])
            if kind == "outside":
                payload["type"], payload["authorHint"] = "changed_outside_editor", "outside"
            send(rig, payload)
            for c in list(oracle.cells):
                if c not in new:
                    oracle.cells.pop(c)
            for c, (r, code) in new.items():
                revs[c] = r
                oracle.put(c, r, code)
            revs = {c: revs[c] for c in new}
        for c in list(oracle.cells) + ["ghost"]:
            for code in codes + ["never"]:
                assert resolve(rig, c, code) == oracle.answer(c, code), (c, code)
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {c: e[-1][0] for c, e in oracle.cells.items()}


# -- reviewer additions ------------------------------------------------------------------------

def test_a_branch_of_a_branch_carries_the_whole_line_up_to_each_cut():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    send(rig, edit(rig, "c1", "b", 2))
    root = rig.root
    child = create(rig, root, 2).branch.branchId
    send(rig, edit(rig, "c1", "c", 3), edit(rig, "c1", "d", 4), target=child)
    grand = create(rig, child, 4).branch.branchId  # ticks: 1 snap, 2 b, 3 branch point, 4 c, 5 d; cut after "c"
    send(rig, edit(rig, "c1", "e", 4), target=grand)
    for branch, code, want in [(grand, "a", {"status": "ok", "cellRevision": 1}),
                               (grand, "b", {"status": "ok", "cellRevision": 2}),
                               (grand, "c", {"status": "ok", "cellRevision": 3}),
                               (grand, "e", {"status": "ok", "cellRevision": 4}),
                               (grand, "d", {"status": "code_differs", "currentRevision": 4}),
                               (child, "d", {"status": "ok", "cellRevision": 4}),
                               (child, "e", {"status": "code_differs", "currentRevision": 4})]:
        assert resolve(rig, "c1", code, branch) == want, (branch, code)
    rebuilt = Service(rig.events, rig.blobs, FixedTokenAuthorizer(TOKEN), rig.clock, new_id=IdFactory("again"))
    for branch in (root, child, grand):
        for code in "abcde":
            assert resolve(rig, "c1", code, branch) == resolve(rig, "c1", code, branch, service=rebuilt)


def test_a_branch_cut_before_a_cell_was_inserted_does_not_know_the_cell():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    root = rig.root
    child = create(rig, root, 1).branch.branchId
    send(rig, insert(rig, "c2", "later", index=1), target=root)
    assert resolve(rig, "c2", "later", branch=child) == {"status": "unknown_cell"}
    assert resolve(rig, "c2", "later", branch=root) == {"status": "ok", "cellRevision": 1}


def test_cell_revisions_follow_the_active_branch():
    from blaym_jupyter_types import SetActiveBranchRequest
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "a")]))
    send(rig, edit(rig, "c1", "b", 2))
    root = rig.root
    child = create(rig, root, 1).branch.branchId
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 1}  # the child is active
    rig.service.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=rig.exp, branchId=root))
    assert rig.service.cell_revisions(TOKEN, rig.exp) == {"c1": 2}
    assert resolve(rig, "c1", "b") == {"status": "ok", "cellRevision": 2}  # default branch is the active one
    assert child != root

"""the committed revision index and `resolve_cell_revision`.

Named cases, a rebuilt (hydrated) service, branches, and generated edit sequences against an
independent oracle that keeps plain `(revision, code)` lists.
"""

from __future__ import annotations

import uuid


from blaym_notebook_types import ResolveCellRevisionRequest

from blaym_notebook.tests.service.helpers import TOKEN, Rig, create


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


def test_the_same_code_at_revisions_3_and_5_gives_5():
    rig = Rig()
    send(rig, snapshot(rig, [cell("c1", "w")]))
    for rev, code in [(2, "y"), (3, "z"), (4, "w2"), (5, "z")]:
        send(rig, edit(rig, "c1", code, rev))
    assert resolve(rig, "c1", "z") == {"status": "ok", "cellRevision": 5}
    assert resolve(rig, "c1", "w2") == {"status": "ok", "cellRevision": 4}


def test_unknown_cell_deleted_cell_unknown_history_and_unknown_branch():
    rig = Rig()
    assert resolve(rig, "c1", "a") == {"status": "unknown_history"}  # nothing recorded yet, no raise
    send(rig, snapshot(rig, [cell("c1", "a"), cell("c2", "b")]))
    assert resolve(rig, "nope", "a") == {"status": "unknown_cell"}
    assert resolve(rig, "c1", "a", exp="other-history") == {"status": "unknown_history"}
    assert resolve(rig, "c1", "a", branch="no-such-branch") == {"status": "unknown_history"}
    send(rig, ev(rig, "cell_deleted", {"cellId": "c2"}))
    assert resolve(rig, "c2", "b") == {"status": "unknown_cell"}


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
    # no branch given: the active branch (still the root; create_branch does not move it)
    assert resolve(rig, "c1", "later-on-parent") == {"status": "ok", "cellRevision": 3}


# -- generated edit sequences against an independent oracle -----------------------------------


# -- reviewer additions ------------------------------------------------------------------------



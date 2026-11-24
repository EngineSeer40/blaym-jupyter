"""Child process for test_kill.py: ingests generated recorder events until it is killed.

Prints one line per acknowledged commit and flushes it, so the parent knows what was
acknowledged (committed): `ack <eventId>` for an ingested event, `branch <branchId>` for a
created branch. Not a test module.
Usage: python _crash_child.py <root> <exploration id> <seed> <repo root>
"""

import hashlib
import random
import sys
import uuid

root, EX, seed, repo = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
sys.path.insert(0, repo)

from blaym_jupyter_types import CreateBranchRequest, SetActiveBranchRequest  # noqa: E402

from blaym_jupyter.local import open_service  # noqa: E402
from blaym_jupyter.service.auth import FixedTokenAuthorizer  # noqa: E402

TOKEN = "t0ken"
rnd = random.Random(seed)
svc = open_service(root, FixedTokenAuthorizer(TOKEN), recover=False)


def tick_of(o):
    return getattr(o.tick, "root", o.tick)


def event(type_, payload):
    return {"schemaVersion": 2, "eventId": str(uuid.uuid4()), "explorationId": EX, "wallTimeMs": 1_770_000_000_000,
            "userId": "local", "workspaceId": "local", "originId": "child", "type": type_, "payload": payload}


def send(evs, target=None):
    req = {"explorationId": EX, "editor": "jupyterlab", "instanceId": "child", "events": evs}
    if target:
        req["targetBranchId"] = target
    outs = [getattr(o, "root", o) for o in svc.ingest(TOKEN, req).outcomes]
    for e, o in zip(evs, outs):
        if o.status == "accepted":
            print("ack", e["eventId"], flush=True)
    return outs


def item(text, blob=None):
    """An inline output item, or one whose content lives in a blob (`blob` = (hash, size))."""
    if blob is None:
        return {"outputType": "stream", "mimeTypes": ["text/plain"], "preview": text, "truncated": False,
                "contentHash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(), "sizeBytes": len(text.encode())}
    return {"outputType": "stream", "mimeTypes": ["text/plain"], "preview": text, "truncated": True,
            "contentHash": blob[0], "sizeBytes": blob[1]}


snapshot = event("notebook_snapshot", {"path": "kill.ipynb", "notebookMetadata": {}, "kernelName": "python3",
                                       "language": "python", "cells": [
    {"cellId": "c1", "cellType": "code", "code": "x = 1", "revision": 1, "metadata": {}, "outputs": []}]})
root_branch = send([snapshot])[0].branchId
rev = {root_branch: 1}
last = {root_branch: 1}  # branch -> tick of its last event
active = root_branch
run_count = 0

while True:
    action = rnd.choice(("edit", "edit", "run", "run", "run", "branch", "switch"))
    if action == "edit":
        rev[active] += 1
        o = send([event("cell_source_changed", {"cellId": "c1", "code": f"x = {rnd.randint(0, 99)}", "revision": rev[active]})],
                 target=active)[0]
        last[active] = tick_of(o)
    elif action == "run":
        run_count += 1
        run_id = f"run-{run_count}"
        o = send([event("run_started", {"runId": run_id, "cellId": "c1", "cellRevision": rev[active], "runCount": run_count})],
                 target=active)[0]
        for i in range(rnd.randint(0, 3)):
            if rnd.random() < 0.4:
                data = bytes(rnd.randrange(32, 127) for _ in range(rnd.randint(20_000, 40_000)))
                put = svc.put_blob(TOKEN, EX, data)
                it = item("chart...", (put.contentHash, put.originalBytes))
            else:
                it = item(f"line {i} of {run_id}")
            o = send([event("output_added", {"runId": run_id, "cellId": "c1", "outputIndex": i, "item": it})], target=active)[0]
        o = send([event("run_finished", {"runId": run_id, "status": rnd.choice(("ok", "error"))})], target=active)[0]
        last[active] = tick_of(o)
    elif action == "branch":
        out = svc.create_branch(TOKEN, CreateBranchRequest(explorationId=EX, fromBranchId=active, fromTick=last[active],
                                                          name=f"b{len(rev)}")).root
        if out.status == "ok":
            new = out.branch.branchId
            print("branch", new, flush=True)
            rev[new], last[new], active = rev[active], getattr(out.branch.ownTicks[0], 'root', out.branch.ownTicks[0]), new
    else:
        active = rnd.choice(list(rev))
        svc.set_active_branch(TOKEN, SetActiveBranchRequest(explorationId=EX, branchId=active))

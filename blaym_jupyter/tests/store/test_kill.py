"""Real crash tests: a child process is SIGKILLed in the middle of a stream of
ingests, blob writes and branch creations; then the root is reopened and recovery runs.

Every cycle uses its own history in the SAME root, so recovery has earlier cycles' histories
(already recovered once) to go through as well. `KILL_CYCLES` raises the cycle count.
"""

from __future__ import annotations

import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blaym_jupyter.local import open_service
from blaym_jupyter.ports.events import canonical_event_json
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.service.blobrefs import is_inline, items_of
from blaym_jupyter.service.hydrate import hydrate
from blaym_jupyter.store.events import SqliteEventStore

CHILD = Path(__file__).with_name("_crash_child.py")
REPO = str(Path(__file__).resolve().parents[3])
TOKEN = "t0ken"
CYCLES = int(os.environ.get("KILL_CYCLES", 12))
SEED = 20261123


def run_child_and_kill(root, exploration, seed, rnd) -> list[str]:
    """Start the child, let it commit a random number of events, SIGKILL it. Returns every line it printed."""
    proc = subprocess.Popen([sys.executable, str(CHILD), str(root), exploration, str(seed), REPO],
                            stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": REPO})
    lines: list[str] = []
    want = rnd.randint(1, 60)
    deadline = time.monotonic() + 60
    while len(lines) < want and time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.strip())
    time.sleep(rnd.random() * 0.05)  # land anywhere inside a commit, not only between them
    proc.send_signal(signal.SIGKILL)
    lines += [ln.strip() for ln in proc.stdout.read().splitlines()]
    proc.wait()
    assert proc.returncode == -signal.SIGKILL, f"the child stopped by itself (exit {proc.returncode})"
    assert lines, "the child never acknowledged anything"
    return lines


def snapshot_of_store(root) -> dict:
    store = SqliteEventStore(root)
    try:
        return {r.exploration_id: [canonical_event_json(e) for e in store.events_for(r.exploration_id)]
                for r in store.explorations()}
    finally:
        store.close()


def check_history(svc, report, exploration, baseline: list[str], rnd) -> None:
    store = svc._events
    events = store.events_for(exploration)
    assert [e.tick for e in events] == list(range(1, len(events) + 1)), "ticks are dense"
    now = [canonical_event_json(e) for e in events]
    assert now[: len(baseline)] == baseline, "history written before the kill is byte-identical"
    missing = {(e, h) for e, _, h in report.missing_blobs}
    for ev in events:
        for it in items_of(ev):
            if not is_inline(it):
                assert svc._blobs.exists(exploration, it.contentHash) or (exploration, it.contentHash) in missing
    rebuilder = hydrate(store, exploration)
    for branch in store.branches(exploration):
        own = [e.tick for e in events if e.branchId == branch.branch_id]
        assert own, f"branch {branch.branch_id} has no events"
        for tick in {own[-1], *rnd.sample(own, min(5, len(own)))}:
            got = rebuilder.state_at(branch.branch_id, tick)
            assert got.status == "ok", (branch.branch_id, tick, got)
        tip = rebuilder.state_at(branch.branch_id, own[-1]).state
        assert not [r for r in tip.runs if r.status.root == "running"], "no run left running"


@pytest.mark.slow
def test_kill_cycles_leave_a_history_that_recovers(tmp_path):
    rnd = random.Random(SEED)
    closed = 0
    for cycle in range(CYCLES):
        exploration = f"kill-{cycle}"
        lines = run_child_and_kill(tmp_path, exploration, SEED + cycle, rnd)
        baseline = snapshot_of_store(tmp_path)  # BEFORE recovery touches anything
        svc = open_service(tmp_path, FixedTokenAuthorizer(TOKEN))  # recovery runs here
        try:
            report = svc.recovery_report
            closed += len(report.runs_closed)
            assert not report.problems, report.problems
            for name, before in baseline.items():
                check_history(svc, report, name, before, rnd)
            # an acknowledged line is a committed one
            for line in lines:
                kind, ident = line.split()[:2]
                if kind == "ack":
                    assert svc._events.find_event(exploration, ident) is not None, line
                else:
                    assert ident in {b.branch_id for b in svc._events.branches(exploration)}, line
            # recovery is idempotent on the real store too
            assert not svc.recover().changed
            # the next ingest gets tick n + 1 + the events recovery wrote for this history
            n_before = len(baseline[exploration])
            recovery_events = sum(1 for g in report.gaps_written if g[0] == exploration) + sum(
                1 for r in report.runs_closed if r[0] == exploration)
            out = svc.ingest(TOKEN, {"explorationId": exploration, "editor": "jupyterlab", "instanceId": "parent", "events": [{
                "schemaVersion": 2, "eventId": f"00000000-0000-4000-8000-{cycle:012d}",
                "explorationId": exploration, "wallTimeMs": 1, "userId": "local", "workspaceId": "local",
                "originId": "parent", "type": "kernel_started",
                "payload": {"kernelId": "k", "kernelName": "python3", "language": "python"}}]}).outcomes[0]
            out = getattr(out, "root", out)
            assert out.status == "accepted", out
            assert getattr(out.tick, 'root', out.tick) == n_before + recovery_events + 1
        finally:
            svc.close()
    assert closed > 0, "no cycle left a run open: the kill points are not reaching mid-run"

"""Storage benchmark for the real service on SQLite (S4, Task 11).

Not a test (pytest does not collect it: the name does not start with `test_`). Run by hand:

    .venv/bin/python -m blaym_jupyter.tests.store.bench_store [--out DIR] [--seed N]

It builds a generated month (about 7,600 events, 500 runs, 270 chat turns, 50 charts, 4 branches)
through `local.open_service`, measures it, prints the numbers and writes `results.json` to
`--out` (default: a temporary folder). Cold start and the 100 MB put run in fresh child processes
(this same module with `--child`), so memory is measured from a clean interpreter. Compare with
Probe C in `docs/s2-findings.md`.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path

from blaym_jupyter_types import (
    CreateBranchRequest, IngestRequest, OutlineRequest, SearchRequest, StateAtRequest,
)

from blaym_jupyter.local import open_service
from blaym_jupyter.service.auth import FixedTokenAuthorizer

TOKEN = "bench-token"
EXP = "exploration-bench"
CHART_MIME = "application/vnd.plotly.v1+json"
TABLE_MIME = "application/vnd.dataresource+json"
NOUNS = ["residuals", "histogram", "scatter", "boxplot", "heatmap", "forecast", "lift curve", "roc curve"]
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
         "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango"]


def tk(x):
    return x.root if hasattr(x, "root") else x


def ms(x: float) -> float:
    return round(x * 1000, 3)


def stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    return {"n": len(xs), "median_ms": ms(statistics.median(xs)),
            "p95_ms": ms(xs[min(len(xs) - 1, int(len(xs) * 0.95))]), "max_ms": ms(xs[-1])}


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return round(int(line.split()[1]) / 1024, 1)
    return -1.0


def peak_rss_mb() -> float:
    """The process's own high-water mark (VmHWM; ru_maxrss is inherited across exec and misleads)."""
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return round(int(line.split()[1]) / 1024, 1)
    return -1.0


def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def disk(root: Path) -> dict:
    def size(name):
        p = root / name
        return p.stat().st_size if p.exists() else 0
    blobs = root / "explorations"
    return {"metadata_sqlite3": size("metadata.sqlite3"), "metadata_wal": size("metadata.sqlite3-wal"),
            "search_sqlite3": size("search.sqlite3"), "search_wal": size("search.sqlite3-wal"),
            "blobs_folder": dir_bytes(blobs), "blob_files": sum(
                1 for f in blobs.rglob("*") if f.is_file() and f.parent.name == "blobs")}


# -- the generated month ------------------------------------------------------------------------


class Month:
    """Builds a month of recorder events and sends them one by one through the real service."""

    def __init__(self, service, seed: int, *, events: int, runs: int, chats: int, charts: int,
                 big: int, children: int, restarts: int, refusals: bool, wall_start: int = 1_760_000_000_000):
        self.svc = service
        self.rnd = random.Random(seed)
        self.target = dict(events=events, runs=runs, chats=chats, charts=charts, big=big,
                           children=children, restarts=restarts)
        self.refusals = refusals
        self.wall = wall_start
        self.msg_no = 0
        self.run_no = 0
        self.chart_no = 0
        self.charts_made: list[tuple[bytes, str]] = []
        # per branch: revisions, run counts, code, parent info
        self.branches: dict[str, dict] = {}
        self.order: list[str] = []
        self.root = ""
        self.tip: dict[str, int] = {}
        self.lat_plain: list[float] = []
        self.lat_blob: list[float] = []
        self.put_times: list[float] = []
        self.put_sizes: list[tuple[int, float, bool]] = []
        self.refused: list[str] = []
        self.accepted = 0
        self.code_runs: list[str] = []
        self.chat_words: list[str] = []
        self.titles: list[str] = []
        self.columns: list[str] = []
        self.blob_stats = {"made": 0, "truncated": 0, "bytes_in": 0, "bytes_stored": 0}

    # -- plumbing

    def ev(self, type_: str, payload: dict, **extra) -> dict:
        self.wall += self.rnd.randrange(50, 4000)
        return {"schemaVersion": 2, "eventId": str(uuid.UUID(int=self.rnd.getrandbits(128), version=4)),
                "explorationId": EXP, "wallTimeMs": self.wall, "userId": "local", "workspaceId": "local",
                "originId": "tab-1", "type": type_, "payload": payload, **extra}

    def send(self, event: dict, branch: str | None = None, with_blob: bool = False, record: bool = True):
        req = {"explorationId": EXP, "editor": "jupyterlab", "instanceId": "tab-1", "events": [event]}
        if branch is not None:
            req["targetBranchId"] = branch
        t0 = time.perf_counter()
        out = self.svc.ingest(TOKEN, req).outcomes[0]
        dt = time.perf_counter() - t0
        out = getattr(out, "root", out)
        if out.status == "accepted":
            self.accepted += 1
            self.tip[out.branchId] = out.tick
            if record:
                (self.lat_blob if with_blob else self.lat_plain).append(dt)
        else:
            self.refused.append(out.reason)
        return out

    def code_for(self, cell: str) -> str:
        r = self.rnd
        lines = [f"# {cell}"]
        for _ in range(r.randrange(8, 24)):
            lines.append(f"{r.choice(WORDS)}_{r.randrange(100)} = df['{r.choice(WORDS)}'].{r.choice(['mean', 'sum', 'std'])}() "
                         f"* {r.random():.4f}  # {r.choice(WORDS)} {r.choice(WORDS)}")
        return "\n".join(lines)

    # -- setup and activities

    def start(self):
        cells = []
        code = {}
        for i in range(40):
            cid = f"c{i}"
            code[cid] = self.code_for(cid)
            cells.append({"cellId": cid, "cellType": "code", "code": code[cid], "revision": 1,
                          "metadata": {}, "outputs": []})
        e = self.ev("notebook_snapshot", {"path": "analysis.ipynb", "notebookMetadata": {}, "kernelName": "python3",
                                          "language": "python", "cells": cells})
        o = self.send(e, record=False)
        self.root = o.branchId
        self.branches[self.root] = {"revs": {c: 1 for c in code}, "counts": {}, "code": code}
        self.order.append(self.root)

    def pick(self) -> str:
        n = len(self.order)
        if n == 1 or self.rnd.random() < 0.6:
            return self.order[-1]
        return self.rnd.choice(self.order)

    def edit(self, b: str):
        st = self.branches[b]
        cid = f"c{self.rnd.randrange(40)}"
        lines = st["code"][cid].split("\n")
        i = self.rnd.randrange(len(lines))
        lines[i] = f"{self.rnd.choice(WORDS)}_{self.rnd.randrange(100)} = {self.rnd.random():.5f}  # edit {self.wall}"
        st["code"][cid] = "\n".join(lines)
        st["revs"][cid] += 1
        self.send(self.ev("cell_source_changed", {"cellId": cid, "code": st["code"][cid],
                                                  "revision": st["revs"][cid]}), b)

    def chart_bytes(self) -> tuple[bytes, str]:
        r = self.rnd
        title = (f"{r.choice(NOUNS).title()} of {r.choice(WORDS)}{r.randrange(1000)} against "
                 f"{r.choice(WORDS)}{r.randrange(1000)}")
        if self.charts_made and r.random() < 0.24:  # the same chart drawn again: identical content
            return r.choice(self.charts_made)
        size = int(50_000 * (100 ** r.random()))  # 50 KB to 5 MB, log-uniform
        n = size // 24
        xs = ",".join(f"{r.random():.10f}" for _ in range(n))
        ys = ",".join(f"{r.random():.10f}" for _ in range(n))
        text = ('{"data":[{"type":"scatter","mode":"markers","x":[' + xs + '],"y":[' + ys + ']}],"layout":{"title":{"text":"'
                + title + '"},"xaxis":{"title":{"text":"x axis"}},"yaxis":{"title":{"text":"y axis"}}}}')
        self.charts_made.append((text.encode(), title))
        return text.encode(), title

    def put_item(self, data: bytes, mime: str, search_text: str | None) -> dict:
        t0 = time.perf_counter()
        res = self.svc.put_blob(TOKEN, EXP, data)
        self.put_times.append(time.perf_counter() - t0)
        self.put_sizes.append((len(data), self.put_times[-1], res.truncated))
        self.blob_stats["made"] += 1
        self.blob_stats["truncated"] += int(res.truncated)
        self.blob_stats["bytes_in"] += res.originalBytes
        self.blob_stats["bytes_stored"] += res.storedBytes
        item = {"outputType": "display_data", "mimeTypes": [mime], "contentHash": res.contentHash,
                "preview": data[:512].decode("utf-8", "ignore"), "truncated": res.truncated,
                "sizeBytes": res.originalBytes}
        if search_text:
            item["searchText"] = search_text
        return item

    def run(self, b: str, kind: str):
        """One run with outputs. kind: text, table, chart, big."""
        st = self.branches[b]
        cid = f"c{self.rnd.randrange(40)}"
        self.run_no += 1
        rid = f"run-{self.run_no}"
        st["counts"][cid] = st["counts"].get(cid, 0) + 1
        self.code_runs.append(st["code"][cid])
        self.send(self.ev("run_started", {"runId": rid, "cellId": cid, "cellRevision": st["revs"][cid],
                                          "runCount": st["counts"][cid]}), b)
        items: list[tuple[dict, bool]] = []
        text = f"result {self.run_no}: " + " ".join(self.rnd.choices(WORDS, k=self.rnd.randrange(3, 40)))
        items.append(({"outputType": "stream", "mimeTypes": ["text/plain"], "contentHash": "sha256:x",
                       "preview": text, "truncated": False, "sizeBytes": len(text.encode())}, False))
        if kind == "table":
            cols = [f"col_{self.rnd.choice(WORDS)}{self.rnd.randrange(1000)}" for _ in range(self.rnd.randrange(3, 9))]
            self.columns.extend(cols)
            rows = [{c: round(self.rnd.random(), 4) for c in cols} for _ in range(self.rnd.randrange(5, 40))]
            body = json.dumps({"schema": {"fields": [{"name": c} for c in cols]}, "data": rows})
            items.append(({"outputType": "execute_result", "mimeTypes": [TABLE_MIME], "contentHash": "sha256:x",
                           "preview": body, "truncated": False, "sizeBytes": len(body.encode()),
                           "searchText": " ".join(cols)}, False))
        elif kind == "chart":
            data, title = self.chart_bytes()
            self.titles.append(title)
            items.append((self.put_item(data, CHART_MIME, f"{title} x axis y axis"), True))
        elif kind == "big":
            data = ("0123456789abcdef" * 4 + "\n").encode() * (20 * 1024 * 1024 // 65)
            items.append((self.put_item(data, "text/plain", None), True))
        for idx, (item, blob) in enumerate(items):
            self.send(self.ev("output_added", {"runId": rid, "cellId": cid, "outputIndex": idx, "item": item}),
                      b, with_blob=blob)
        self.send(self.ev("run_finished", {"runId": rid, "status": "ok"}), b)

    def chat(self, b: str):
        self.msg_no += 1
        word = self.rnd.choice(WORDS) + str(self.rnd.randrange(50))
        self.chat_words.append(word)
        ask = f"please check {word} and plot the {self.rnd.choice(NOUNS)}"
        self.send(self.ev("chat_message_added", {"chatSource": "jupyter-ai", "messageId": f"m{self.msg_no}",
                                                 "sender": "user-1", "senderKind": "person", "body": ask,
                                                 "chatTimeMs": self.wall}), b)
        self.msg_no += 1
        reply = " ".join(self.rnd.choices(WORDS, k=self.rnd.randrange(30, 160)))
        self.send(self.ev("chat_message_added", {"chatSource": "jupyter-ai", "messageId": f"m{self.msg_no}",
                                                 "sender": "ai", "senderKind": "agent", "body": reply[:20],
                                                 "chatTimeMs": self.wall}), b)
        self.send(self.ev("chat_message_updated", {"chatSource": "jupyter-ai", "messageId": f"m{self.msg_no}",
                                                   "body": reply}), b)

    def restart(self, b: str):
        self.send(self.ev("kernel_restarted", {"kernelId": f"k{self.wall}", "reason": "requested"}), b)

    def make_refusals(self, b: str):
        """A few refused events: invalid (then a recovery snapshot), conflict, missing blob, unknown branch."""
        st = self.branches[b]
        self.send(self.ev("cell_source_changed", {"cellId": "no-such-cell", "code": "x", "revision": 2}), b)
        cells = [{"cellId": c, "cellType": "code", "code": st["code"][c], "revision": st["revs"][c],
                  "metadata": {}, "outputs": []} for c in st["code"]]
        self.send(self.ev("notebook_snapshot", {"path": "analysis.ipynb", "notebookMetadata": {},
                                                "kernelName": "python3", "language": "python", "cells": cells}), b)
        e = self.ev("cell_source_changed", {"cellId": "c1", "code": st["code"]["c1"] + " ", "revision": st["revs"]["c1"] + 1})
        st["code"]["c1"] += " "
        st["revs"]["c1"] += 1
        self.send(e, b)
        self.send({**e, "payload": {**e["payload"], "code": "different"}}, b)  # same id, other content: conflict
        miss = {"outputType": "display_data", "mimeTypes": [CHART_MIME], "contentHash": "sha256:" + "0" * 64,
                "preview": "p", "truncated": False, "sizeBytes": 999}
        self.run_no += 1
        rid = f"run-{self.run_no}"
        st["counts"]["c2"] = st["counts"].get("c2", 0) + 1
        self.send(self.ev("run_started", {"runId": rid, "cellId": "c2", "cellRevision": st["revs"]["c2"],
                                          "runCount": st["counts"]["c2"]}), b)
        self.send(self.ev("output_added", {"runId": rid, "cellId": "c2", "outputIndex": 0, "item": miss}), b)
        self.send(self.ev("run_finished", {"runId": rid, "status": "ok"}), b)
        self.send(self.ev("cell_source_changed", {"cellId": "c3", "code": "y", "revision": 99}), "no-such-branch")

    def branch(self, name: str):
        b = self.order[-1] if self.rnd.random() < 0.5 else self.rnd.choice(self.order)
        r = self.svc.create_branch(TOKEN, CreateBranchRequest(
            explorationId=EXP, fromBranchId=b, fromTick=self.tip[b], name=name))
        r = getattr(r, "root", r)
        assert r.status == "ok", r
        nb = r.branch.branchId
        parent = self.branches[b]
        self.branches[nb] = {"revs": dict(parent["revs"]), "counts": dict(parent["counts"]),
                             "code": dict(parent["code"])}
        self.order.append(nb)
        self.tip[nb] = tk(r.branch.ownTicks[0])
        self.tip[b] = self.tip[b]  # the parent's tip is unchanged by branching

    def build(self):
        t = self.target
        self.start()
        acts: list[str] = (["text"] * (t["runs"] - t["charts"] - t["big"] - t["runs"] // 5)
                           + ["table"] * (t["runs"] // 5) + ["chart"] * t["charts"] + ["big"] * t["big"]
                           + ["chat"] * t["chats"] + ["restart"] * t["restarts"])
        # edits fill the rest: about 3 events per run, 3 per chat turn, 1 per restart
        used = 3 * t["runs"] + 3 * t["chats"] + t["restarts"] + 1 + 2 * t["charts"] // 1
        acts += ["edit"] * max(0, t["events"] - used - 12)
        self.rnd.shuffle(acts)
        marks = {int(len(acts) * f): f"branch-{i + 1}" for i, f in enumerate(
            [(i + 1) / (t["children"] + 1) for i in range(t["children"])])}
        refuse_at = {len(acts) // 3, 2 * len(acts) // 3} if self.refusals else set()
        for i, a in enumerate(acts):
            if i in marks:
                self.branch(marks[i])
            if i in refuse_at:
                self.make_refusals(self.pick())
            b = self.pick()
            if a == "edit":
                self.edit(b)
            elif a == "chat":
                self.chat(b)
            elif a == "restart":
                self.restart(b)
            else:
                self.run(b, a)


# -- measurements ---------------------------------------------------------------------------------


def make_service(root: Path, **kw):
    return open_service(root, FixedTokenAuthorizer(TOKEN), **kw)


def commit_rate(seed: int, mode: str, events: int = 2000) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"bench-{mode}-"))
    try:
        svc = make_service(root, synchronous=mode, recover=True)
        m = Month(svc, seed, events=events, runs=130, chats=70, charts=0, big=0, children=0, restarts=2,
                  refusals=False)
        t0 = time.perf_counter()
        m.build()
        dt = time.perf_counter() - t0
        svc.close()
        return {"events": m.accepted, "seconds": round(dt, 2), "events_per_second": round(m.accepted / dt, 1),
                "latency": stats(m.lat_plain)}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def state_json(state) -> tuple[int, int]:
    data = state.model_dump_json(exclude_none=True)
    groups = sum(len(c.outputs) for c in state.notebook.cells)
    return len(data.encode()), groups


def jump_times(svc, rb, rnd, count=300) -> dict:
    outline = svc.outline(TOKEN, OutlineRequest(explorationId=EXP))
    picks = []
    for _ in range(count):
        br = rnd.choice(outline.branches)
        line = rb.line_events(br.branchId)
        picks.append((br.branchId, tk(rnd.choice(line).tick)))
    first, second = [], []
    for sink in (first, second):
        for b, t in picks:
            t0 = time.perf_counter()
            r = svc.state_at(TOKEN, StateAtRequest(explorationId=EXP, branchId=b, tick=t))
            sink.append(time.perf_counter() - t0)
            assert r.status == "ok", r
    return {"first_pass": stats(first), "repeat_pass": stats(second)}


def search_times(svc, m_info: dict, rnd) -> dict:
    code_pool = [c for c in m_info["code_runs"]]
    kinds = {
        "code_substring": ("code", lambda: (lambda c: c[(s := rnd.randrange(max(1, len(c) - 12))):s + 10])(rnd.choice(code_pool))),
        "chat_word_prefix": ("chat", lambda: rnd.choice(m_info["chat_words"])[:5]),
        "chart_title": ("chart_text", lambda: rnd.choice(m_info["titles"])),
        "column_name": ("table_columns", lambda: rnd.choice(m_info["columns"])),
    }
    out = {}
    for name, (kind, make) in kinds.items():
        times, hits = [], []
        for _ in range(10):
            q = make()
            t0 = time.perf_counter()
            r = svc.search(TOKEN, SearchRequest(explorationId=EXP, query=q, kinds=[kind]))
            times.append(time.perf_counter() - t0)
            hits.append(len(r.hits))
        out[name] = {"median_ms": ms(statistics.median(times)), "max_ms": ms(max(times)),
                     "queries_with_hits": sum(1 for h in hits if h), "hits_median": statistics.median(hits)}
    return out


def rebuild_search_cost(root: Path, every_edit: bool) -> dict:
    copy = Path(tempfile.mkdtemp(prefix="bench-idx-")) / "root"
    shutil.copytree(root, copy)
    try:
        svc = make_service(copy, recover=False, index_every_edit=every_edit)
        t0 = time.perf_counter()
        docs = svc.rebuild_search(TOKEN, EXP)
        dt = time.perf_counter() - t0
        svc.close()
        d = disk(copy)
        return {"seconds": round(dt, 2), "documents": docs,
                "index_bytes": d["search_sqlite3"] + d["search_wal"]}
    finally:
        shutil.rmtree(copy.parent, ignore_errors=True)


def run_child(args: list[str]) -> dict:
    out = subprocess.run([sys.executable, "-m", "blaym_jupyter.tests.store.bench_store", "--child", *args],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def child_cold(root: str, recover: bool, branch: str, trace: bool) -> dict:
    res: dict = {"rss_start_mb": rss_mb()}
    if trace:
        tracemalloc.start()
    t0 = time.perf_counter()
    svc = make_service(Path(root), recover=recover)
    res["open_s"] = round(time.perf_counter() - t0, 3)
    t1 = time.perf_counter()
    outline = svc.outline(TOKEN, OutlineRequest(explorationId=EXP))
    res["first_outline_s"] = round(time.perf_counter() - t1, 3)
    tip = max(tk(t) for b in outline.branches for t in b.ownTicks if b.branchId == branch)
    t2 = time.perf_counter()
    r = svc.state_at(TOKEN, StateAtRequest(explorationId=EXP, branchId=branch, tick=tip))
    res["first_state_at_s"] = round(time.perf_counter() - t2, 3)
    res["total_to_first_state_s"] = round(time.perf_counter() - t0, 3)
    assert r.status == "ok"
    res["rss_after_mb"] = rss_mb()
    res["peak_rss_mb"] = peak_rss_mb()
    if not recover:
        t3 = time.perf_counter()
        report = svc.recover()
        res["warm_recover_s"] = round(time.perf_counter() - t3, 3)
        res["recover_changed"] = bool(report.changed)
    if trace:
        gc.collect()
        cur, peak = tracemalloc.get_traced_memory()
        rb = svc._exps[EXP].rebuilder
        n = len(rb._events)
        res.update(traced_current_mb=round(cur / 1e6, 1), traced_peak_mb=round(peak / 1e6, 1),
                   events_kept=n, events_list_bytes=sys.getsizeof(rb._events))
        res["list_items_same_objects_as_log"] = sum(1 for e in rb._events if rb._log.by_tick.get(e.tick) is e)
        rb._events = []
        gc.collect()
        res["traced_after_clearing_event_list_mb"] = round(tracemalloc.get_traced_memory()[0] / 1e6, 1)
    svc.close()
    return res


def child_bigblob() -> dict:
    root = Path(tempfile.mkdtemp(prefix="bench-big-"))
    try:
        svc = make_service(root, recover=False)
        chunk = ("0123456789abcdef" * 4 + "\n").encode() * 16384  # about 1 MB

        def stream():
            for _ in range(100):
                yield chunk

        rss0 = rss_mb()
        tracemalloc.start()
        t0 = time.perf_counter()
        res = svc.put_blob(TOKEN, "exploration-big", stream())
        dt = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        svc.close()
        return {"seconds": round(dt, 2), "traced_peak_mb": round(peak / 1e6, 1), "rss_before_mb": rss0,
                "peak_rss_mb": peak_rss_mb(), "original_bytes": res.originalBytes, "stored_bytes": res.storedBytes,
                "truncated": res.truncated}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--seed", type=int, default=20261122)
    ap.add_argument("--child", nargs="*", default=None)
    ns, rest = ap.parse_known_args()
    if ns.child is not None:
        kind = ns.child[0]
        if kind == "bigblob":
            print(json.dumps(child_bigblob()))
        else:
            root, recover, branch, trace = ns.child[1:5]
            print(json.dumps(child_cold(root, recover == "1", branch, trace == "1")))
        return

    out = Path(ns.out) if ns.out else Path(tempfile.mkdtemp(prefix="bench-store-out-"))
    out.mkdir(parents=True, exist_ok=True)
    root = out / "root"
    if root.exists():
        shutil.rmtree(root)
    results: dict = {"seed": ns.seed}

    def log(msg):
        print(msg, flush=True)

    log("building the month (this takes a while: every event is committed with synchronous=FULL)")
    svc = make_service(root, recover=True)
    m = Month(svc, ns.seed, events=7600, runs=500, chats=270, charts=50, big=3, children=4, restarts=5,
              refusals=True)
    t0 = time.perf_counter()
    m.build()
    build_s = time.perf_counter() - t0
    info = {"code_runs": m.code_runs, "chat_words": m.chat_words, "titles": m.titles, "columns": m.columns}
    outline = svc.outline(TOKEN, OutlineRequest(explorationId=EXP))
    stored_events = sum(len(b.ownTicks) for b in outline.branches)
    results["made"] = {"events_stored": stored_events, "events_accepted_by_recorder": m.accepted,
                       "refused": {r: m.refused.count(r) for r in set(m.refused)}, "branches": len(outline.branches),
                       "runs": m.run_no, "chat_messages": m.msg_no, "charts_blobs": m.blob_stats, "chart_sizes_bytes": sorted(n for n, _, tr in m.put_sizes if not tr)[::6],
                       "build_seconds": round(build_s, 1), "search_errors": len(svc.search_errors)}
    log(json.dumps(results["made"]))
    results["ingest_latency"] = {"without_new_blob": stats(m.lat_plain), "output_event_with_blob": stats(m.lat_blob),
                                 "put_blob_alone": stats(m.put_times),
                                 "put_blob_under_1mb": stats([t for n, t, tr in m.put_sizes if n < 1_000_000 and not tr]),
                                 "put_blob_1_to_5mb": stats([t for n, t, tr in m.put_sizes if n >= 1_000_000 and not tr]),
                                 "put_blob_20mb_truncated": stats([t for n, t, tr in m.put_sizes if tr])}
    results["disk_before_close"] = disk(root)
    svc.close()
    results["disk"] = disk(root)
    results["disk"]["total_bytes"] = sum(results["disk"][k] for k in (
        "metadata_sqlite3", "metadata_wal", "search_sqlite3", "search_wal", "blobs_folder"))

    log("warm measurements")
    warm = make_service(root, recover=False)
    rb = warm._exps.get(EXP)
    warm.outline(TOKEN, OutlineRequest(explorationId=EXP))
    rb = warm._exps[EXP].rebuilder
    rnd = random.Random(ns.seed + 1)
    results["jump"] = jump_times(warm, rb, rnd)
    # outline: first build then cached
    warm._exps[EXP].rebuilder._outline = None
    t = time.perf_counter()
    warm.outline(TOKEN, OutlineRequest(explorationId=EXP))
    first = time.perf_counter() - t
    t = time.perf_counter()
    warm.outline(TOKEN, OutlineRequest(explorationId=EXP))
    results["outline"] = {"first_build_ms": ms(first), "cached_ms": ms(time.perf_counter() - t)}
    # sizes of rebuilt states
    root_branch = outline.branches[0]
    sizes = {}
    for br in outline.branches:
        tip = max(tk(t) for t in br.ownTicks)
        r = warm.state_at(TOKEN, StateAtRequest(explorationId=EXP, branchId=br.branchId, tick=tip))
        b, g = state_json(r.state)
        sizes[br.name or br.branchId] = {"json_bytes": b, "output_groups": g}
    rr = warm.state_at(TOKEN, StateAtRequest(explorationId=EXP, branchId=root_branch.branchId,
                                             tick=max(tk(t) for t in root_branch.ownTicks)))
    b, g = state_json(rr.state)
    results["end_state_root"] = {"json_bytes": b, "output_groups": g,
                                 "snapshots_every_200_events": stored_events // 200,
                                 "snapshots_total_bytes_if_all_same_size": b * (stored_events // 200)}
    results["end_state_all_branches"] = sizes
    results["search"] = search_times(warm, info, random.Random(ns.seed + 2))
    warm.close()

    log("search rebuild")
    results["rebuild_search_every_edit_off"] = rebuild_search_cost(root, False)
    results["rebuild_search_every_edit_on"] = rebuild_search_cost(root, True)

    log("commit rate, 2,000 events")
    results["commit_rate"] = {"FULL": commit_rate(ns.seed, "FULL"), "NORMAL": commit_rate(ns.seed, "NORMAL")}

    log("cold start (fresh processes, on copies of the finished root)")
    branch = root_branch.branchId
    cold = {}
    for name, recover, trace in (("recover_false", "0", "0"), ("recover_true", "1", "0"),
                                 ("recover_false_traced", "0", "1")):
        copy = out / f"cold-{name}"
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(root, copy)
        cold[name] = run_child(["cold", str(copy), recover, branch, trace])
        shutil.rmtree(copy)
    results["cold_start"] = cold

    log("100 MB put_blob")
    results["put_100mb"] = run_child(["bigblob"])

    (out / "results.json").write_text(json.dumps(results, indent=2))
    log(json.dumps(results, indent=2))
    log(f"results written to {out / 'results.json'}")


if __name__ == "__main__":
    main()

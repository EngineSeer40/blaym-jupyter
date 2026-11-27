"""Jump-speed benchmark for the real rebuilder.

Not a test (pytest does not collect it). Run by hand:

    python -m blaym_jupyter.tests.history.bench_rebuild

Shape: about 8,000 events, 4 branches, snapshot interval 200, 200 random positions,
asked cold (first pass) and warm (same positions again). Targets: median <= 10 ms,
p95 <= 50 ms, max <= 150 ms; adding the 8,000 events (with eager snapshots) <= 2 s.
"""

from __future__ import annotations

import random
import statistics
import time

from blaym_jupyter.history import Rebuilder
from blaym_jupyter.tests.history.reference import reference_rebuild
from blaym_jupyter.tests.history.scenario import Scenario

TARGET_EVENTS = 8000
INTERVAL = 200
POSITIONS = 200


def build(seed: int = 7):
    rnd = random.Random(seed)
    s = Scenario()
    s.start(cells=[(f"c{i}", f"x{i}") for i in range(8)])
    branches = ["main"]
    while s.tick < TARGET_EVENTS:
        s.on(rnd.choice(branches))
        r = rnd.random()
        c = f"c{rnd.randrange(8)}"
        if r < 0.7:
            s.edit(c, f"v{s.tick}")
        elif r < 0.9:
            rid = s.run(c)
            s.output(rid, f"out{s.tick}")
            s.finish(rid)
        elif r < 0.93 and len(branches) < 4 and s.tick > 100:
            par = rnd.choice(branches)
            # the parent's own latest event is a settled, valid branch point
            t = max(e.tick for e in s.events if e.branchId == par and e.tick < s.tick - 1)
            branches.append(s.branch_from(tick=t, name=f"b{len(branches)}", parent=par))
        else:
            s.chat(f"m{s.tick}", "hi")
    return s, branches, rnd


def _stats(name: str, xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    out = {
        "median": statistics.median(xs) * 1e3,
        "p95": xs[min(len(xs) - 1, int(len(xs) * 0.95))] * 1e3,
        "max": xs[-1] * 1e3,
    }
    print(f"{name}: median {out['median']:.2f} ms  p95 {out['p95']:.2f} ms  max {out['max']:.2f} ms")
    return out


def main() -> None:
    s, branches, rnd = build()
    ev = s.events
    print("events", len(ev), "branches", branches, "interval", INTERVAL)
    rb = Rebuilder(interval=INTERVAL)
    t0 = time.perf_counter()
    rb.add(ev)
    print(f"add {len(ev)}: {time.perf_counter() - t0:.3f} s  (snapshots cached: {len(rb.snapshots)})")
    pos = [(rnd.choice(branches), rnd.randint(1, s.tick)) for _ in range(POSITIONS)]
    cold = []
    for b, t in pos:
        t0 = time.perf_counter()
        r = rb.state_at(b, t)
        cold.append(time.perf_counter() - t0)
        assert r.status == "ok", (b, t, r.status)
    _stats("cold (first pass)", cold)
    warm = []
    for b, t in pos:
        t0 = time.perf_counter()
        rb.state_at(b, t)
        warm.append(time.perf_counter() - t0)
    _stats("warm (same positions again)", warm)
    for b, t in pos[:3]:
        got = rb.state_at(b, t).model_dump(mode="json")
        assert got == reference_rebuild(ev, b, t).model_dump(mode="json"), (b, t)
    print("equal to the reference on 3 positions")


if __name__ == "__main__":
    main()

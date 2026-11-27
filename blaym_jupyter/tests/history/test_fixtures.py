"""The shared rebuild fixtures in `fixtures/rebuild/`.

The same files are read by `src/__tests__/rebuild-fixtures.spec.ts`, which validates them
with the TypeScript contract validator. Here every event and every recorded result is
validated by the Python models, and the real rebuilder (and the reference) must reproduce
every recorded result from the events as stored.
"""

from __future__ import annotations

import json
from pathlib import Path

from blaym_jupyter_types import BlaymEvent, RebuildResult

from blaym_jupyter.history import Rebuilder
from blaym_jupyter.tests.history.reference import reference_rebuild

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "rebuild"
NAMES = sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))


def dump(result):
    # null fields are left out, as in the fixture files: the shared schema has optional fields, never nulls
    return result.model_dump(mode="json", exclude_none=True)


def test_every_fixture_validates_and_the_real_rebuilder_reproduces_every_result():
    assert len(NAMES) >= 16
    for name in NAMES:
        data = json.loads((FIXTURE_DIR / f"{name}.json").read_text())
        assert data["expectations"], f"{name}: a fixture must record at least one result"
        events = [BlaymEvent.model_validate(e).root for e in data["events"]]
        for ex in data["expectations"]:
            RebuildResult.model_validate(ex["result"])  # the recorded result is a valid contract value
        for interval in (1, 7, None):
            rb = Rebuilder(interval=interval)
            rb.add(events)
            for ex in data["expectations"]:
                got = rb.state_at(ex["branchId"], ex["tick"])
                assert dump(got) == ex["result"], (name, interval, ex["branchId"], ex["tick"])
                assert dump(reference_rebuild(events, ex["branchId"], ex["tick"])) == ex["result"]

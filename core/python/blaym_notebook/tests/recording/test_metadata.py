"""The metadata policy applier, against the shared cases file."""

from __future__ import annotations

import copy
import json
from pathlib import Path


from blaym_notebook.recording.metadata import apply_policy, load_policy

FIXTURES = Path(__file__).resolve().parents[5] / "fixtures" / "recorder"
CASES = json.loads((FIXTURES / "metadata-cases.json").read_text())["cases"]


def _policy(case):
    return {**load_policy(), **case.get("policyOverride", {})}


def test_shared_cases():
    for case in CASES:
        before = copy.deepcopy(case["input"])
        out, dropped = apply_policy(case["input"], case["level"], policy=_policy(case))
        assert out == case["expected"], case["name"]
        assert dropped == case["dropped"], case["name"]
        assert case["input"] == before  # never mutated


def test_a_300kb_value_is_replaced_by_a_marker():
    big = "x" * 300_000
    out, dropped = apply_policy({"widgets": {"state": big}, "tags": ["t"]}, "notebook")
    assert dropped == ["widgets"]
    assert out["tags"] == ["t"]
    assert out["widgets"]["blaymDropped"] == "too_large" and out["widgets"]["bytes"] > 300_000
    # exactly at the limit is kept
    keep, none = apply_policy({"v": "x" * (256 * 1024 - 2)}, "cell")
    assert none == [] and keep["v"].startswith("xxx")

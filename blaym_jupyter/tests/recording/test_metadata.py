"""The metadata policy applier (S5, Task 4), against the shared cases file."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from blaym_jupyter.recording.metadata import apply_policy, load_policy

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "recorder"
CASES = json.loads((FIXTURES / "metadata-cases.json").read_text())["cases"]


def _policy(case):
    return {**load_policy(), **case.get("policyOverride", {})}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_cases(case):
    before = copy.deepcopy(case["input"])
    out, dropped = apply_policy(case["input"], case["level"], policy=_policy(case))
    assert out == case["expected"]
    assert dropped == case["dropped"]
    assert case["input"] == before  # never mutated


def test_policy_file_shape():
    p = load_policy()
    assert p["version"] == 1 and p["maxValueBytes"] == 256 * 1024
    assert set(p["notebook"]["dropKeys"]) == {"kernelspec", "language_info"}
    assert p["cell"]["dropKeys"] == ["trusted"]


def test_a_300kb_value_is_replaced_by_a_marker():
    big = "x" * 300_000
    out, dropped = apply_policy({"widgets": {"state": big}, "tags": ["t"]}, "notebook")
    assert dropped == ["widgets"]
    assert out["tags"] == ["t"]
    assert out["widgets"]["blaymDropped"] == "too_large" and out["widgets"]["bytes"] > 300_000
    # exactly at the limit is kept
    keep, none = apply_policy({"v": "x" * (256 * 1024 - 2)}, "cell")
    assert none == [] and keep["v"].startswith("xxx")


def test_unknown_and_none_inputs():
    assert apply_policy(None, "cell") == ({}, [])
    out, _ = apply_policy({"zzz": {"deep": [1]}}, "notebook")
    assert out == {"zzz": {"deep": [1]}}
    with pytest.raises(ValueError):
        apply_policy({}, "output")  # type: ignore[arg-type]


def test_result_does_not_share_structure_with_input():
    src = {"tags": ["a"]}
    out, _ = apply_policy(src, "cell")
    out["tags"].append("b")
    assert src == {"tags": ["a"]}


def test_deterministic():
    m = {"trusted": 1, "execution": {"shell.execute_reply": 1}, "k": 2}
    assert apply_policy(m, "cell") == apply_policy(dict(reversed(list(m.items()))), "cell")

"""The metadata policy: what of a notebook's or cell's own metadata is not recorded.

One data file decides (`metadata-policy.json` beside this module, read with importlib.resources and
copied into the TypeScript core at build time); this module only applies it. Pure: the input is never changed, the result is
deterministic, and the keys left out are listed so the caller can count them.
"""

from __future__ import annotations

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Mapping


_cached: dict | None = None


def load_policy(path: Path | None = None) -> dict:
    """The policy file's content (the default file is read once)."""
    global _cached
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    if _cached is None:
        _cached = json.loads(files("blaym_notebook.recording").joinpath("metadata-policy.json").read_text(encoding="utf-8"))
    return _cached


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def apply_policy(
    metadata: Mapping[str, Any] | None,
    level: Literal["notebook", "cell"],
    *,
    policy: Mapping[str, Any] | None = None,
) -> tuple[dict, list[str]]:
    """`(metadata kept, keys dropped)`. A dropped nested key is listed as `outer.inner`; a
    value over the size limit is replaced by the policy's marker plus its `bytes` and listed
    by its key. The list is sorted."""
    if level not in ("notebook", "cell"):
        raise ValueError(f"unknown metadata level {level!r}")
    pol = load_policy() if policy is None else policy
    rules = pol[level]
    limit = pol["maxValueBytes"]
    result = copy.deepcopy(dict(metadata or {}))
    dropped: list[str] = []

    for key in rules.get("dropKeys", []):
        if key in result:
            del result[key]
            dropped.append(key)

    for key, inner in rules.get("dropNestedKeys", {}).items():
        value = result.get(key)
        if not isinstance(value, dict):
            continue
        for name in inner:
            if name in value:
                del value[name]
                dropped.append(f"{key}.{name}")
        if not value:
            del result[key]

    for key in list(result):
        size = _size(result[key])
        if size > limit:
            result[key] = {**pol["oversizeMarker"], "bytes": size}
            dropped.append(key)

    return result, sorted(dropped)

"""Identity matching (pure): stable ids by overlap, older notebooks by exact source hash."""

from __future__ import annotations

from blaym_jupyter.recording.identity import match_record


def rec(nid, path, ids=None, h="h", stable=True, created=1):
    fp = {"nbformat": "4.5" if stable else "4.4", "sourceHash": h}
    if ids is not None:
        fp["cellIds"] = ids
    return {"notebookId": nid, "path": path, "cellIdsStable": stable, "fingerprint": fp, "createdMs": created}


def fp(ids=None, h="h"):
    out = {"nbformat": "4.5", "sourceHash": h}
    if ids is not None:
        out["cellIds"] = ids
    return out


def test_stable_ids_match_by_overlap_not_hash():
    records = [rec("a", "a.ipynb", ["c1", "c2"], h="old")]
    assert match_record(True, fp(["c2", "c9"], h="new"), records, lambda p: True)["notebookId"] == "a"
    assert match_record(True, fp(["x"], h="old"), records, lambda p: True) is None


def test_old_notebooks_only_match_exact_hash_of_old_records():
    records = [rec("o", "o", stable=False, h="same"), rec("s", "s", ["c1"], h="same")]
    assert match_record(False, fp(h="same"), records, lambda p: True)["notebookId"] == "o"
    assert match_record(False, fp(h="edited"), records, lambda p: True) is None

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


def test_best_overlap_wins_and_gone_path_breaks_a_tie():
    records = [rec("a", "a", ["c1"]), rec("b", "b", ["c1", "c2"])]
    assert match_record(True, fp(["c1", "c2"]), records, lambda p: True)["notebookId"] == "b"
    tie = [rec("a", "a", ["c1"]), rec("b", "b", ["c1"])]
    assert match_record(True, fp(["c1"]), tie, lambda p: p == "a")["notebookId"] == "b"


def test_old_notebooks_only_match_exact_hash_of_old_records():
    records = [rec("o", "o", stable=False, h="same"), rec("s", "s", ["c1"], h="same")]
    assert match_record(False, fp(h="same"), records, lambda p: True)["notebookId"] == "o"
    assert match_record(False, fp(h="edited"), records, lambda p: True) is None


def test_stable_notebook_never_matches_an_old_record_and_back():
    assert match_record(True, fp(["c1"], h="x"), [rec("o", "o", stable=False, h="x")], lambda p: True) is None
    assert match_record(False, fp(h="x"), [rec("s", "s", ["c1"], h="x")], lambda p: True) is None


def test_result_does_not_depend_on_record_order():
    a, b = rec("a", "a", ["c1"]), rec("b", "b", ["c1"])
    assert match_record(True, fp(["c1"]), [a, b], lambda p: True)["notebookId"] == \
        match_record(True, fp(["c1"]), [b, a], lambda p: True)["notebookId"]

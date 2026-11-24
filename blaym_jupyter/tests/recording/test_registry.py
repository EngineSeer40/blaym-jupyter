"""The notebook registry: identity, the active instance, the heartbeat, durable records."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from blaym_jupyter.recording.registry import DEFAULT_ALIVE_WINDOW_MS, Registry
from blaym_jupyter.recording.registry_store import FolderRegistryStore
from blaym_jupyter.testing.fakes import FakeClock
from blaym_jupyter_types import NotebookFingerprint, NotebookRegistration, OpenGap

WINDOW = DEFAULT_ALIVE_WINDOW_MS


class Counter:
    def __init__(self, prefix="nb"):
        self._n = count(1)
        self._p = prefix

    def new(self):
        return f"{self._p}{next(self._n)}"


class Env:
    """A registry over a folder, a fake clock and a fake file system (a set of paths)."""

    def __init__(self, root: Path, files=None, probe=None, clock=None, ids=None):
        self.root = root
        self.files: set[str] = set() if files is None else files
        self.clock = clock or FakeClock()
        self.ids = ids or Counter()
        self.store = FolderRegistryStore(root)
        self.probe = probe
        self.reg = Registry(self.store, self.clock, self.ids, probe=probe, path_exists=self.files.__contains__)

    def restart(self):
        return Env(self.root, self.files, self.probe, self.clock, self.ids)


def registration(path="a.ipynb", instance="i1", ids=("c1", "c2"), source_hash="h", **kw):
    stable = kw.pop("stable", True)
    fp = NotebookFingerprint(
        nbformat="4.5" if stable else "4.4",
        sourceHash=source_hash,
        cellIds=list(ids) if stable else None,
    )
    return NotebookRegistration(
        path=path, editor="jupyterlab", instanceId=instance, cellIdsStable=stable,
        fingerprint=fp, cellAliases=kw.pop("aliases", {}), **kw,
    )


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.files.add("a.ipynb")
    return e


def test_first_register_creates_a_record_with_recording_off(env):
    s = env.reg.register(registration())
    assert (s.recording, s.active, s.notebookId, s.explorationId) == (False, True, "nb1", "nb1")
    assert s.notices == [] and not s.needsRegister
    files = list((env.root / "notebooks").glob("*_metadata.json"))
    assert [f.name for f in files] == ["nb1_metadata.json"]
    data = json.loads(files[0].read_text())
    assert data["recording"] is False and data["path"] == "a.ipynb" and data["version"] == 1


def test_register_is_idempotent(env):
    a = env.reg.register(registration())
    b = env.reg.register(registration())
    assert a.notebookId == b.notebookId and len(env.store.load_all()) == 1


def test_switching_recording_on_persists_across_a_new_registry(env):
    env.reg.register(registration(setRecording=True))
    env2 = env.restart()
    s = env2.reg.register(registration(instance="i9"))
    assert s.notebookId == "nb1" and s.recording is True
    assert env2.reg.register(registration(instance="i9", setRecording=False)).recording is False
    assert env.restart().reg.register(registration()).recording is False


def test_second_instance_refused_while_first_alive(env):
    env.reg.register(registration(instance="i1", setRecording=True))
    s = env.reg.register(registration(instance="i2", setRecording=False))
    assert (s.active, s.reason, s.holderInstanceId) == (False, "other_recorder_active", "i1")
    assert s.recording is True  # the refused instance changed nothing
    assert env.reg.gate_ingest("i1", "nb1") == "ok"
    assert env.reg.gate_ingest("i2", "nb1") == "not_active_recorder"


def test_second_instance_wins_when_first_is_stale(env):
    env.reg.register(registration(instance="i1"))
    env.clock._ms += WINDOW + 1
    s = env.reg.register(registration(instance="i2"))
    assert s.active and s.needsResync
    assert env.reg.gate_ingest("i1", "nb1") == "not_active_recorder"
    # the old holder coming back does not take it back
    assert env.reg.status("nb1", "i1").active is False


def test_a_heartbeat_keeps_the_holder_alive(env):
    env.reg.register(registration(instance="i1"))
    env.reg.register(registration(instance="i2"))
    for _ in range(5):
        env.clock._ms += WINDOW - 1
        assert env.reg.status("nb1", "i1").active
    assert not env.reg.status("nb1", "i2").active


def test_stale_holder_loses_to_a_heartbeat_of_the_other_instance(env):
    env.reg.register(registration(instance="i1"))
    env.reg.register(registration(instance="i2"))
    env.clock._ms += WINDOW + 1
    s = env.reg.status("nb1", "i2")
    assert s.active and s.needsResync
    assert not env.reg.status("nb1", "i2").needsResync  # asked once


def test_takeover_wins_even_if_first_is_alive(env):
    env.reg.register(registration(instance="i1"))
    s = env.reg.register(registration(instance="i2", takeover=True))
    assert s.active and s.needsResync
    assert env.reg.gate_ingest("i1", "nb1") == "not_active_recorder"


def test_close_frees_the_notebook_at_once(env):
    env.reg.register(registration(instance="i1"))
    env.reg.close("nb1", "i1")
    assert env.reg.register(registration(instance="i2")).active
    assert not env.reg.status("nb1", "i1").active and env.reg.status("nb1", "i1").needsRegister


def test_clean_close_is_told_apart_from_a_lost_session(env):
    env.reg.register(registration())
    env.reg.close("nb1", "i1")
    e2 = env.restart()
    e2.reg.register(registration())
    assert e2.reg.was_clean_close("nb1") is True
    e3 = e2.restart()  # i1 never closed
    e3.reg.register(registration())
    assert e3.reg.was_clean_close("nb1") is False


def test_collaboration_is_not_supported_and_recording_stays_off(env):
    s = env.reg.register(registration(collaboration=True, setRecording=True))
    assert (s.active, s.reason, s.recording) == (False, "collaboration_not_supported", False)
    assert env.reg.gate_ingest("i1", s.notebookId) == "not_active_recorder"
    assert env.store.load_all()[0]["recording"] is False


def test_rename_keeps_the_id(env):
    s = env.reg.register(registration())
    env.reg.note_rename("a.ipynb", "b.ipynb")
    env.files.discard("a.ipynb")
    env.files.add("b.ipynb")
    again = env.reg.register(registration(path="b.ipynb", ids=("c1", "c2", "c3"), source_hash="edited"))
    assert again.notebookId == s.notebookId and again.notices == []
    rec = env.store.load_all()[0]
    assert rec["path"] == "b.ipynb" and rec["previousPaths"] == ["a.ipynb"]


def test_copy_while_original_exists_gets_a_new_id_and_notice(env):
    a = env.reg.register(registration(setRecording=True))
    env.files.add("copy.ipynb")
    c = env.reg.register(registration(path="copy.ipynb", instance="i2"))
    assert c.notebookId != a.notebookId
    assert [n.code for n in c.notices] == ["copied"] and c.recording is False and c.active
    assert len(env.store.load_all()) == 2


def test_move_while_original_is_gone_follows_the_id(env):
    a = env.reg.register(registration(setRecording=True))
    env.files.discard("a.ipynb")
    env.files.add("moved.ipynb")
    m = env.reg.register(registration(path="moved.ipynb"))
    assert m.notebookId == a.notebookId and m.recording is True
    assert [n.code for n in m.notices] == ["moved"]
    assert len(env.store.load_all()) == 1


def test_old_notebook_with_identical_sources_follows_and_edited_one_is_new(env):
    a = env.reg.register(registration(stable=False, source_hash="same"))
    env.files.discard("a.ipynb")
    env.files.update({"b.ipynb", "c.ipynb"})
    edited = env.reg.register(registration(path="c.ipynb", stable=False, source_hash="edited", instance="i2"))
    assert edited.notebookId != a.notebookId and edited.notices == []
    moved = env.reg.register(registration(path="b.ipynb", stable=False, source_hash="same", instance="i3"))
    assert moved.notebookId == a.notebookId and [n.code for n in moved.notices] == ["moved"]


def test_register_after_a_server_restart_needs_register_first(env):
    env.reg.register(registration(setRecording=True))
    e2 = env.restart()
    hb = e2.reg.status("nb1", "i1")
    assert hb.needsRegister and not hb.active and hb.notebookId == "nb1"
    assert e2.reg.gate_ingest("i1", "nb1") == "not_active_recorder"
    s = e2.reg.register(registration())
    assert s.active and s.notebookId == "nb1" and s.recording
    assert not e2.reg.status("nb1", "i1").needsRegister


def test_heartbeat_carries_the_probe_state(tmp_path):
    class Probe:
        def runs_open(self, e): return 2
        def cell_revisions(self, e): return {"c1": 4}
        def open_gaps(self, e): return [OpenGap(gapId="g", source="browser", reason="no_client")]

    e = Env(tmp_path, probe=Probe())
    e.files.add("a.ipynb")
    s = e.reg.register(registration())
    hb = e.reg.status(s.notebookId, "i1")
    assert (hb.runsOpen, hb.cellRevisions, hb.openGaps[0].gapId) == (2, {"c1": 4}, "g")


def test_aliases_and_liveness(env):
    s = env.reg.register(registration(aliases={"m1": "b1"}))
    assert env.reg.aliases(s.notebookId) == {"m1": "b1"}
    assert env.reg.alias_for(s.notebookId, "m1") == "b1" and env.reg.alias_for(s.notebookId, "zz") == "zz"
    env.reg.register(registration(aliases={"m1": "b2"}))
    assert env.reg.alias_for(s.notebookId, "m1") == "b2"
    assert env.reg.is_alive(s.notebookId)
    env.clock._ms += WINDOW + 1
    assert not env.reg.is_alive(s.notebookId)
    env.reg.status(s.notebookId, "i1")
    assert env.reg.is_alive(s.notebookId)
    assert not env.reg.is_alive("unknown")


def test_scan_marks_missing_and_never_deletes(env):
    env.reg.register(registration())
    env.files.discard("a.ipynb")
    assert env.reg.scan(env.files.__contains__) == ["nb1"]
    assert env.store.load_all()[0]["pathMissing"] is True
    env.files.add("a.ipynb")
    assert env.reg.scan(env.files.__contains__) == []
    assert env.store.load_all()[0]["pathMissing"] is False


def test_note_save_records_the_hash(env):
    env.reg.register(registration())
    env.reg.note_save("a.ipynb", "deadbeef")
    assert env.store.load_all()[0]["lastWrittenHash"] == "deadbeef"


# ---- durability ----------------------------------------------------------------------


class Crash(Exception):
    pass


def test_a_crash_between_temp_write_and_rename_is_never_half_read(tmp_path):
    e = Env(tmp_path)
    e.files.add("a.ipynb")
    e.reg.register(registration(setRecording=True))
    before = (tmp_path / "notebooks" / "nb1_metadata.json").read_bytes()

    class Faulty(FolderRegistryStore):
        def _commit(self, tmp, final):
            assert tmp.read_bytes()  # the temp file is fully written when the crash comes
            raise Crash

    faulty = Registry(Faulty(tmp_path), e.clock, e.ids, path_exists=e.files.__contains__)
    with pytest.raises(Crash):
        faulty.register(registration(instance="i7", setRecording=False))
    assert (tmp_path / "notebooks" / "nb1_metadata.json").read_bytes() == before
    assert not list((tmp_path / "notebooks").glob("*.tmp"))
    # an orphaned temp file from a real kill is ignored on load
    (tmp_path / "notebooks" / ".nb1_metadata.json.dead.tmp").write_text('{"half')
    assert e.restart().reg.register(registration()).recording is True


def test_a_crash_on_first_write_leaves_no_record(tmp_path):
    class Faulty(FolderRegistryStore):
        def _commit(self, tmp, final):
            raise Crash

    r = Registry(Faulty(tmp_path), FakeClock(), Counter(), path_exists=lambda p: True)
    with pytest.raises(Crash):
        r.register(registration())
    assert FolderRegistryStore(tmp_path).load_all() == []


def test_unknown_future_version_is_left_untouched_and_counted(tmp_path):
    folder = tmp_path / "notebooks"
    folder.mkdir()
    future = folder / "zz_metadata.json"
    future.write_text(json.dumps({"version": 99, "notebookId": "zz", "recording": True, "createdMs": 1,
                                  "fingerprint": {"nbformat": "4.5", "sourceHash": "h", "cellIds": ["c1", "c2"]},
                                  "path": "a.ipynb", "cellIdsStable": True, "extra": "kept"}))
    (folder / "bad_metadata.json").write_text("{not json")
    original = future.read_bytes()
    e = Env(tmp_path)
    e.files.add("a.ipynb")
    assert e.reg.counters["future_version"] == 1 and e.reg.counters["unreadable"] == 1
    s = e.reg.register(registration())
    assert s.notebookId != "zz" and s.recording is False  # the future record is not used
    assert future.read_bytes() == original


# ---- generated sequences -------------------------------------------------------------

PATHS = ["a", "b", "c"]
OPS = st.one_of(
    st.tuples(st.just("register"), st.sampled_from(PATHS), st.sampled_from(["i1", "i2", "i3"]),
              st.booleans(), st.sampled_from([0, 1, 2]), st.booleans()),
    st.tuples(st.just("close"), st.sampled_from(PATHS), st.sampled_from(["i1", "i2", "i3"])),
    st.tuples(st.just("heartbeat"), st.sampled_from(PATHS), st.sampled_from(["i1", "i2", "i3"])),
    st.tuples(st.just("rename"), st.sampled_from(PATHS), st.sampled_from(PATHS)),
    st.tuples(st.just("delete"), st.sampled_from(PATHS)),
    st.tuples(st.just("wait"), st.sampled_from([1_000, WINDOW // 2, WINDOW + 1])),
    st.tuples(st.just("restart")),
)
CELLS = [("c1", "c2"), ("c3", "c4"), ("c1", "c5")]


class _NoDelete(FolderRegistryStore):
    def delete(self, notebook_id):
        raise AssertionError("a record was deleted")


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(OPS, max_size=40))
def test_generated_sequences_keep_the_invariants(tmp_path_factory, ops):
    root = tmp_path_factory.mktemp("gen")
    files: set[str] = set()
    clock = FakeClock()
    ids = Counter()

    def build():
        return Registry(_NoDelete(root), clock, ids, path_exists=files.__contains__)

    reg = build()
    known: set[str] = set()
    for op in ops:
        kind = op[0]
        if kind == "register":
            _, path, inst, takeover, cells, _stable = op
            files.add(path)
            s = reg.register(registration(path=path, instance=inst, ids=CELLS[cells], takeover=takeover))
            known.add(s.notebookId)
            assert s.active == (reg.gate_ingest(inst, s.notebookId) == "ok")
        elif kind == "close":
            nb = reg.notebook_id_for_path(op[1])
            if nb:
                reg.close(nb, op[2])
        elif kind == "heartbeat":
            nb = reg.notebook_id_for_path(op[1])
            if nb:
                s = reg.status(nb, op[2])
                assert s.active == (reg.gate_ingest(op[2], nb) == "ok")
        elif kind == "rename":
            if op[1] in files and op[1] != op[2]:
                reg.note_rename(op[1], op[2])
                files.discard(op[1])
                files.add(op[2])
        elif kind == "delete":
            files.discard(op[1])
        elif kind == "wait":
            clock._ms += op[1]
        else:
            reg = build()
        # invariants
        records = FolderRegistryStore(root).load_all()
        rec_ids = [r["notebookId"] for r in records]
        assert len(rec_ids) == len(set(rec_ids))  # one record per notebook id
        assert known <= set(rec_ids)  # no record is ever deleted
        for nb in rec_ids:
            holders = [i for i in ("i1", "i2", "i3") if reg.gate_ingest(i, nb) == "ok"]
            assert len(holders) <= 1  # at most one active instance per notebook

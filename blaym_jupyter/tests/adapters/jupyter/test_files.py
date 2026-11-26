"""Outside changes to notebook files (S5, Task 18): the detector, the watcher and the save step."""

import json
import random

import pytest

from blaym_jupyter.adapters.jupyter.files import FileWatcher, choose_mode
from blaym_jupyter.recording.outside import OutsideChangeDetector, TipCell, content_hash, parse_notebook
from blaym_jupyter.recording.registry import RECORD_VERSION, Registry


class Ids:
    def __init__(self):
        self.n = 0

    def new(self):
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"


class Clock:
    def now_ms(self):
        return 1000


class Store:
    def __init__(self, rec):
        self.rec = rec

    def load_all(self):
        return [self.rec]

    def save(self, rec):
        self.rec = rec


class Sink:
    def __init__(self):
        self.writes = []

    def submit(self, write):
        self.writes.append(write)

    def types(self):
        return [e["type"] for w in self.writes for e in w.events]


class Sched:
    def __init__(self):
        self.jobs = []

    def call_later(self, ms, fn):
        job = [fn, False]
        self.jobs.append(job)
        return lambda: job.__setitem__(1, True)

    def run(self):
        """Run what is queued now, once (the watcher's own re-arming is left for the next call)."""
        jobs, self.jobs = self.jobs, []
        for fn, cancelled in jobs:
            if not cancelled:
                fn()


def nb(*cells, minor=5, meta=None):
    return {"nbformat": 4, "nbformat_minor": minor, "metadata": meta or {},
            "cells": [{"cell_type": t, "id": i, "source": s, "metadata": {},
                       **({"outputs": o, "execution_count": None} if t == "code" else {})}
                      for i, t, s, o in cells]}


def code(i, src, outs=()):
    return (i, "code", src, list(outs))


def write(path, notebook, indent=None):
    path.write_text(json.dumps(notebook, indent=indent))


@pytest.fixture
def rig(tmp_path):
    ids = Ids()
    rec = {"version": RECORD_VERSION, "notebookId": "nb1", "recording": True, "path": "a.ipynb",
           "previousPaths": [], "pathMissing": False, "cellIdsStable": True,
           "fingerprint": {"nbformat": 4, "sourceHash": "x"}, "lastWrittenHash": None, "createdMs": 1, "cleanClose": True}
    registry = Registry(Store(rec), Clock(), ids, path_exists=lambda p: (tmp_path / p).exists())
    sink, sched = Sink(), Sched()
    det = OutsideChangeDetector(Clock(), ids, editor="jupyterlab", origin_id="t")
    tip = {"cells": [TipCell("c1", "code", "x = 1")], "rev": {"c1": 1}}
    state = lambda nb_id: (tip["rev"], tip["cells"])  # noqa: E731
    watcher = FileWatcher(registry, sink, det, state, sched, ids, tmp_path, mode="polling")
    file = tmp_path / "a.ipynb"
    write(file, nb(code("c1", "x = 1")))
    registry.note_save("a.ipynb", content_hash(nb(code("c1", "x = 1"))))
    return type("Rig", (), dict(watcher=watcher, sink=sink, sched=sched, file=file, registry=registry, tip=tip))


def settle(r, path="a.ipynb"):
    r.watcher.schedule(path)
    for _ in range(6):
        r.sched.run()


def test_an_outside_rewrite_is_recorded_once_with_the_outside_mark(rig):
    write(rig.file, nb(code("c1", "x = 2"), code("c2", "y", [{"output_type": "stream", "name": "stdout", "text": "hi"}])))
    settle(rig)
    settle(rig)  # the same content again
    assert rig.sink.types() == ["changed_outside_editor"]
    event = rig.sink.writes[0].events[0]
    assert (event["authorHint"], event["authorConfidence"]) == ("outside", "known")
    cells = event["payload"]["cells"]
    assert [(c["cellId"], c["revision"]) for c in cells] == [("c1", 2), ("c2", 1)]  # continued from the tip
    assert cells[1]["outputs"][0]["runId"] == "snapshot:" + event["eventId"]


def test_formatting_only_rewrite_records_nothing(rig):
    write(rig.file, nb(code("c1", "x = 1")), indent=4)
    data = json.loads(rig.file.read_text())
    data["cells"][0]["source"] = ["x = ", "1"]
    rig.file.write_text(json.dumps(data, indent=1, sort_keys=True))
    settle(rig)
    assert rig.sink.types() == []


def test_a_rename_over_and_a_briefly_missing_file_are_one_change(rig, tmp_path):
    rig.file.unlink()
    rig.watcher.schedule("a.ipynb")
    rig.sched.run()  # missing: looks once more
    tmp = tmp_path / "a.tmp"
    write(tmp, nb(code("c1", "x = 5")))
    tmp.replace(rig.file)
    for _ in range(4):
        rig.sched.run()
    assert rig.sink.types() == ["changed_outside_editor"]


def test_an_unparseable_file_is_a_gap_closed_by_the_next_good_read(rig):
    rig.file.write_text('{"cells": [')
    settle(rig)
    assert rig.sink.types() == ["recording_gap_started"]
    gap = rig.sink.writes[0].events[0]["payload"]
    assert gap["source"] == "files"
    write(rig.file, nb(code("c1", "x = 1")))  # good again, nothing else changed
    settle(rig)
    assert rig.sink.types() == ["recording_gap_started", "recording_gap_ended"]
    assert rig.sink.writes[1].events[0]["payload"]["gapId"] == gap["gapId"]


def test_a_change_made_while_jupyter_was_stopped_is_found_by_the_first_look(rig):
    write(rig.file, nb(code("c1", "x = 'offline'")))
    rig.watcher._tick()
    for _ in range(4):
        rig.sched.run()
    assert rig.sink.types() == ["changed_outside_editor"]
    assert rig.registry.watched()[0][2] == content_hash(parse_notebook(rig.file.read_bytes()))


def test_unsaved_edits_in_the_tip_with_no_last_hash_record_nothing(rig):
    rig.tip["cells"] = [TipCell("c1", "code", "x = 99"), TipCell("c2", "code", "new")]  # unsaved browser edits
    rig.registry.note_save("a.ipynb", None)
    settle(rig)
    assert rig.sink.types() == []
    assert rig.registry.watched()[0][2] == content_hash(parse_notebook(rig.file.read_bytes()))


def test_editor_saves_are_never_outside_changes_whatever_the_order(rig):
    """Generated interleavings: the editor's save is a start, a write, other checks, a finish; an
    outside write is a plain write. Only outside writes may be recorded, each by its last content."""
    for seed in range(40):
        rig.sink.writes.clear()
        rnd = random.Random(seed)
        outside_wrote = False
        for step in range(rnd.randint(2, 6)):
            if rnd.random() < 0.5:
                text = f"x = 'editor {seed} {step}'"
                rig.watcher.save_started("a.ipynb")
                if rnd.random() < 0.5:
                    settle(rig)  # a check before the write
                write(rig.file, nb(code("c1", text)))
                if rnd.random() < 0.7:
                    settle(rig)  # a check between the write and the post-save step
                rig.watcher.save_finished("a.ipynb")
            else:
                write(rig.file, nb(code("c1", f"x = 'outside {seed} {step}'")))
                outside_wrote = True
                if rnd.random() < 0.5:
                    settle(rig)
        settle(rig)
        events = [e for w in rig.sink.writes for e in w.events]
        last_src = json.loads(rig.file.read_text())["cells"][0]["source"]
        if "editor" in last_src or not outside_wrote:
            # the file ends with an editor write: nothing recorded may carry editor content
            assert all("editor" not in e["payload"]["cells"][0]["code"] for e in events)
        assert len(events) <= 6 and all(e["type"] == "changed_outside_editor" for e in events)
        if events:
            assert "outside" in events[-1]["payload"]["cells"][0]["code"] or "editor" in last_src
        # the stored hash is always the file's own, so nothing is judged twice
        assert rig.registry.watched()[0][2] == content_hash(parse_notebook(rig.file.read_bytes()))


def test_unstable_cell_ids_are_aligned_with_the_tip(rig):
    det = OutsideChangeDetector(Clock(), Ids(), editor="jupyterlab", origin_id="t")
    old = nb(code(None, "a"), code(None, "b"), minor=4)
    for c in old["cells"]:
        del c["id"]
    tip = [TipCell("k1", "code", "a"), TipCell("k2", "code", "b")]
    new = json.loads(json.dumps(old))
    new["cells"][1]["source"] = "b2"
    new["cells"].append({"cell_type": "markdown", "source": "new", "metadata": {}})
    d = det.inspect("nb1", "a.ipynb", new, content_hash(old), {"k1": 3, "k2": 4}, tip)
    cells = d.event["payload"]["cells"]
    assert [(c["cellId"], c["revision"]) for c in cells[:2]] == [("k1", 3), ("k2", 5)]
    assert cells[2]["cellId"] not in ("k1", "k2") and cells[2]["revision"] == 1


def test_mnt_files_fall_back_to_polling(tmp_path):
    assert choose_mode(tmp_path, force="polling")[0] == "polling"
    mode, why = choose_mode(type("P", (), {"resolve": lambda s: s, "__str__": lambda s: "/mnt/c/x"})())
    assert mode == "polling" and "/mnt" in why


async def test_the_real_contents_manager_save_is_not_an_outside_change(jp_fetch, jp_serverapp, jp_root_dir):
    from blaym_jupyter.adapters.jupyter import SETTINGS_KEY
    from blaym_jupyter.adapters.jupyter.files import install_file_watcher

    from .test_contents import make_notebook, settle as settle_app, start_recording

    make_notebook(jp_root_dir, "s.ipynb")
    await start_recording(jp_fetch, "s.ipynb")
    host = await settle_app(jp_serverapp)
    watcher = install_file_watcher(jp_serverapp, host)
    try:
        model = {"type": "notebook", "format": "json", "content": nb(code("c1", "z = 1"))}
        await jp_serverapp.contents_manager.save(model, "s.ipynb")  # runs the pre and post save hooks
        assert not watcher._saving
        watcher.check("s.ipynb")
        assert watcher.stats["outside"] == 0
        stored = host.registry.watched()[0][2]
        assert stored == content_hash(parse_notebook((jp_root_dir / "s.ipynb").read_bytes()))
        write(jp_root_dir / "s.ipynb", nb(code("c1", "z = 2")))
        watcher.check("s.ipynb")
        assert watcher.stats["outside"] == 1
    finally:
        watcher.close()
        assert jp_serverapp.web_app.settings[SETTINGS_KEY] is host


def test_an_outside_change_asks_the_browser_for_a_resync_at_the_next_heartbeat(rig):
    rig.registry.register(__import__("blaym_jupyter_types").NotebookRegistration(
        editor="jupyterlab", instanceId="tab-1", path="a.ipynb", cellIdsStable=True, cellAliases={},
        fingerprint={"nbformat": "4.5", "sourceHash": "x", "cellIds": ["c1"]}))
    rig.registry.status("nb1", "tab-1")  # the first heartbeat after register: any resync is spent
    write(rig.file, nb(code("c1", "x = 'outside'")))
    settle(rig)
    assert rig.registry.status("nb1", "tab-1").needsResync is True
    assert rig.registry.status("nb1", "tab-1").needsResync is False


def test_an_editor_save_that_lands_while_a_check_is_reading_is_not_judged_by_the_stale_read(rig):
    """Outside write X is read; before the verdict the editor saves Y and its hook finishes. The
    generation check throws the stale verdict away, so Y is never recorded as outside and the
    stored hash ends as the file's own."""
    write(rig.file, nb(code("c1", "x = 'outside X'")))
    fired = []

    def state(nb_id):
        if not fired:
            fired.append(1)
            rig.watcher.save_started("a.ipynb")
            write(rig.file, nb(code("c1", "x = 'editor Y'")))
            rig.watcher.save_finished("a.ipynb")
        return rig.tip["rev"], rig.tip["cells"]

    rig.watcher._state = state
    settle(rig)
    codes = [e["payload"]["cells"][0]["code"] for w in rig.sink.writes for e in w.events]
    assert "x = 'editor Y'" not in codes
    assert rig.registry.watched()[0][2] == content_hash(parse_notebook(rig.file.read_bytes()))

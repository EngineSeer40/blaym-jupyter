"""Outside changes to notebook files: the detector, the watcher and the save step."""

import json

import pytest

from blaym_jupyter.adapters.jupyter.files import FileWatcher
from blaym_notebook.recording.outside import OutsideChangeDetector, TipCell, content_hash, parse_notebook
from blaym_notebook.recording.registry import RECORD_VERSION, Registry


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


def test_formatting_only_rewrite_is_ignored_and_an_unparseable_file_is_a_gap_closed_by_the_next_good_read(rig):
    write(rig.file, nb(code("c1", "x = 1")), indent=4)
    data = json.loads(rig.file.read_text())
    data["cells"][0]["source"] = ["x = ", "1"]
    rig.file.write_text(json.dumps(data, indent=1, sort_keys=True))
    settle(rig)
    assert rig.sink.types() == []
    rig.file.write_text('{"cells": [')
    settle(rig)
    assert rig.sink.types() == ["recording_gap_started"]
    gap = rig.sink.writes[0].events[0]["payload"]
    assert gap["source"] == "files"
    write(rig.file, nb(code("c1", "x = 1")))  # good again, nothing else changed
    settle(rig)
    assert rig.sink.types() == ["recording_gap_started", "recording_gap_ended"]
    assert rig.sink.writes[1].events[0]["payload"]["gapId"] == gap["gapId"]


def test_unsaved_edits_in_the_tip_with_no_last_hash_record_nothing(rig):
    rig.tip["cells"] = [TipCell("c1", "code", "x = 99"), TipCell("c2", "code", "new")]  # unsaved browser edits
    rig.registry.note_save("a.ipynb", None)
    settle(rig)
    assert rig.sink.types() == []
    assert rig.registry.watched()[0][2] == content_hash(parse_notebook(rig.file.read_bytes()))


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



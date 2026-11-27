"""Outside changes to notebook files: the save hooks against a real contents manager."""

import json

from blaym_notebook.recording.outside import content_hash, parse_notebook


def nb(*cells, minor=5, meta=None):
    return {"nbformat": 4, "nbformat_minor": minor, "metadata": meta or {},
            "cells": [{"cell_type": t, "id": i, "source": s, "metadata": {},
                       **({"outputs": o, "execution_count": None} if t == "code" else {})}
                      for i, t, s, o in cells]}


def code(i, src, outs=()):
    return (i, "code", src, list(outs))


def write(path, notebook, indent=None):
    path.write_text(json.dumps(notebook, indent=indent))



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



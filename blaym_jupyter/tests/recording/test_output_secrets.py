"""Secrets in blobs (S5, Task 5, review finding F22): a planted fake key inside any mime type of an
output blob must not be on disk when the blob is read back, after `Service.put_blob`.

`recording/` never imports `privacy` (the service hides secrets), so this runs the real service
over the in-memory ports.
"""

from __future__ import annotations

import json

import pytest

from blaym_jupyter.recording.outputs import item_from_output
from blaym_jupyter.tests.service.helpers import Rig, TOKEN
from blaym_jupyter_types import GetBlobRequest

KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # a fake OpenAI-style key
PASSWORD = "hunter2-Plant3d-Pw"
SECRETS = (KEY, PASSWORD)

PAD = ["    padding line of an ordinary traceback frame"] * 600  # over the inline limit: the error is a blob

# name -> (output, the planted values that must not be readable afterwards)
CASES = {
    "text/plain": ({"output_type": "display_data", "metadata": {},
                    "data": {"text/plain": f"config loaded, api key {KEY} ok", "image/png": "AAAA"}}, [KEY]),
    "text/plain quoted assignment": ({"output_type": "display_data", "metadata": {},
                                      "data": {"text/plain": f'password="{PASSWORD}"\napi_key = "{KEY}"', "image/png": "AAAA"}},
                                     [KEY, PASSWORD]),
    "text/html": ({"output_type": "display_data", "metadata": {},
                   "data": {"text/html": f"<table><tr><td>token</td><td>{KEY}</td></tr></table>"}}, [KEY]),
    "text/html script with a quoted assignment": ({"output_type": "display_data", "metadata": {},
                   "data": {"text/html": f'<script>var password = "{PASSWORD}"; var k = "{KEY}";</script>'}}, [KEY, PASSWORD]),
    "plotly json": ({"output_type": "display_data", "metadata": {},
                     "data": {"application/vnd.plotly.v1+json": {
                         "data": [{"name": f"series {KEY}", "x": [1]}],
                         "layout": {"title": {"text": f"auth {KEY}"}, "meta": {"password": PASSWORD}}}}}, [KEY, PASSWORD]),
    "error traceback": ({"output_type": "error", "ename": "ValueError", "evalue": f"bad key {KEY}",
                         "traceback": PAD + ['    client = Client(api_key="' + KEY + '", password="' + PASSWORD + '")',
                                             f"ValueError: bad key {KEY}"]}, [KEY, PASSWORD]),
    "stream": ({"output_type": "stream", "name": "stdout", "text": f"export OPENAI_API_KEY={KEY}\n" + "x" * 20000}, [KEY]),
}


@pytest.mark.parametrize("name", list(CASES))
def test_no_planted_secret_is_readable_from_a_stored_blob(name):
    output, planted = CASES[name]
    prepared = item_from_output(output)
    assert prepared.blob is not None, "these cases are built to need a blob"
    for value in planted:  # the case really plants what it says
        assert value in prepared.blob.decode("utf-8") or value in json.dumps(output)
    rig = Rig()
    put = rig.service.put_blob(TOKEN, rig.exp, prepared.blob)
    stored = rig.service.get_blob(TOKEN, GetBlobRequest(explorationId=rig.exp, contentHash=put.contentHash)).decode("utf-8")
    decoded = json.dumps(json.loads(stored), ensure_ascii=False)  # JSON escapes undone: the value itself
    leaked = [v for v in planted if v in stored or v in decoded]
    assert not leaked, f"{name}: {leaked} readable in the stored blob"

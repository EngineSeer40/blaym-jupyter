"""The standalone host as a real process: sign-in, a round trip over HTTP, the store lock, and
the end when standard input closes."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
from pathlib import Path

import pytest

from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import recorder

TOKEN = "k" * 40
PACKAGE = Path(__file__).resolve().parents[1]


def start(root: Path, token: str = TOKEN) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "blaym_vscode", "--root", str(root)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, cwd=PACKAGE.parent)


def ready(proc: subprocess.Popen, token: str = TOKEN) -> dict:
    proc.stdin.write(token + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def call(port: int, method: str, path: str, body=None, *, token: str | None = TOKEN, host: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    conn.request(method, path, data, headers)
    resp = conn.getresponse()
    out = resp.status, resp.read(), dict(resp.getheaders())
    conn.close()
    return out


@pytest.fixture
def service(tmp_path):
    proc = start(tmp_path / "store")
    line = ready(proc)
    assert line["ready"] is True and line["apiVersion"] == 1, line
    yield proc, line["port"]
    proc.stdin.close()
    proc.wait(timeout=20)


@pytest.mark.slow
def test_requests_without_the_token_or_with_a_foreign_host_are_refused(service):
    _, port = service
    assert call(port, "GET", "/info")[0] == 200
    assert call(port, "GET", "/info", token=None)[0] == 401
    assert call(port, "GET", "/info", token="x" * 40)[0] == 401
    assert call(port, "GET", "/info", host="evil.example")[0] == 403
    assert call(port, "GET", "/info", host="127.0.0.1:1")[0] == 403
    status, _, headers = call(port, "GET", "/info", host="localhost:%d" % port)
    assert status == 200 and not any(h.lower().startswith("access-control") for h in headers)


@pytest.mark.slow
def test_ingest_then_state_over_http_and_an_exception_in_handle_is_answered_with_500(service, monkeypatch):
    _, port = service
    reg = {"path": str(PACKAGE / "a.ipynb"), "editor": "vscode", "instanceId": "ext-1", "cellIdsStable": True,
           "fingerprint": {"nbformat": "4.5", "sourceHash": "h", "cellIds": ["c1"]}, "cellAliases": {}}
    status, body, _ = call(port, "POST", "/notebooks/register", reg)
    assert status == 200, body
    exp = json.loads(body)["explorationId"]
    s = Scenario()
    s.start(cells=[("c1", "needle = 1")])
    s.edit("c1", "needle = 2")
    status, body, _ = call(port, "POST", "/ingest", {
        "explorationId": exp, "editor": "vscode", "instanceId": "ext-1",
        "events": [{**recorder(e), "explorationId": exp} for e in s.events]})
    out = json.loads(body)
    assert status == 200 and [o["status"] for o in out["outcomes"]] == ["accepted", "accepted"], body
    branch = out["outcomes"][0]["branchId"]
    status, body, _ = call(port, "GET", f"/state?explorationId={exp}&branchId={branch}&tick=2")
    assert status == 200 and json.loads(body)["status"] == "ok", body

    import threading

    from blaym_vscode import server as server_module
    from blaym_vscode.auth import TokenAuthorizer

    def boom(*args):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(server_module, "handle", boom)
    srv = server_module.make_server(None, TokenAuthorizer(TOKEN))  # type: ignore[arg-type]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for _ in range(2):
            status, body, _ = call(srv.server_address[1], "GET", "/info")
            assert status == 500 and TOKEN.encode() not in body
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.slow
def test_a_second_service_on_the_same_store_is_refused_and_the_lock_goes_when_standard_input_closes(service, tmp_path):
    first, _ = service
    second = start(tmp_path / "store")
    assert ready(second) == {"ready": False, "error": "store_in_use"}
    assert second.wait(timeout=20) != 0
    first.stdin.close()
    assert first.wait(timeout=20) == 0
    again = start(tmp_path / "store")
    assert ready(again)["ready"] is True
    again.stdin.close()
    again.wait(timeout=20)

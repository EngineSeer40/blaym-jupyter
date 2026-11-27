"""The API dispatcher over the memory-port service: the route table, sign-in, hostile input,
busy store, the one-recorder gate, and one round trip of ingest and reads.

`fixtures/api/routes.json` is generated from `api/routes.py`. To rewrite it after a change to the
route table, run `BLAYM_REGEN_API_ROUTES=1 .venv/bin/python -m pytest blaym_notebook/tests/api -k routes_file`;
without the flag the test only compares.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blaym_jupyter.adapters.jupyter.auth import Credential, JupyterAuthorizer
from blaym_notebook.api import ROUTES, ApiRequest, Recording, handle
from blaym_notebook.api.dispatch import _HANDLERS, MAX_JSON_BODY_BYTES
from blaym_notebook.api.routes import as_json, match
from blaym_notebook.recording.registry import Registry
from blaym_notebook.recording.registry_store import FolderRegistryStore
from blaym_notebook.service.auth import FixedTokenAuthorizer
from blaym_notebook.service.core import Service, ServiceBusy
from blaym_notebook.testing.fakes import FakeClock, IdFactory
from blaym_notebook.testing.memory import MemoryBlobStore, MemoryEventStore, MemorySearchIndex
from blaym_notebook.tests.history.scenario import Scenario
from blaym_notebook.tests.service.helpers import recorder
from blaym_notebook_types import (
    EventsSinceResponse,
    HistoryOutline,
    IngestResponse,
    NotebookStatus,
    PutBlobResponse,
    RebuildResult,
    SearchResponse,
)

TOKEN = "t0ken"
ROUTES_FILE = Path(__file__).resolve().parents[5] / "fixtures" / "api" / "routes.json"


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock()
        self.service = Service(MemoryEventStore(), MemoryBlobStore(), FixedTokenAuthorizer(TOKEN), self.clock,
                               new_id=IdFactory("svc"), search_index=MemorySearchIndex(), index_every_edit=True)
        ids = IdFactory("nb")
        self.registry = Registry(FolderRegistryStore(tmp_path), self.clock, type("I", (), {"new": lambda s: ids()})())
        self.rec = Recording(self.service, self.registry)

    def call(self, method, path, *, query=None, body=b"", cred=TOKEN):
        if not isinstance(body, (bytes, bytearray)) and not hasattr(body, "__next__"):
            body = json.dumps(body).encode()
        return handle(ApiRequest(method, path, query or {}, {}, body), cred, self.rec)

    def register(self, instance="tab-1", path="a.ipynb", **extra):
        body = {"path": path, "editor": "jupyterlab", "instanceId": instance, "cellIdsStable": True,
                "fingerprint": {"nbformat": "4.5", "sourceHash": "h", "cellIds": ["c1"]}, "cellAliases": {}, **extra}
        r = self.call("POST", "/notebooks/register", body=body)
        assert r.status == 200, r.body
        return NotebookStatus.model_validate_json(r.body)

    def ingest_body(self, exp, events, instance="tab-1"):
        return {"explorationId": exp, "editor": "jupyterlab", "instanceId": instance,
                "events": [{**recorder(e), "explorationId": exp} for e in events]}

    def seed(self):
        """A registered notebook with a first snapshot and an edit ingested."""
        status = self.register()
        s = Scenario()
        s.start(cells=[("c1", "needle = 1")])
        s.edit("c1", "needle = 2")
        r = self.call("POST", "/ingest", body=self.ingest_body(status.explorationId, s.events))
        out = IngestResponse.model_validate_json(r.body)
        assert [o.root.status for o in out.outcomes] == ["accepted", "accepted"], r.body
        return status.explorationId, out.outcomes[0].root.branchId


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _json(response):
    return json.loads(response.body)


def _raw(rig, method, path, body=b"", query=None, cred=TOKEN):
    return handle(ApiRequest(method, path, query or {}, {}, body), cred, rig.rec)


SECRET = "sk-LIVE-9f8e7d6c5b4a"
_ANY = {"explorationId": "e", "notebookId": "n", "contentHash": "sha256:" + "0" * 64}
POST_ROUTES = [r.path for r in ROUTES if r.method == "POST"]


def test_ingest_and_the_reads_round_trip(rig):
    exp, branch = rig.seed()
    state = rig.call("GET", "/state", query={"explorationId": exp, "branchId": branch, "tick": "2"})
    assert state.status == 200 and RebuildResult.model_validate_json(state.body).root.status == "ok"
    assert HistoryOutline.model_validate_json(rig.call("GET", "/outline", query={"explorationId": exp}).body)
    since = rig.call("GET", "/events-since", query={"explorationId": exp, "branchId": branch, "afterTick": "1"})
    assert [e.root.tick for e in EventsSinceResponse.model_validate_json(since.body).events] == [2]
    hits = rig.call("GET", "/search", query={"explorationId": exp, "query": "needle", "limit": "5"})
    assert SearchResponse.model_validate_json(hits.body).hits


def test_register_status_heartbeat_and_close(rig):
    s = rig.register()
    assert s.active and not s.recording
    path = f"/notebooks/{s.notebookId}/status"
    assert NotebookStatus.model_validate_json(rig.call("GET", path, query={"instanceId": "tab-1"}).body).active
    assert rig.call("POST", "/notebooks/close", body={"notebookId": s.notebookId, "instanceId": "tab-1"}).status == 204
    assert NotebookStatus.model_validate_json(rig.call("GET", path, query={"instanceId": "tab-1"}).body).needsRegister
    unknown = NotebookStatus.model_validate_json(rig.call("GET", "/notebooks/nope/status", query={"instanceId": "t9"}).body)
    assert unknown.needsRegister and not unknown.active


def test_blobs_stream_in_and_come_back(rig):
    exp = rig.register().explorationId
    put = rig.call("PUT", f"/blobs/{exp}", body=iter([b"hello ", b"streamed ", b"world"]))
    assert put.status == 200
    got = rig.call("GET", f"/blobs/{exp}/{PutBlobResponse.model_validate_json(put.body).contentHash}")
    assert got.status == 200 and got.body == b"hello streamed world"
    assert rig.call("GET", f"/blobs/{exp}/sha256:{'0' * 64}").status == 404


def test_timeline_and_events_between_routes(rig):
    from blaym_notebook_types import EventsBetweenResponse, TimelineIndexResponse
    exp, branch = rig.seed()
    q = {"explorationId": exp, "branchId": branch}
    tl = rig.call("GET", "/timeline", query={**q, "limit": "1"})
    body = TimelineIndexResponse.model_validate_json(tl.body)
    assert tl.status == 200 and [t.root for t in body.ticks] == [1] and body.truncated and body.nextTick == 2
    between = rig.call("GET", "/events-between", query={**q, "fromTick": "2", "toTick": "2"})
    assert [e.root.tick for e in EventsBetweenResponse.model_validate_json(between.body).events] == [2]
    assert rig.call("GET", "/timeline", query={**q, "branchId": "nope"}).status == 404


def test_unknown_route_404_wrong_method_405_unknown_history_404(rig):
    assert rig.call("GET", "/nothing").status == 404
    r = rig.call("GET", "/ingest")
    assert r.status == 405 and r.headers["Allow"] == "POST"
    assert rig.call("GET", "/outline", query={"explorationId": "ghost"}).status == 404


def test_the_route_table_matches_its_handlers_and_the_contract_routes_file():
    """Set BLAYM_REGEN_API_ROUTES=1 to rewrite the file; otherwise it must equal the table."""
    assert {r.name for r in ROUTES} == set(_HANDLERS)
    for r in ROUTES:
        found, _, _ = match(r.method, r.path.format(**_ANY))
        assert found is r
    text = json.dumps(as_json(), indent=2) + "\n"
    if os.environ.get("BLAYM_REGEN_API_ROUTES"):
        ROUTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        ROUTES_FILE.write_text(text)
    assert ROUTES_FILE.read_text() == text


def test_a_request_without_a_valid_sign_in_is_refused_and_a_credential_cannot_be_forged(rig):
    other = JupyterAuthorizer().mint()
    forged = ("wrong", None, "", b"x" * 32, object(), Credential(b"x" * 32), other, {"_key": b""})
    before = len(rig.registry._records)
    for route in ROUTES:
        path = route.path.format(**_ANY)
        for cred in forged:
            r = rig.call(route.method, path, body={"path": "z.ipynb"}, query={"instanceId": "i"}, cred=cred)
            assert r.status == 401, (route.name, cred)
    assert len(rig.registry._records) == before  # the registry was not reached either
    assert "key" not in repr(Credential(b"secretkey")).lower().replace("credential", "")


def test_bad_and_hostile_bodies_are_400_and_an_oversized_body_is_413(rig):
    deep = [b"[" * 100_000, b"{" * 100_000, b'{"a":' * 50_000 + b"1" + b"}" * 50_000]
    bodies = [b"{not json", b"{}", b"[]", b"\xff\xfe\x00bad", b"", b"null", b'"s"', b"12", b'{"events": 5}', *deep]
    for path in POST_ROUTES:
        for body in bodies:
            r = _raw(rig, "POST", path, body)
            assert r.status == 400, (path, body[:20], r.status)
            assert "Traceback" not in _json(r)["error"] and len(_json(r)["error"]) < 300
    exp = rig.register().explorationId
    assert _raw(rig, "POST", "/ingest", b" " * (MAX_JSON_BODY_BYTES + 1)).status == 413
    for path, query in [("/state", {"explorationId": "e", "branchId": "b", "tick": "9" * 6000}),
                        ("/state", {"explorationId": "e", "branchId": "b", "tick": "soon"}),
                        ("/events-since", {"explorationId": "e"}), ("/search", {"explorationId": "e"})]:
        assert _raw(rig, "GET", path, query=query).status == 400
    assert _raw(rig, "PUT", "/blobs/..%2Fx", b"x").status == 400
    for method, bad in (("PUT", "/blobs/a%2fb"), ("PUT", "/blobs/a\x00b"), ("GET", f"/blobs/{exp}/.."),
                        ("GET", f"/blobs/{exp}/" + "z" * 5000)):
        assert _raw(rig, method, bad, b"x").status in (400, 404)


def test_a_secret_is_never_echoed_in_an_error(rig):
    for body in ({"explorationId": SECRET, "editor": SECRET, "instanceId": 1, "events": SECRET},
                 {"notebookId": SECRET, "instanceId": 5}, {"path": SECRET, "editor": SECRET},
                 {"explorationId": "e", "cellId": "c", "code": 12345, "secret": SECRET}):
        for path in POST_ROUTES:
            assert SECRET not in _raw(rig, "POST", path, json.dumps(body).encode()).body.decode(), path
    for path, q in (("/outline", {"explorationId": SECRET}),
                    ("/state", {"explorationId": SECRET, "branchId": SECRET, "tick": "1"}),
                    ("/search", {"explorationId": SECRET, "query": SECRET, "kinds": SECRET}),
                    ("/events-since", {"explorationId": SECRET, "branchId": SECRET, "afterTick": SECRET})):
        assert SECRET not in _raw(rig, "GET", path, query=q).body.decode(), path
    assert SECRET not in _raw(rig, "PUT", f"/blobs/{SECRET}", b"x").body.decode()
    assert SECRET not in _raw(rig, "GET", f"/nope/{SECRET}").body.decode()


def test_a_busy_store_is_503_with_retry_after_and_the_committed_outcomes(rig):
    def busy(cred, request):
        raise ServiceBusy("locked", [])

    rig.service.ingest = busy
    status = rig.register()
    r = rig.call("POST", "/ingest", body=rig.ingest_body(status.explorationId, []))
    assert r.status == 503 and r.headers["Retry-After"] == "1" and _json(r)["outcomes"] == []


def test_a_second_instance_is_refused_before_the_service_and_cannot_take_the_notebook(rig):
    first = rig.register("tab-1")
    second = rig.register("tab-2")
    assert first.active and not second.active and second.reason.value == "other_recorder_active"
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    calls = []
    real = rig.service.ingest
    rig.service.ingest = lambda cred, req: calls.append(req) or real(cred, req)

    refused = rig.call("POST", "/ingest", body=rig.ingest_body(first.explorationId, s.events, "tab-2"))
    out = IngestResponse.model_validate_json(refused.body)
    assert refused.status == 200 and calls == []
    assert [(o.root.status, o.root.reason.value, o.root.eventId) for o in out.outcomes] == [
        ("refused", "not_active_recorder", recorder(s.events[0])["eventId"])]
    assert rig.call("GET", "/outline", query={"explorationId": first.explorationId}).status == 404  # nothing stored

    assert rig.call("POST", "/notebooks/close", body={"notebookId": first.notebookId, "instanceId": "tab-2"}).status == 204
    again = rig.call("GET", f"/notebooks/{first.notebookId}/status", query={"instanceId": "tab-1"})
    assert NotebookStatus.model_validate_json(again.body).active  # an inactive close changes nothing

    ok = rig.call("POST", "/ingest", body=rig.ingest_body(first.explorationId, s.events, "tab-1"))
    assert IngestResponse.model_validate_json(ok.body).outcomes[0].root.status == "accepted" and len(calls) == 1
    never = rig.call("POST", "/ingest", body=rig.ingest_body("never-registered", s.events))
    assert IngestResponse.model_validate_json(never.body).outcomes[0].root.reason.value == "not_active_recorder"


def test_a_lost_session_gets_one_closed_gap_after_the_next_first_snapshot(tmp_path):
    lost = Rig(tmp_path / "lost")
    (tmp_path / "lost").mkdir()
    exp, branch = lost.seed()  # the page never closes: the server record stays open
    again = Rig(tmp_path / "lost")  # a restart, then a new page of the same notebook
    status = again.register("tab-2")
    s = Scenario()
    s.start(cells=[("c1", "needle = 2")])
    r = again.call("POST", "/ingest", body=again.ingest_body(status.explorationId, s.events, "tab-2"))
    assert IngestResponse.model_validate_json(r.body).outcomes[0].root.status == "accepted"
    since = again.call("GET", "/events-since", query={"explorationId": exp, "branchId": branch, "afterTick": "0"})
    events = EventsSinceResponse.model_validate_json(since.body).events
    assert [e.root.type for e in events][-3:] == ["notebook_snapshot", "recording_gap_started", "recording_gap_ended"]
    started = events[-2].root.payload
    assert (started.source.value, started.reason.value) == ("browser", "unknown")


def test_the_api_refuses_writes_it_does_not_own(rig):
    first = rig.register("tab-1", "a.ipynb")
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    # the server's own instance id cannot ingest through the API
    r = rig.call("POST", "/ingest", body=rig.ingest_body(first.explorationId, s.events, "blaym-server"))
    assert IngestResponse.model_validate_json(r.body).outcomes[0].root.status == "refused"
    # events for another history cannot ride in an envelope the caller holds
    theirs = rig.register("tab-9", "b.ipynb", fingerprint={"nbformat": "4.5", "sourceHash": "other", "cellIds": ["z9"]})
    assert theirs.explorationId != first.explorationId
    body = rig.ingest_body(first.explorationId, s.events, "tab-1")
    for ev in body["events"]:
        ev["explorationId"] = theirs.explorationId
    out = IngestResponse.model_validate_json(rig.call("POST", "/ingest", body=body).body)
    assert all(o.root.status != "accepted" for o in out.outcomes)
    outline = rig.call("GET", "/outline", query={"explorationId": theirs.explorationId})
    assert outline.status == 404 or json.loads(outline.body).get("highestTick", 0) == 0


class _Sink:
    def __init__(self):
        self.writes = []

    def submit(self, write):
        self.writes.append(write)


def test_a_rename_is_recorded_as_notebook_renamed_and_the_registry_follows_the_new_path(rig):
    status = rig.register("tab-1", path="a.ipynb", setRecording=True)
    sink = _Sink()
    rig.rec.editor = "vscode"
    rig.rec.sink, rig.rec.clock, rig.rec.ids = sink, rig.clock, type("I", (), {"new": lambda s: "ev-1"})()
    r = rig.call("POST", "/notebooks/rename", body={
        "notebookId": status.notebookId, "instanceId": "tab-1", "oldPath": "a.ipynb", "newPath": "b.ipynb"})
    assert r.status == 200 and NotebookStatus.model_validate_json(r.body).active
    assert rig.registry.notebook_id_for_path("b.ipynb") == status.notebookId
    [write] = sink.writes
    assert [(e["type"], e["payload"], e["editor"], e["originId"]) for e in write.events] == [
        ("notebook_renamed", {"oldPath": "a.ipynb", "newPath": "b.ipynb"}, "vscode", "tab-1")]


def test_a_save_notice_from_an_instance_that_is_not_the_active_recorder_is_refused(rig):
    first = rig.register("tab-1")
    rig.register("tab-2")
    seen = []
    rig.rec.files = type("F", (), {"save_started": lambda s, p: seen.append(("started", p)),
                                   "save_finished": lambda s, p: seen.append(("finished", p))})()
    body = {"notebookId": first.notebookId, "path": "a.ipynb", "phase": "started"}
    assert rig.call("POST", "/notebooks/save", body={**body, "instanceId": "tab-2"}).status == 409 and seen == []
    assert rig.call("POST", "/notebooks/save", body={**body, "instanceId": "tab-1"}).status == 204
    assert seen == [("started", "a.ipynb")]
    assert rig.call("POST", "/notebooks/save", body={**body, "instanceId": "tab-1", "phase": "finished"}).status == 204
    assert seen == [("started", "a.ipynb"), ("finished", "a.ipynb")]

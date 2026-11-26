"""The API dispatcher over the memory-port service (S5, Task 8).

Every route has a happy path, a bad-body case and an unauthorised case; response bodies are
validated against the generated models; the gate refuses a second recorder instance before the
service is called; the heartbeat reports `needsRegister` for an unknown instance.

`fixtures/api/routes.json` is generated from `api/routes.py`. To rewrite it after a change to the
route table, run `BLAYM_REGEN_API_ROUTES=1 .venv/bin/python -m pytest blaym_jupyter/tests/api -k routes_file`;
without the flag the test only compares.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blaym_jupyter.api import ROUTES, ApiRequest, Recording, handle
from blaym_jupyter.api.dispatch import _HANDLERS
from blaym_jupyter.api.routes import as_json, match
from blaym_jupyter.recording.registry import Registry
from blaym_jupyter.recording.registry_store import FolderRegistryStore
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.service.core import Service, ServiceBusy
from blaym_jupyter.testing.fakes import FakeClock, IdFactory
from blaym_jupyter.testing.memory import MemoryBlobStore, MemoryEventStore, MemorySearchIndex
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import recorder
from blaym_jupyter_types import (
    EventsSinceResponse,
    HistoryOutline,
    IngestResponse,
    NotebookStatus,
    PrepareOutputsResponse,
    PutBlobResponse,
    RebuildResult,
    ResolveCellRevisionResponse,
    SearchResponse,
    ServiceInfo,
)

TOKEN = "t0ken"
ROUTES_FILE = Path(__file__).resolve().parents[3] / "fixtures" / "api" / "routes.json"


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


# -- happy paths, one per route, with the response validated against the generated model --------


def test_info(rig):
    r = rig.call("GET", "/info")
    assert r.status == 200 and ServiceInfo.model_validate_json(r.body).apiVersion.root == 1


def test_register_status_and_close(rig):
    s = rig.register()
    assert s.active and not s.recording
    r = rig.call("GET", f"/notebooks/{s.notebookId}/status", query={"instanceId": "tab-1"})
    assert r.status == 200 and NotebookStatus.model_validate_json(r.body).active
    assert rig.call("POST", "/notebooks/close", body={"notebookId": s.notebookId, "instanceId": "tab-1"}).status == 204
    again = rig.call("GET", f"/notebooks/{s.notebookId}/status", query={"instanceId": "tab-1"})
    assert NotebookStatus.model_validate_json(again.body).needsRegister


def test_heartbeat_of_an_unknown_instance_asks_to_register(rig):
    r = rig.call("GET", "/notebooks/nope/status", query={"instanceId": "tab-9"})
    status = NotebookStatus.model_validate_json(r.body)
    assert r.status == 200 and status.needsRegister and not status.active


def test_ingest_and_the_reads(rig):
    exp, branch = rig.seed()
    state = rig.call("GET", "/state", query={"explorationId": exp, "branchId": branch, "tick": "2"})
    assert state.status == 200 and RebuildResult.model_validate_json(state.body).root.status == "ok"
    assert HistoryOutline.model_validate_json(rig.call("GET", "/outline", query={"explorationId": exp}).body)
    since = rig.call("GET", "/events-since", query={"explorationId": exp, "branchId": branch, "afterTick": "1"})
    assert [e.root.tick for e in EventsSinceResponse.model_validate_json(since.body).events] == [2]
    hits = rig.call("GET", "/search", query={"explorationId": exp, "query": "needle", "limit": "5"})
    assert SearchResponse.model_validate_json(hits.body).hits


def test_timeline_and_events_between_routes(rig):
    from blaym_jupyter_types import EventsBetweenResponse, TimelineIndexResponse
    exp, branch = rig.seed()
    q = {"explorationId": exp, "branchId": branch}
    tl = rig.call("GET", "/timeline", query={**q, "limit": "1"})
    body = TimelineIndexResponse.model_validate_json(tl.body)
    assert tl.status == 200 and [t.root for t in body.ticks] == [1] and body.truncated and body.nextTick == 2
    between = rig.call("GET", "/events-between", query={**q, "fromTick": "2", "toTick": "2"})
    assert [e.root.tick for e in EventsBetweenResponse.model_validate_json(between.body).events] == [2]
    assert rig.call("GET", "/timeline", query={**q, "branchId": "nope"}).status == 404
    assert rig.call("GET", "/events-between", query={**q, "fromTick": "2"}).status == 400
    assert rig.call("GET", "/timeline", query={**q, "limit": "9999"}).status == 400  # over the contract maximum


def test_resolve_cell_revision(rig):
    exp, _ = rig.seed()
    r = rig.call("POST", "/cells/revision", body={"explorationId": exp, "cellId": "c1", "code": "needle = 2"})
    assert ResolveCellRevisionResponse.model_validate_json(r.body).root.status == "ok"


def test_put_blob_streams_chunks_and_get_blob_returns_the_bytes(rig):
    exp = rig.register().explorationId
    chunks = iter([b"hello ", b"streamed ", b"world"])
    put = rig.call("PUT", f"/blobs/{exp}", body=chunks)
    assert put.status == 200
    h = PutBlobResponse.model_validate_json(put.body).contentHash
    got = rig.call("GET", f"/blobs/{exp}/{h}")
    assert got.status == 200 and got.body == b"hello streamed world"
    assert got.headers["Content-Type"] == "application/octet-stream"
    assert rig.call("PUT", f"/blobs/{exp}", body=b"x" * 3_000_000).status == 200  # bytes are chunked


def test_prepare_outputs_stores_blobs_and_matches_the_item_order(rig):
    exp = rig.register().explorationId
    big = "x" * 40_000
    r = rig.call("POST", "/outputs/prepare", body={"explorationId": exp, "outputs": [
        {"output_type": "stream", "name": "stdout", "text": "hi\n"},
        {"output_type": "display_data", "data": {"text/plain": big, "image/png": "AAAA"}, "metadata": {}},
        {"output_type": "error", "ename": "E", "evalue": "v", "traceback": ["tb"]}]})
    items = PrepareOutputsResponse.model_validate_json(r.body).items
    assert [i.outputType.value for i in items] == ["stream", "display_data", "error"]
    stored = rig.call("GET", f"/blobs/{exp}/{items[1].contentHash}")
    assert stored.status == 200 and b"image/png" in stored.body


# -- bad bodies --------------------------------------------------------------------------------

POST_ROUTES = ["/ingest", "/outputs/prepare", "/cells/revision", "/notebooks/register", "/notebooks/close"]


@pytest.mark.parametrize("path", POST_ROUTES)
@pytest.mark.parametrize("body", [b"{not json", b"{}", b"[]"])
def test_a_bad_body_is_400_with_a_short_message(rig, path, body):
    r = rig.call("POST", path, body=body)
    assert r.status == 400
    message = _json(r)["error"]
    assert message and "Traceback" not in message and len(message) < 300


def test_a_bad_body_does_not_echo_its_content(rig):
    r = rig.call("POST", "/cells/revision", body={"explorationId": "e", "cellId": "c", "code": 12345,
                                                  "secret": "sk-live-abcdefghijkl"})
    assert r.status == 400 and b"sk-live" not in r.body


@pytest.mark.parametrize("path,query", [
    ("/notebooks/x/status", {}), ("/state", {"explorationId": "e", "branchId": "b"}),
    ("/state", {"explorationId": "e", "branchId": "b", "tick": "soon"}), ("/outline", {}),
    ("/events-since", {"explorationId": "e"}), ("/search", {"explorationId": "e"}),
])
def test_a_bad_query_is_400(rig, path, query):
    assert rig.call("GET", path, query=query).status == 400


def test_a_bad_history_id_is_400_for_blobs_and_an_unknown_blob_is_404(rig):
    assert rig.call("PUT", "/blobs/..%2Fx", body=b"x").status == 400
    exp = rig.register().explorationId
    assert rig.call("GET", f"/blobs/{exp}/sha256:{'0' * 64}").status == 404
    assert rig.call("PUT", "/blobs/bad id", body=b"x").status == 400


def test_prepare_outputs_with_an_unknown_output_type_is_400(rig):
    exp = rig.register().explorationId
    r = rig.call("POST", "/outputs/prepare", body={"explorationId": exp, "outputs": [{"output_type": "nope"}]})
    assert r.status == 400


# -- sign-in -----------------------------------------------------------------------------------

_ANY = {"explorationId": "e", "notebookId": "n", "contentHash": "sha256:" + "0" * 64}


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.name)
@pytest.mark.parametrize("cred", ["wrong", None])
def test_every_route_refuses_a_bad_credential(rig, route, cred):
    path = route.path.format(**_ANY)
    before = len(rig.registry._records)
    r = rig.call(route.method, path, body={"path": "z.ipynb"}, query={"instanceId": "i"}, cred=cred)
    assert r.status == 401
    assert len(rig.registry._records) == before  # the registry was not reached either


# -- errors from the service --------------------------------------------------------------------


def test_unknown_history_is_404(rig):
    q = {"explorationId": "ghost", "branchId": "main", "afterTick": "0"}
    assert rig.call("GET", "/events-since", query=q).status == 404
    assert rig.call("GET", "/outline", query={"explorationId": "ghost"}).status == 404


def test_a_busy_store_is_503_with_retry_after_and_the_committed_outcomes(rig):
    def busy(cred, request):
        raise ServiceBusy("locked", [])

    rig.service.ingest = busy
    status = rig.register()
    r = rig.call("POST", "/ingest", body=rig.ingest_body(status.explorationId, []))
    assert r.status == 503 and r.headers["Retry-After"] == "1" and _json(r)["outcomes"] == []


def test_a_busy_store_on_the_startup_gap_keeps_the_flag_for_the_next_ingest(rig):
    status = rig.register()
    rig.registry._startup_unclean[status.explorationId] = True  # the last session did not close cleanly
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    real, calls = rig.service.ingest, []

    def flaky(cred, request):
        calls.append(request)
        if len(calls) == 2:  # the start-up gap
            raise ServiceBusy("locked", [])
        return real(cred, request)

    rig.service.ingest = flaky
    body = rig.ingest_body(status.explorationId, s.events)
    assert rig.call("POST", "/ingest", body=body).status == 503  # the gap hit a busy store
    assert rig.registry.startup_unclean(status.explorationId)  # so the flag is still set
    retry = rig.call("POST", "/ingest", body=body)  # the browser resends the batch
    assert retry.status == 200 and len(calls) == 4  # the snapshot again (duplicate), then the gap
    assert not rig.registry.startup_unclean(status.explorationId)


def test_unknown_route_404_and_wrong_method_405(rig):
    assert rig.call("GET", "/nothing").status == 404
    r = rig.call("GET", "/ingest")
    assert r.status == 405 and r.headers["Allow"] == "POST"


# -- the gate -------------------------------------------------------------------------------------


def test_a_second_instance_is_refused_before_the_service(rig):
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

    ok = rig.call("POST", "/ingest", body=rig.ingest_body(first.explorationId, s.events, "tab-1"))
    assert IngestResponse.model_validate_json(ok.body).outcomes[0].root.status == "accepted" and len(calls) == 1


def test_an_unregistered_instance_is_refused(rig):
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    r = rig.call("POST", "/ingest", body=rig.ingest_body("never-registered", s.events))
    assert IngestResponse.model_validate_json(r.body).outcomes[0].root.reason.value == "not_active_recorder"


# -- the table ------------------------------------------------------------------------------------


def test_every_route_has_a_handler_and_the_table_matches():
    assert {r.name for r in ROUTES} == set(_HANDLERS)
    for r in ROUTES:
        found, _, _ = match(r.method, r.path.format(**_ANY))
        assert found is r


def test_routes_file_is_generated_from_the_table():
    """Set BLAYM_REGEN_API_ROUTES=1 to rewrite the file; otherwise it must equal the table."""
    text = json.dumps(as_json(), indent=2) + "\n"
    if os.environ.get("BLAYM_REGEN_API_ROUTES"):
        ROUTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        ROUTES_FILE.write_text(text)
    assert ROUTES_FILE.read_text() == text


# -- Task 20: the start-up gap (O7) --------------------------------------------------------------


def test_a_lost_session_gets_one_closed_gap_after_the_next_first_snapshot_and_a_clean_one_none(tmp_path):
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
    types = [e.root.type for e in events]
    assert types[-3:] == ["notebook_snapshot", "recording_gap_started", "recording_gap_ended"]
    started = events[-2].root.payload
    assert (started.source.value, started.reason.value) == ("browser", "unknown")
    again.call("POST", "/ingest", body=again.ingest_body(exp, Scenario().events, "tab-2"))  # no second gap

    clean = Rig(tmp_path / "clean")
    (tmp_path / "clean").mkdir()
    exp2, branch2 = clean.seed()
    clean.call("POST", "/notebooks/close", body={"notebookId": exp2, "instanceId": "tab-1"})
    reopened = Rig(tmp_path / "clean")
    st = reopened.register("tab-2")
    s2 = Scenario()
    s2.start(cells=[("c1", "needle = 2")])
    reopened.call("POST", "/ingest", body=reopened.ingest_body(st.explorationId, s2.events, "tab-2"))
    since = reopened.call("GET", "/events-since", query={"explorationId": exp2, "branchId": branch2, "afterTick": "0"})
    assert not [e for e in EventsSinceResponse.model_validate_json(since.body).events
                if e.root.type.startswith("recording_gap")]

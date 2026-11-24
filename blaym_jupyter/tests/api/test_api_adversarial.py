"""Reviewer's adversarial cases for the dispatcher: odd path segments, hostile bodies, the gate."""

from __future__ import annotations

import json

import pytest

from blaym_jupyter.adapters.jupyter.auth import Credential, JupyterAuthorizer
from blaym_jupyter.api import ROUTES, ApiRequest, handle
from blaym_jupyter.tests.api.test_api import Rig, TOKEN, rig  # noqa: F401
from blaym_jupyter.tests.history.scenario import Scenario
from blaym_jupyter.tests.service.helpers import recorder
from blaym_jupyter_types import IngestResponse, NotebookStatus

SECRET = "sk-LIVE-9f8e7d6c5b4a"


def _raw(rig, method, path, body=b"", query=None, cred=TOKEN):
    return handle(ApiRequest(method, path, query or {}, {}, body), cred, rig.rec)


@pytest.mark.parametrize("path", ["/blobs/..", "/blobs/", "/blobs/" + "a" * 100_000, "/blobs/a%2fb", "/blobs/a\x00b",
                                  "/blobs/‮"])
def test_odd_history_ids_in_blob_paths_are_400_or_404_and_never_echoed(rig, path):
    for method in ("PUT", "GET"):
        r = _raw(rig, method, path, b"data")
        assert r.status in (400, 404, 405), (method, r.status)
        assert "aaaa" not in r.body.decode() and "\x00" not in r.body.decode()


@pytest.mark.parametrize("path", ["/blobs/../sha256:" + "0" * 64, "/blobs/e/..", "/blobs/e/%2e%2e", "/blobs/e/" + "z" * 5000])
def test_odd_hash_segments_are_rejected(rig, path):
    r = _raw(rig, "GET", path)
    assert r.status in (400, 404)
    assert "zzzz" not in r.body.decode()


def test_status_with_odd_notebook_ids_never_500(rig):
    for nb in ("..", "%2f", "x" * 50_000, "a b", "\u0000"):
        r = _raw(rig, "GET", f"/notebooks/{nb}/status", query={"instanceId": "t"})
        assert r.status == 200 and NotebookStatus.model_validate_json(r.body).needsRegister


@pytest.mark.parametrize("body", [b"\xff\xfe\x00bad", b"", b"null", b"[]", b'"s"', b"12", b'{"events": 5}'])
@pytest.mark.parametrize("route", [r for r in ROUTES if r.method == "POST"], ids=lambda r: r.name)
def test_hostile_bodies_are_400_never_an_exception(rig, route, body):
    path = route.path
    r = _raw(rig, "POST", path, body)
    assert r.status == 400, (route.name, r.status, r.body[:200])


@pytest.mark.parametrize("deep", [b"[" * 100_000, b"{" * 100_000, b'{"a":' * 50_000 + b"1" + b"}" * 50_000],
                         ids=["arrays", "open-objects", "objects"])
@pytest.mark.parametrize("path", ["/ingest", "/notebooks/close", "/notebooks/register", "/outputs/prepare",
                                  "/cells/revision"])
def test_deeply_nested_json_is_400_not_a_recursion_error(rig, path, deep):
    assert _raw(rig, "POST", path, deep).status == 400


def test_huge_integer_query_values_are_400(rig):
    r = _raw(rig, "GET", "/state", query={"explorationId": "e", "branchId": "b", "tick": "9" * 6000})
    assert r.status == 400


def test_a_secret_in_a_wrongly_shaped_body_is_not_echoed(rig):
    for body in ({"explorationId": SECRET, "editor": SECRET, "instanceId": 1, "events": SECRET},
                 {"notebookId": SECRET, "instanceId": 5},
                 {"path": SECRET, "editor": SECRET}):
        for path in ("/ingest", "/notebooks/close", "/notebooks/register", "/outputs/prepare", "/cells/revision"):
            r = _raw(rig, "POST", path, json.dumps(body).encode())
            assert SECRET not in r.body.decode(), path


def test_a_secret_in_an_unknown_history_or_query_is_not_echoed(rig):
    for path, q in (("/outline", {"explorationId": SECRET}),
                    ("/state", {"explorationId": SECRET, "branchId": SECRET, "tick": "1"}),
                    ("/search", {"explorationId": SECRET, "query": SECRET, "kinds": SECRET}),
                    ("/events-since", {"explorationId": SECRET, "branchId": SECRET, "afterTick": SECRET})):
        r = _raw(rig, "GET", path, query=q)
        assert SECRET not in r.body.decode(), path
    assert SECRET not in _raw(rig, "PUT", f"/blobs/{SECRET}", b"x").body.decode()
    assert SECRET not in _raw(rig, "GET", f"/nope/{SECRET}").body.decode()


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "HEAD", "OPTIONS", "post", "BREW"])
def test_other_verbs_are_405_or_handled_and_never_500(rig, method):
    r = _raw(rig, method, "/ingest", b"{}")
    assert r.status in (400, 405)
    assert _raw(rig, method, "/info").status in (200, 404, 405)


def test_a_credential_cannot_be_forged_from_a_string_bytes_or_another_authorizer(rig):
    other = JupyterAuthorizer().mint()
    for cred in (None, "", "Credential", b"x" * 32, object(), Credential(b"x" * 32), other, {"_key": b""}):
        for route in ROUTES:
            r = _raw(rig, route.method, route.path.format(explorationId="e", notebookId="n", contentHash="h"),
                     b"{}", {"instanceId": "t"}, cred=cred)
            assert r.status in (401, 404, 405) and r.status != 200, (route.name, cred)
    assert "key" not in repr(Credential(b"secretkey")).lower().replace("credential", "")


def test_the_server_instance_id_cannot_ingest_through_the_api(rig):
    first = rig.register("tab-1")
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    r = rig.call("POST", "/ingest", body=rig.ingest_body(first.explorationId, s.events, "blaym-server"))
    assert IngestResponse.model_validate_json(r.body).outcomes[0].root.status == "refused"


def test_events_for_another_history_cannot_ride_in_an_envelope_the_caller_holds(rig):
    mine = rig.register("tab-1", "a.ipynb")
    theirs = rig.register("tab-9", "b.ipynb", fingerprint={"nbformat": "4.5", "sourceHash": "other", "cellIds": ["z9"]})
    assert theirs.explorationId != mine.explorationId
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    body = rig.ingest_body(mine.explorationId, s.events, "tab-1")
    for ev in body["events"]:
        ev["explorationId"] = theirs.explorationId
    r = rig.call("POST", "/ingest", body=body)
    out = IngestResponse.model_validate_json(r.body)
    assert all(o.root.status != "accepted" for o in out.outcomes)
    assert rig.call("GET", "/outline", query={"explorationId": theirs.explorationId}).status in (404, 200)
    outline = rig.call("GET", "/outline", query={"explorationId": theirs.explorationId})
    assert outline.status == 404 or json.loads(outline.body).get("highestTick", 0) == 0


def test_close_by_an_inactive_instance_does_not_take_the_notebook_from_the_active_one(rig):
    first = rig.register("tab-1")
    rig.register("tab-2")
    assert rig.call("POST", "/notebooks/close", body={"notebookId": first.notebookId, "instanceId": "tab-2"}).status == 204
    again = rig.call("GET", f"/notebooks/{first.notebookId}/status", query={"instanceId": "tab-1"})
    assert NotebookStatus.model_validate_json(again.body).active


def test_a_refused_ingest_for_a_second_instance_does_not_reach_the_lease_or_store(rig):
    first = rig.register("tab-1")
    rig.register("tab-2")
    s = Scenario()
    s.start(cells=[("c1", "x = 1")])
    rig.call("POST", "/ingest", body=rig.ingest_body(first.explorationId, s.events, "tab-2"))
    assert rig.call("GET", "/outline", query={"explorationId": first.explorationId}).status == 404

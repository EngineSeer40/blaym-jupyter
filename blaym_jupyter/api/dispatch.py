"""`handle`: one request in, one answer out (S5, decision G). No web framework, no logging of bodies."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from blaym_jupyter_types import (
    EventsSinceRequest,
    GetBlobRequest,
    IngestRefused,
    IngestRequest,
    IngestResponse,
    NotebookRegistration,
    OutlineRequest,
    PrepareOutputsRequest,
    PrepareOutputsResponse,
    ResolveCellRevisionRequest,
    SearchRequest,
    StateAtRequest,
)

from blaym_jupyter.ports.blobs import BlobCorrupt, BlobNotFound
from blaym_jupyter.recording.outputs import OutputLimits, item_from_output
from blaym_jupyter.recording.registry import Registry
from blaym_jupyter.service.auth import Unauthorized
from blaym_jupyter.service.core import Service, ServiceBusy, UnknownHistory

from . import codec
from .errors import ApiError, BadRequest
from .routes import match

JSON = "application/json"
CHUNK = 1 << 20
RETRY_AFTER_S = "1"


@dataclass(frozen=True)
class ApiRequest:
    """`path` is relative to the API base (`/ingest`); `query` and `headers` are plain dicts
    (header names lower case); `body` is bytes, or chunks for a streamed upload."""

    method: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | Iterable[bytes] = b""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Recording:
    """What the dispatcher serves: the service and the notebook registry (and the output limits
    `prepare_outputs` builds items with)."""

    service: Service
    registry: Registry
    output_limits: OutputLimits = field(default_factory=OutputLimits)


def _json(status: int, value: Any, headers: Mapping[str, str] | None = None) -> ApiResponse:
    return ApiResponse(status, codec.dump(value), {"Content-Type": JSON, **(headers or {})})


def _error(exc: ApiError) -> ApiResponse:
    return _json(exc.status, {"error": exc.message, **exc.extra}, exc.headers)


MAX_JSON_BODY_BYTES = 64 * 1024 * 1024  # every route but blob upload


def handle(request: ApiRequest, credential: Any, recording: Recording) -> ApiResponse:
    """Never raises for a bad request, a bad credential, an unknown history or a busy store."""
    try:
        route, params, allowed = match(request.method.upper(), request.path)
        if route is None:
            if allowed:
                raise ApiError(405, "method not allowed", {"Allow": ", ".join(allowed)})
            raise ApiError(404, "no such route")
        if route.name != "put_blob" and isinstance(request.body, (bytes, bytearray)) \
                and len(request.body) > MAX_JSON_BODY_BYTES:
            raise ApiError(413, "the request body is too large")
        recording.service.info(credential)  # the sign-in check, before any route (registry routes never reach the service)
        return _HANDLERS[route.name](request, params, credential, recording)
    except ApiError as exc:
        return _error(exc)
    except Unauthorized:
        return _json(401, {"error": "not signed in"})
    except UnknownHistory:
        return _json(404, {"error": "unknown history or branch"})
    except BlobNotFound:
        return _json(404, {"error": "not found"})
    except BlobCorrupt:
        return _json(500, {"error": "the stored blob is damaged"})
    except ServiceBusy as busy:
        outcomes = [o.model_dump(mode="json", exclude_none=True) for o in (busy.outcomes or [])]
        return _json(503, {"error": "the store is busy", "outcomes": outcomes}, {"Retry-After": RETRY_AFTER_S})


# -- routes ----------------------------------------------------------------------------------


def _ingest(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    body = codec.parse(IngestRequest, req.body)
    if rec.registry.gate_ingest(body.instanceId, body.explorationId) != "ok":
        detail = "another recorder instance is active for this notebook"
        refused = [IngestRefused(status="refused", eventId=str(ev.root.eventId), reason="not_active_recorder",
                                 detail=detail) for ev in body.events]
        return _json(200, IngestResponse(apiVersion=1, outcomes=refused))
    return _json(200, rec.service.ingest(cred, body))


def _put_blob(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    data = req.body
    if isinstance(data, (bytes, bytearray)):
        view = memoryview(bytes(data))
        data = (bytes(view[i:i + CHUNK]) for i in range(0, len(view), CHUNK))
    try:
        return _json(200, rec.service.put_blob(cred, params["explorationId"], data))
    except ValueError:
        raise BadRequest("explorationId: not a valid history id") from None


def _prepare_outputs(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    body = codec.parse(PrepareOutputsRequest, req.body)
    items = []
    for index, output in enumerate(body.outputs):
        try:
            prepared = item_from_output(output, rec.output_limits)
        except ValueError:
            raise BadRequest(f"outputs.{index}: not a notebook output") from None
        item = prepared.item
        if prepared.blob is not None:
            stored = rec.service.put_blob(cred, body.explorationId, prepared.blob)
            item = item.model_copy(update={"contentHash": stored.contentHash})
        items.append(item)
    return _json(200, PrepareOutputsResponse(apiVersion=1, items=items))


def _resolve(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    body = codec.parse(ResolveCellRevisionRequest, req.body)
    return _json(200, rec.service.resolve_cell_revision(cred, body))


def _register(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    return _json(200, rec.registry.register(codec.parse(NotebookRegistration, req.body)))


def _status(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    return _json(200, rec.registry.status(params["notebookId"], codec.str_param(req.query, "instanceId")))


def _close(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    try:
        data = json.loads(codec.body_bytes(req.body))
    except (ValueError, RecursionError):
        raise BadRequest("the body is not valid JSON") from None
    for key in ("notebookId", "instanceId"):
        if not isinstance(data, dict) or not isinstance(data.get(key), str) or not data[key]:
            raise BadRequest(f"{key}: required string")
    rec.registry.close(data["notebookId"], data["instanceId"])
    return ApiResponse(204)


def _info(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    return _json(200, rec.service.info(cred))


def _state(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    request = codec.from_mapping(StateAtRequest, {
        "explorationId": codec.str_param(req.query, "explorationId"),
        "branchId": codec.str_param(req.query, "branchId"),
        "tick": codec.int_param(req.query, "tick")})
    return _json(200, rec.service.state_at(cred, request))


def _outline(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    request = codec.from_mapping(OutlineRequest, {"explorationId": codec.str_param(req.query, "explorationId")})
    outline = rec.service.outline(cred, request)
    if outline is None:
        raise UnknownHistory("this history does not exist")
    return _json(200, outline)


def _events_since(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    request = codec.from_mapping(EventsSinceRequest, {
        "explorationId": codec.str_param(req.query, "explorationId"),
        "branchId": codec.str_param(req.query, "branchId"),
        "afterTick": codec.int_param(req.query, "afterTick")})
    return _json(200, rec.service.events_since(cred, request))


def _search(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    data: dict[str, Any] = {"explorationId": codec.str_param(req.query, "explorationId"),
                            "query": codec.str_param(req.query, "query")}
    if req.query.get("kinds"):
        data["kinds"] = req.query["kinds"].split(",")
    if req.query.get("limit"):
        data["limit"] = codec.int_param(req.query, "limit")
    return _json(200, rec.service.search(cred, codec.from_mapping(SearchRequest, data)))


def _get_blob(req: ApiRequest, params, cred, rec: Recording) -> ApiResponse:
    request = codec.from_mapping(GetBlobRequest, params)
    try:
        data = rec.service.get_blob(cred, request)
    except ValueError:
        raise BadRequest("explorationId or contentHash: not valid") from None
    return ApiResponse(200, data, {"Content-Type": "application/octet-stream"})


_HANDLERS: dict[str, Callable[..., ApiResponse]] = {
    "ingest": _ingest, "put_blob": _put_blob, "prepare_outputs": _prepare_outputs,
    "resolve_cell_revision": _resolve, "register": _register, "status": _status, "close": _close,
    "info": _info, "state": _state, "outline": _outline, "events_since": _events_since,
    "search": _search, "get_blob": _get_blob,
}

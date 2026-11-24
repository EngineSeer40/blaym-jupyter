"""The route table (S5, section 3). One place: the dispatcher reads it, a test writes it to
`fixtures/api/routes.json`, and the TypeScript side checks that file, so the two cannot drift.

Paths are relative to `<base_url>/blaym-jupyter/api` and use `{name}` for one path segment.
`request` and `response` name the contract types (`query` means query parameters,
`bytes` means a raw body, `none` an empty one).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BASE_PATH = "blaym-jupyter/api"


@dataclass(frozen=True)
class Route:
    name: str
    method: str
    path: str
    request: str
    response: str

    def regex(self) -> re.Pattern[str]:
        return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path) + "$")


ROUTES: tuple[Route, ...] = (
    Route("ingest", "POST", "/ingest", "IngestRequest", "IngestResponse"),
    Route("put_blob", "PUT", "/blobs/{explorationId}", "bytes", "PutBlobResponse"),
    Route("prepare_outputs", "POST", "/outputs/prepare", "PrepareOutputsRequest", "PrepareOutputsResponse"),
    Route("resolve_cell_revision", "POST", "/cells/revision", "ResolveCellRevisionRequest",
          "ResolveCellRevisionResponse"),
    Route("register", "POST", "/notebooks/register", "NotebookRegistration", "NotebookStatus"),
    Route("status", "GET", "/notebooks/{notebookId}/status", "query:instanceId", "NotebookStatus"),
    Route("close", "POST", "/notebooks/close", "json:notebookId,instanceId", "none"),
    Route("info", "GET", "/info", "none", "ServiceInfo"),
    Route("state", "GET", "/state", "query:explorationId,branchId,tick", "RebuildResult"),
    Route("outline", "GET", "/outline", "query:explorationId", "HistoryOutline"),
    Route("events_since", "GET", "/events-since", "query:explorationId,branchId,afterTick", "EventsSinceResponse"),
    Route("search", "GET", "/search", "query:explorationId,query,kinds?,limit?", "SearchResponse"),
    Route("get_blob", "GET", "/blobs/{explorationId}/{contentHash}", "none", "bytes"),
)


def match(method: str, path: str) -> tuple[Route | None, dict[str, str], list[str]]:
    """`(route, path parameters, allowed methods)`: no route and no methods is a 404; no route
    with methods is a 405."""
    allowed: list[str] = []
    for route in ROUTES:
        found = route.regex().match(path)
        if found is None:
            continue
        if route.method == method:
            return route, found.groupdict(), []
        allowed.append(route.method)
    return None, {}, allowed


def as_json() -> dict:
    return {"base": BASE_PATH, "routes": [
        {"name": r.name, "method": r.method, "path": r.path, "request": r.request, "response": r.response}
        for r in ROUTES]}

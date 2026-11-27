"""HTTP for the standalone host: `http.server` in front of `api.dispatch.handle`.

Every request needs a `Host` of `127.0.0.1:<port>` or `localhost:<port>` (403 otherwise) and
`Authorization: Bearer <token>` (401 otherwise). No CORS headers. Nothing about a request body or
the token is logged. Paths are the API's own (`/ingest`, `/state`, ...), with no base prefix.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit

from blaym_notebook.api import ApiRequest, Recording, handle
from blaym_notebook.api.dispatch import MAX_JSON_BODY_BYTES
from blaym_notebook.api.stream import AdvancedStream
from blaym_notebook.service.auth import Unauthorized

CHUNK = 1 << 20
_REFUSALS = {401: b'{"error": "not signed in"}', 403: b'{"error": "bad host"}', 413: b'{"error": "the request body is too large"}'}


def make_server(recording: Recording, authorizer: Any, port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.recording, server.authorizer = recording, authorizer  # type: ignore[attr-defined]
    return server


class _Handler(BaseHTTPRequestHandler):
    server_version = "BlaymVscode/0"
    server: Any

    def log_message(self, fmt: str, *args: Any) -> None:  # no request log
        pass

    def _send(self, status: int, body: bytes = b"", headers: Any = None) -> None:
        self.send_response(status)
        headers = headers or {"Content-Type": "application/json"}
        for name, value in headers.items():
            self.send_header(name, value)
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0" if status == 204 else str(len(body)))
        self.end_headers()
        if status != 204:
            self.wfile.write(body)

    def _chunks(self, length: int) -> Iterator[bytes]:
        while length > 0:
            data = self.rfile.read(min(CHUNK, length))
            if not data:
                return
            length -= len(data)
            yield data

    def _serve(self) -> None:
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self._send(403, _REFUSALS[403])
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not self.server.authorizer.check(auth[7:]):
            return self._send(401, _REFUSALS[401])
        url = urlsplit(self.path)
        if self.command == "GET" and url.path == "/stream":
            return self._stream(auth[7:], dict(parse_qsl(url.query)))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._send(400, b'{"error": "bad content length"}')
        streamed = self.command == "PUT"
        if not streamed and length > MAX_JSON_BODY_BYTES:
            return self._send(413, _REFUSALS[413])
        body: Any = self._chunks(length) if streamed else self.rfile.read(length)
        request = ApiRequest(self.command, url.path, dict(parse_qsl(url.query)),
                             {k.lower(): v for k, v in self.headers.items()}, body)
        try:
            response = handle(request, auth[7:], self.server.recording)
        except Exception:  # noqa: BLE001  # always answer; say nothing about the request
            return self._send(500, b'{"error": "internal error"}')
        self._send(response.status, response.body, dict(response.headers))

    def _stream(self, credential: str, query: dict[str, str]) -> None:
        """Frames until the client goes (a failed write), then the iterator is closed."""
        try:
            after = int(query["afterTick"]) if query.get("afterTick") else None
        except ValueError:
            return self._send(400, b'{"error": "afterTick: not an integer"}')
        if not query.get("explorationId"):
            return self._send(400, b'{"error": "explorationId: required"}')
        try:
            stream = AdvancedStream(self.server.recording.service, credential, query["explorationId"], after)
        except Unauthorized:
            return self._send(401, _REFUSALS[401])
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.flush()
            for frame in stream:
                self.wfile.write(frame)
                self.wfile.flush()
        except OSError:  # the client went
            pass
        finally:
            stream.close()
            self.close_connection = True

    do_GET = do_POST = do_PUT = _serve

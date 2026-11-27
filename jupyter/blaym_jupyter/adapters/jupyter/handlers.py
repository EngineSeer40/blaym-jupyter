"""One Tornado handler for every Blaym API route.

All logic lives in `api.handle`; this turns a Tornado request into an `ApiRequest`, runs the
dispatcher in the thread pool and writes the answer. `@authenticated` guards every verb, and
Jupyter's own XSRF check runs before the verb as for any other API handler (token-signed-in
requests skip it, as Jupyter does).
"""

from __future__ import annotations

import asyncio

import tornado.web
from jupyter_server.base.handlers import APIHandler
from tornado.ioloop import IOLoop

from tornado.iostream import StreamClosedError

from blaym_notebook.api import ApiRequest, handle
from blaym_notebook.api.stream import KEEPALIVE_S, KEEP_ALIVE, MAX_PENDING, advanced_frame, catch_up_frame, offer
from blaym_notebook.service.auth import Unauthorized


class BlaymApiHandler(APIHandler):
    def initialize(self, host) -> None:
        self.host = host

    async def _serve(self, path: str) -> None:
        request = ApiRequest(
            method=self.request.method,
            path="/" + path,
            query={k: v[-1].decode("utf-8", "replace") for k, v in self.request.query_arguments.items()},
            headers={k.lower(): v for k, v in self.request.headers.items()},
            body=self.request.body,
        )
        response = await IOLoop.current().run_in_executor(
            None, handle, request, self.host.credential(), self.host.recording)
        self.set_status(response.status)
        for name, value in response.headers.items():
            if name.lower() != "content-type":
                self.set_header(name, value)
        if response.status == 204:
            self.finish()
        else:
            self.finish(response.body, set_content_type=response.headers.get("Content-Type", "application/json"))

    @tornado.web.authenticated
    async def get(self, path: str) -> None:
        await self._serve(path)

    @tornado.web.authenticated
    async def post(self, path: str) -> None:
        await self._serve(path)

    @tornado.web.authenticated
    async def put(self, path: str) -> None:
        await self._serve(path)


class BlaymStreamHandler(APIHandler):
    """`GET <base>/blaym-jupyter/api/stream`: the frames of `api.stream`, flushed one by one.
    No thread waits for a stream: the service callback hands each advance to the IOLoop."""

    def initialize(self, host) -> None:
        self.host = host
        self._unsubscribe = None

    def _stop(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def on_connection_close(self) -> None:
        self._stop()

    @tornado.web.authenticated
    async def get(self) -> None:
        query = {k: v[-1].decode("utf-8", "replace") for k, v in self.request.query_arguments.items()}
        try:
            after = int(query["afterTick"]) if query.get("afterTick") else None
        except ValueError:
            raise tornado.web.HTTPError(400, "afterTick: not an integer")
        if not query.get("explorationId"):
            raise tornado.web.HTTPError(400, "explorationId: required")
        service, credential, loop = self.host.recording.service, self.host.credential(), IOLoop.current()
        advances: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING)
        try:
            service.info(credential)  # the sign-in check
            self._unsubscribe = service.subscribe(
                query["explorationId"], lambda advance: loop.add_callback(offer, advances, advance))
            catch_up = await loop.run_in_executor(
                None, catch_up_frame, service, credential, query["explorationId"], after)
        except Unauthorized:
            self._stop()
            raise tornado.web.HTTPError(401, "not signed in")
        except BaseException:  # the connection may stay open, so on_connection_close would not run
            self._stop()
            raise
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("X-Accel-Buffering", "no")
        try:
            self.write(catch_up if catch_up is not None else KEEP_ALIVE)  # opens the response at once
            await self.flush()
            while self._unsubscribe is not None:
                try:
                    frame = advanced_frame(await asyncio.wait_for(advances.get(), KEEPALIVE_S))
                except asyncio.TimeoutError:
                    frame = KEEP_ALIVE
                self.write(frame)
                await self.flush()
        except StreamClosedError:
            pass
        finally:
            self._stop()

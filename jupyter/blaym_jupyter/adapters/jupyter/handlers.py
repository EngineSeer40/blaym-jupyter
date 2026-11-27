"""One Tornado handler for every Blaym API route.

All logic lives in `api.handle`; this turns a Tornado request into an `ApiRequest`, runs the
dispatcher in the thread pool and writes the answer. `@authenticated` guards every verb, and
Jupyter's own XSRF check runs before the verb as for any other API handler (token-signed-in
requests skip it, as Jupyter does).
"""

from __future__ import annotations

import tornado.web
from jupyter_server.base.handlers import APIHandler
from tornado.ioloop import IOLoop

from blaym_notebook.api import ApiRequest, handle


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

"""Sign-in for the Jupyter host.

The service checks a credential through the `Authorizer` port and never sees Jupyter. Here the
credential is an object that only `JupyterAuthorizer.mint()` makes, and `mint()` is called in two
places: the handler after `@tornado.web.authenticated` has let the request through, and the
in-process recorders when the host is built. A request that did not pass Jupyter's own sign-in
cannot reach a credential, and a string or any other object never passes `check`.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Any


class Credential:
    """Proof that Jupyter's sign-in (or this process) vouched for the caller."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes):
        self._key = key

    def matches(self, key: bytes) -> bool:
        return hmac.compare_digest(self._key, key)

    def __repr__(self) -> str:  # never show the key
        return "<Credential>"


class JupyterAuthorizer:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)  # new for every start; never leaves this object

    def mint(self) -> Credential:
        return Credential(self._key)

    def check(self, credential: Any) -> bool:
        return isinstance(credential, Credential) and credential.matches(self._key)

"""Sign-in for the standalone host: one token the extension hands over on standard input.

The service checks a credential through the `Authorizer` port. Here the credential is the bearer
token itself: the server passes what the request carried, the in-process parts pass the host's own.
"""

from __future__ import annotations

import hmac
from typing import Any

MIN_TOKEN_BYTES = 32


class TokenAuthorizer:
    def __init__(self, token: str):
        data = token.encode("utf-8")
        if len(data) < MIN_TOKEN_BYTES:
            raise ValueError(f"the token must be at least {MIN_TOKEN_BYTES} bytes")
        self._token = data

    def check(self, credential: Any) -> bool:
        return isinstance(credential, str) and hmac.compare_digest(credential.encode("utf-8"), self._token)

    def credential(self) -> str:
        """The credential of this process's own parts."""
        return self._token.decode("utf-8")

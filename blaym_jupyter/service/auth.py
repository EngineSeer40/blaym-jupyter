"""Sign-in check implementations (S4, decision I)."""

from __future__ import annotations

import hmac
from typing import Any


class Unauthorized(Exception):
    """The credential failed the sign-in check. Nothing was read or written."""


class FixedTokenAuthorizer:
    """One shared token, compared in constant time. Jupyter's own sign-in is the S5 host's."""

    def __init__(self, token: str):
        if not token:
            raise ValueError("the token cannot be empty")
        self._token = token.encode("utf-8")

    def check(self, credential: Any) -> bool:
        return isinstance(credential, str) and hmac.compare_digest(credential.encode("utf-8"), self._token)

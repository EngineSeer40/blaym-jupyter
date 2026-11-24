"""The sign-in check port (S4, decision I). Every service call goes through it."""

from __future__ import annotations

from typing import Any, Protocol


class Authorizer(Protocol):
    def check(self, credential: Any) -> bool:
        """True when the credential may use the service."""
        ...

"""Errors the dispatcher turns into HTTP answers."""

from __future__ import annotations


class ApiError(Exception):
    """An answer that is not a success: a status, a short message and optional headers.
    The message never contains request content (it may hold secrets)."""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None,
                 extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}
        self.extra = extra or {}


class BadRequest(ApiError):
    def __init__(self, message: str):
        super().__init__(400, message)

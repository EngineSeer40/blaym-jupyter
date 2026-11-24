"""JSON in and out of the generated contract models."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import BadRequest

M = TypeVar("M", bound=BaseModel)


def first_error(exc: ValidationError) -> str:
    """The first validation problem as `where: message`, never the offending value."""
    err = exc.errors(include_input=False, include_url=False)[0]
    where = ".".join(str(p) for p in err.get("loc", ())) or "body"
    return f"{where}: {err.get('msg', 'invalid')}"


def body_bytes(body: bytes | Iterable[bytes]) -> bytes:
    return body if isinstance(body, (bytes, bytearray)) else b"".join(body)


def parse(model: type[M], body: bytes | Iterable[bytes]) -> M:
    """A request body as `model`; `BadRequest` with the first error otherwise."""
    try:
        data = json.loads(body_bytes(body))
    except (ValueError, RecursionError):  # deeply nested JSON raises RecursionError
        raise BadRequest("the body is not valid JSON") from None
    return from_mapping(model, data)


def from_mapping(model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise BadRequest(first_error(exc)) from None


def dump(value: Any) -> bytes:
    """A model, or a plain JSON value, as compact JSON bytes."""
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True).encode("utf-8")
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def int_param(query: Mapping[str, str], name: str) -> int:
    raw = query.get(name)
    if raw is None:
        raise BadRequest(f"{name}: required query parameter")
    try:
        return int(raw)
    except ValueError:
        raise BadRequest(f"{name}: must be an integer") from None


def str_param(query: Mapping[str, str], name: str) -> str:
    raw = query.get(name)
    if not raw:
        raise BadRequest(f"{name}: required query parameter")
    return raw

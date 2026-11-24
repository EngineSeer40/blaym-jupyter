"""Secret hiding: a pure module (standard library and the contracts only)."""

from .redact import MARKER_RE, redact, redact_event

__all__ = ["MARKER_RE", "redact", "redact_event"]

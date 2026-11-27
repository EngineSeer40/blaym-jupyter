"""The blob store port.

Large content (outputs, charts, big messages) lives beside the history, named by the hash
of what is stored. Content enters only through `put`. The store applies a redaction
function it is given without knowing what the function does (the secret rules live
elsewhere), then the size limit, then names the content by the hash of the STORED bytes.

Order inside `put`: redact (text only) -> size limit -> hash -> compress and write.

Interpretations (all pinned by the shared suite in `tests/ports/test_blob_store_contract.py`):

- `contentHash` is `sha256:` + 64 hex digits of the stored (redacted, truncated) bytes. The
  file name uses the hex part only.
- `originalBytes` is the size of the input as given. `storedBytes` is the size of the stored
  content before compression. `truncated` is true when the input was larger than
  `max_stored_bytes` or the redacted text was; the stored content is then at most
  `truncated_preview_bytes` long (cut at a character boundary for text).
- "Text" means the head of the content decodes as UTF-8. Anything else is binary and is never
  passed to `redact`. Redaction sees at most the first `max(max_stored_bytes,
  truncated_preview_bytes)` bytes, so memory stays bounded for any input size.
- Storing identical stored bytes twice is one blob. The second result reports its own
  `originalBytes` and `truncated`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from blaym_jupyter_types import PutBlobResponse, StoreLimits

HASH_PREFIX = "sha256:"
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
MAX_ID_LENGTH = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class BlobError(Exception):
    """Base of blob store errors."""


class BlobNotFound(BlobError, LookupError):
    """No blob with this hash is stored for this exploration."""


class BlobCorrupt(BlobError):
    """A stored blob is missing its file, unreadable, or does not match its hash."""


@dataclass(frozen=True)
class BlobLimits:
    """Mirrors `StoreLimits` defaults."""

    inline_bytes: int = 16384
    max_stored_bytes: int = 10485760
    truncated_preview_bytes: int = 262144
    gzip_level: int = 6

    def __post_init__(self) -> None:
        if min(self.inline_bytes, self.max_stored_bytes, self.truncated_preview_bytes) < 0:
            raise ValueError("limits cannot be negative")
        if not 1 <= self.gzip_level <= 9:
            raise ValueError("gzip_level must be 1 to 9")

    @classmethod
    def from_contract(cls, limits: StoreLimits | None = None) -> "BlobLimits":
        """The one place the contract's optional limits meet the defaults above."""
        base = cls()
        if limits is None:
            return base
        pick = lambda value, default: default if value is None else value  # noqa: E731
        return cls(
            inline_bytes=pick(limits.inlineOutputBytes, base.inline_bytes),
            max_stored_bytes=pick(limits.maxStoredOutputBytes, base.max_stored_bytes),
            truncated_preview_bytes=pick(limits.truncatedPreviewBytes, base.truncated_preview_bytes),
            gzip_level=pick(limits.blobGzipLevel, base.gzip_level),
        )


class BlobStore(Protocol):
    def put(
        self,
        exploration_id: str,
        data: bytes | Iterable[bytes],
        limits: BlobLimits = BlobLimits(),
        redact: Callable[[str], str] | None = None,
    ) -> PutBlobResponse:
        """Store content; return its hash and sizes. Idempotent. `data` may be a stream of
        chunks of any total size: it is read once, in chunks, with bounded memory."""
        ...

    def read(self, exploration_id: str, content_hash: str) -> bytes:
        """The stored bytes. `BlobNotFound` for an unknown hash; `BlobCorrupt` for a missing
        or damaged file. Never returns bytes that do not match the hash."""
        ...

    def exists(self, exploration_id: str, content_hash: str) -> bool: ...

    def cleanup_staging(self) -> int:
        """Housekeeping: remove every leftover staging folder under the store (debris of a
        write that did not finish) and return how many were removed. Stored blobs are never touched."""
        ...

    def orphans(self, exploration_id: str) -> list[str]:
        """Content hashes that have a file but no record. Reported, never deleted. (A record
        with no file is reported by `exists`.)"""
        ...

    def hashes(self, exploration_id: str) -> list[str]:
        """Content hashes of every blob record of the exploration (sorted)."""
        ...

    def close(self) -> None: ...


def check_exploration_id(exploration_id: str) -> str:
    """Ids become folder names: opaque ids only, never a path."""
    if isinstance(exploration_id, str) and len(exploration_id) > MAX_ID_LENGTH:
        raise ValueError(f"exploration id is longer than {MAX_ID_LENGTH} characters")
    if not isinstance(exploration_id, str) or not _ID_RE.fullmatch(exploration_id) or exploration_id.endswith("."):
        raise ValueError(f"not a valid exploration id for storage: {exploration_id!r}")
    return exploration_id


def hash_hex(content_hash: str) -> str | None:
    """The hex part of a well-formed content hash, else None."""
    return content_hash[len(HASH_PREFIX):] if isinstance(content_hash, str) and _HASH_RE.fullmatch(content_hash) else None


def content_hash_of(stored: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(stored).hexdigest()


@dataclass(frozen=True)
class Prepared:
    stored: bytes
    original_bytes: int
    truncated: bool


def _cut_at_char(data: bytes, size: int) -> bytes:
    return data[:size].decode("utf-8", "ignore").encode("utf-8")


def prepare_content(
    data: bytes | Iterable[bytes],
    limits: BlobLimits,
    redact: Callable[[str], str] | None,
) -> Prepared:
    """Apply redaction and the size limit. Holds at most the window (about
    `max_stored_bytes`) plus one chunk; the rest of a long stream is counted and dropped."""
    window = max(limits.max_stored_bytes, limits.truncated_preview_bytes)
    chunks = [data] if isinstance(data, (bytes, bytearray, memoryview)) else data
    head = bytearray()
    total = 0
    for chunk in chunks:
        total += len(chunk)
        room = window - len(head)
        if room > 0:
            head += chunk[:room]
    # a window cut can split a character: text is judged on the complete characters
    text = None
    try:
        text = bytes(head).decode("utf-8")
    except UnicodeDecodeError as exc:
        if total > len(head) and exc.reason == "unexpected end of data":
            text = bytes(head[: exc.start]).decode("utf-8")
    is_text = text is not None
    if is_text:
        if redact is not None:
            text = redact(text)
        stored = text.encode("utf-8")
    else:
        stored = bytes(head)
    del text, head
    truncated = total > limits.max_stored_bytes or len(stored) > limits.max_stored_bytes
    if truncated:
        size = limits.truncated_preview_bytes
        stored = _cut_at_char(stored, size) if is_text else stored[:size]
    return Prepared(stored, total, truncated)

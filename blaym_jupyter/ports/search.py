"""The search index port (S4, Task 8; decision J).

The index only stores and queries documents `(exploration_id, branch_id, tick, kind, text)`.
It knows nothing about events: which text is worth indexing is the policy in
`service/indexing.py`. The index is derived data: it can be dropped and rebuilt from the
stored events without loss.

Interpretations (pinned by `tests/ports/test_search_contract.py`):

- The query is plain text, never search syntax: quotes, brackets, `AND`, `OR`, `NEAR`, `*` and
  `:` are ordinary characters and can neither raise an error nor change the query.
- Matching is case-insensitive. Kind `code` matches any substring of at least 3 characters
  (a shorter query is a bounded scan). Every other kind matches WORDS (runs of letters and
  digits; punctuation and `_` separate words): the query's words must appear consecutively
  and in order, each as a PREFIX of a word. So `resid`, `residual plot` and `resid plo` find
  "Residuals plot", while a mid-word fragment like `sidual` does not. A query with no word
  matches nothing in those kinds.
- Hits are ordered by tick, newest first, then by relevance; only the given exploration's
  documents are ever returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from blaym_jupyter_types import SearchHit

SNIPPET_CHARS = 120


@dataclass(frozen=True)
class SearchDocument:
    exploration_id: str
    branch_id: str
    tick: int
    kind: str  # a SearchHitKind value
    text: str


class SearchIndex(Protocol):
    def add(self, documents: Iterable[SearchDocument]) -> None: ...

    def remove_exploration(self, exploration_id: str) -> None:
        """Drop every document of one exploration (the first step of a rebuild)."""
        ...

    def query(self, exploration_id: str, text: str, kinds: Iterable[str] | None = None,
              limit: int = 20) -> list[SearchHit]: ...

    def close(self) -> None: ...


WORD_RE = re.compile(r"[^\W_]+")


def plain_snippet(text: str, start: int, length: int, width: int = SNIPPET_CHARS) -> str:
    """A slice of `text` of about `width` characters around the match."""
    lo = max(0, start - (width - length) // 2)
    piece = text[lo:lo + max(width, length)]
    return ("…" if lo > 0 else "") + piece + ("…" if lo + len(piece) < len(text) else "")

"""SQLite FTS5 search index in `<root>/search.sqlite3`.

A separate file from the history database: it is derived data, and deleting it loses
nothing that `Service.rebuild_search` cannot restore. Two FTS5 tables: `code_fts` uses the
`trigram` tokenizer (partial matches inside identifiers, like `resid` in `residuals`);
`text_fts` uses `unicode61` for chat and output text (whole words, the last one matched as
a prefix). Imports the ports, the contracts, `store.db` and the standard library.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from blaym_jupyter_types import SearchHit

from blaym_jupyter.ports.search import WORD_RE, SearchDocument, plain_snippet

from .db import immediate, translate_busy

INDEX_NAME = "search.sqlite3"
_COLUMNS = "text, exploration_id UNINDEXED, branch_id UNINDEXED, tick UNINDEXED, kind UNINDEXED"
_SCAN_LIMIT = 5000  # most rows a short code query looks at


def _phrase(text: str) -> str:
    """The user's text as one FTS5 phrase (trigram table): nothing typed can be read as syntax."""
    return '"' + text.replace('"', '""') + '"'


def _words_prefix(text: str) -> str | None:
    """Words of the user's text as consecutive prefix terms (`"resid"* + "plo"*`). Only
    letters and digits survive, so nothing typed can be read as syntax. None: no words."""
    words = WORD_RE.findall(text)
    return " + ".join(f'"{w}"*' for w in words) if words else None


class SqliteSearchIndex:
    def __init__(self, root: Path | str, busy_timeout: float = 5.0):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(root / INDEX_NAME, isolation_level=None, check_same_thread=False,
                                     timeout=busy_timeout)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")  # derived data: a lost tail is rebuilt
        self._conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5({_COLUMNS}, tokenize='trigram')")
        self._conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5({_COLUMNS}, tokenize='unicode61')")

    @translate_busy
    def add(self, documents: Iterable[SearchDocument]) -> None:
        docs = list(documents)
        if not docs:
            return
        with immediate(self._conn) as conn:
            for d in docs:
                table = "code_fts" if d.kind == "code" else "text_fts"
                conn.execute(
                    f"INSERT INTO {table}(text, exploration_id, branch_id, tick, kind) VALUES (?,?,?,?,?)",
                    (d.text, d.exploration_id, d.branch_id, int(d.tick), d.kind))

    @translate_busy
    def remove_exploration(self, exploration_id: str) -> None:
        with immediate(self._conn) as conn:
            for table in ("code_fts", "text_fts"):
                conn.execute(f"DELETE FROM {table} WHERE exploration_id = ?", (exploration_id,))

    @translate_busy
    def query(self, exploration_id: str, text: str, kinds: Iterable[str] | None = None,
              limit: int = 20) -> list[SearchHit]:
        needle = text.strip()
        wanted = None if kinds is None else {getattr(k, "value", k) for k in kinds}
        if not needle or limit < 1:
            return []
        rows: list[tuple] = []  # (tick, rank, branch_id, kind, snippet)
        for table in ("code_fts", "text_fts"):
            kind_filter = ""
            args: list = [exploration_id]
            if wanted is not None:
                if table == "code_fts":
                    if "code" not in wanted:
                        continue
                else:
                    others = sorted(wanted - {"code"})
                    if not others:
                        continue
                    kind_filter = f" AND kind IN ({','.join('?' * len(others))})"
                    args += others
            if table == "code_fts" and len(needle) < 3:
                like = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                found = self._conn.execute(
                    "SELECT CAST(tick AS INTEGER), 0, branch_id, kind, text FROM code_fts "
                    "WHERE exploration_id = ? AND text LIKE ? ESCAPE '\\' "
                    "ORDER BY CAST(tick AS INTEGER) DESC, rowid LIMIT ?",
                    (exploration_id, like, min(limit, _SCAN_LIMIT))).fetchall()
                for tick, rank, branch, kind, body in found:
                    at = body.lower().find(needle.lower())
                    rows.append((tick, rank, branch, kind, plain_snippet(body, max(at, 0), len(needle))))
                continue
            match = _phrase(needle) if table == "code_fts" else _words_prefix(needle)
            if match is None:
                continue
            found = self._conn.execute(
                f"SELECT CAST(tick AS INTEGER), rank, branch_id, kind, snippet({table}, 0, '', '', '…', 24) "
                f"FROM {table} WHERE {table} MATCH ? AND exploration_id = ?{kind_filter} "
                "ORDER BY CAST(tick AS INTEGER) DESC, rank, rowid LIMIT ?",
                [match, *args, limit]).fetchall()
            rows.extend(found)
        rows.sort(key=lambda r: (-r[0], r[1]))
        return [SearchHit(branchId=b, tick=t, kind=k, snippet=s) for t, _, b, k, s in rows[:limit]]

    def close(self) -> None:
        self._conn.close()

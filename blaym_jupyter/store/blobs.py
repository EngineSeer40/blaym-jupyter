"""The file blob store. Implements `ports.BlobStore`.

Files: `<root>/explorations/<explorationId>/blobs/<sha256 hex>` (gzip), written first into
`<root>/explorations/<explorationId>/staging/<operationId>/`. The `blobs` table row in
`metadata.sqlite3` is the record that a blob exists.

Write order (a crash at any point leaves no row without a file):
  1. write the compressed bytes into staging, fsync, read back and verify the hash
  2. `os.replace` into `blobs/`, fsync the folder
  3. only then insert the row (`INSERT OR IGNORE`)
A crash after 1 leaves staging debris; after 2, an orphan file without a row. Both are
harmless: a later `put` of the same content adopts or rewrites the file; housekeeping
removes debris that is never adopted. No redaction logic lives here: `redact` is a parameter.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
import threading
import uuid
import zlib
from pathlib import Path
from typing import Callable, Iterable

from blaym_jupyter_types import PutBlobResponse

from blaym_jupyter.ports.blobs import (
    HASH_PREFIX,
    BlobCorrupt,
    BlobLimits,
    BlobNotFound,
    check_exploration_id,
    content_hash_of,
    hash_hex,
    prepare_content,
)

from .db import open_database, translate_busy


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # not supported on every platform
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class FileBlobStore:
    def __init__(self, root: Path | str, synchronous: str = "FULL", busy_timeout: float = 5.0):
        self.root = Path(root)
        self._conn = open_database(self.root, synchronous, busy_timeout)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- paths --------------------------------------------------------------------

    def _exploration_dir(self, exploration_id: str) -> Path:
        return self.root / "explorations" / check_exploration_id(exploration_id)

    def blob_path(self, exploration_id: str, content_hash: str) -> Path:
        hex_ = hash_hex(content_hash)
        if hex_ is None:
            raise BlobNotFound(content_hash)
        return self._exploration_dir(exploration_id) / "blobs" / hex_

    # -- writes -------------------------------------------------------------------

    @translate_busy
    def put(
        self,
        exploration_id: str,
        data: bytes | Iterable[bytes],
        limits: BlobLimits = BlobLimits(),
        redact: Callable[[str], str] | None = None,
    ) -> PutBlobResponse:
        base = self._exploration_dir(exploration_id)
        prepared = prepare_content(data, limits, redact)
        content_hash = content_hash_of(prepared.stored)
        hex_ = hash_hex(content_hash)
        final = base / "blobs" / hex_
        result = PutBlobResponse(
            contentHash=content_hash,
            originalBytes=prepared.original_bytes,
            storedBytes=len(prepared.stored),
            truncated=prepared.truncated,
        )
        with self._lock:
            have_row = self._row(exploration_id, content_hash) is not None
            if have_row and final.is_file():
                return result
            if final.is_file() and self._file_ok(final, hex_):
                # an orphan file from a crash: adopt it
                self._insert_row(exploration_id, content_hash, result)
                return result
            # else: no file, or a damaged one, or a row whose file is gone: write it again
            staging = base / "staging" / uuid.uuid4().hex
            staging.mkdir(parents=True)
            staged = staging / hex_
            self._write_staged(staged, prepared.stored, hex_, limits.gzip_level)
            (base / "blobs").mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)
            _fsync_dir(base / "blobs")
            self._insert_row(exploration_id, content_hash, result)
            shutil.rmtree(staging, ignore_errors=True)
        return result

    @staticmethod
    def _write_staged(path: Path, stored: bytes, hex_: str, level: int) -> None:
        with open(path, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=level, mtime=0) as gz:
                gz.write(stored)
            raw.flush()
            os.fsync(raw.fileno())
        if not FileBlobStore._file_ok(path, hex_):
            raise BlobCorrupt(f"staged file did not verify: {path.name}")

    @staticmethod
    def _file_ok(path: Path, hex_: str) -> bool:
        try:
            with gzip.open(path, "rb") as fh:
                digest = hashlib.sha256()
                while chunk := fh.read(1 << 20):
                    digest.update(chunk)
        except (OSError, EOFError, zlib.error):
            return False
        return digest.hexdigest() == hex_

    def _row(self, exploration_id: str, content_hash: str):
        return self._conn.execute(
            "SELECT 1 FROM blobs WHERE exploration_id=? AND hash=?", (exploration_id, content_hash)
        ).fetchone()

    def _insert_row(self, exploration_id: str, content_hash: str, result: PutBlobResponse) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO blobs(exploration_id, hash, stored_bytes, original_bytes, truncated)"
            " VALUES (?,?,?,?,?)",
            (exploration_id, content_hash, result.storedBytes, result.originalBytes, int(result.truncated)),
        )

    # -- reads --------------------------------------------------------------------

    def exists(self, exploration_id: str, content_hash: str) -> bool:
        if hash_hex(content_hash) is None:
            return False
        check_exploration_id(exploration_id)
        with self._lock:
            return self._row(exploration_id, content_hash) is not None and self.blob_path(
                exploration_id, content_hash
            ).is_file()

    def cleanup_staging(self) -> int:
        removed = 0
        with self._lock:
            for staging in sorted((self.root / "explorations").glob("*/staging")):
                for entry in sorted(staging.iterdir()):
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
        return removed

    def orphans(self, exploration_id: str) -> list[str]:
        folder = self._exploration_dir(exploration_id) / "blobs"
        if not folder.is_dir():
            return []
        with self._lock:
            found = [
                HASH_PREFIX + f.name
                for f in sorted(folder.iterdir())
                if f.is_file() and hash_hex(HASH_PREFIX + f.name) and self._row(exploration_id, HASH_PREFIX + f.name) is None
            ]
        return found

    def hashes(self, exploration_id: str) -> list[str]:
        check_exploration_id(exploration_id)
        with self._lock:
            rows = self._conn.execute("SELECT hash FROM blobs WHERE exploration_id=? ORDER BY hash", (exploration_id,))
            return [r[0] for r in rows.fetchall()]

    def read(self, exploration_id: str, content_hash: str) -> bytes:
        path = self.blob_path(exploration_id, content_hash)  # BlobNotFound for a malformed hash
        with self._lock:
            if self._row(exploration_id, content_hash) is None:
                raise BlobNotFound(content_hash)
        try:
            with gzip.open(path, "rb") as fh:
                stored = fh.read()
        except FileNotFoundError as exc:
            raise BlobCorrupt(f"file missing for stored blob {content_hash}") from exc
        except (OSError, EOFError, zlib.error) as exc:
            raise BlobCorrupt(f"blob {content_hash} is unreadable: {exc}") from exc
        if content_hash_of(stored) != content_hash:
            raise BlobCorrupt(f"blob {content_hash} does not match its hash")
        return stored

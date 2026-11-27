"""The folder implementation of `RegistryStore`: one small JSON file per notebook.

`<root>/notebooks/<notebookId>_metadata.json`. Writes are atomic (temp file in the same folder,
flushed and fsynced, then renamed over the old file, then the folder is fsynced), so a crash
leaves either the old record or the new one, never half of one. The format is private to
`recording/`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

_SUFFIX = "_metadata.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class FolderRegistryStore:
    def __init__(self, root: str | os.PathLike[str]):
        self._dir = Path(root) / "notebooks"
        self.unreadable = 0  # files that were not valid JSON objects at the last `load_all`

    def _path(self, notebook_id: str) -> Path:
        if not _SAFE_ID.match(notebook_id):
            raise ValueError("unsafe notebook id")
        return self._dir / f"{notebook_id}{_SUFFIX}"

    def load_all(self) -> list[dict[str, Any]]:
        self.unreadable = 0
        records: list[dict[str, Any]] = []
        if not self._dir.is_dir():
            return records
        for path in sorted(self._dir.glob(f"*{_SUFFIX}")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.unreadable += 1
                continue
            if isinstance(data, dict):
                records.append(data)
            else:
                self.unreadable += 1
        return records

    def save(self, record: dict[str, Any]) -> None:
        final = self._path(record["notebookId"])
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f".{final.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(json.dumps(record, sort_keys=True, indent=1).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            self._commit(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._sync_dir()

    def _commit(self, tmp: Path, final: Path) -> None:
        """The one step that makes the new record visible (tests replace it to simulate a crash)."""
        os.replace(tmp, final)

    def _sync_dir(self) -> None:
        try:
            fd = os.open(self._dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def delete(self, notebook_id: str) -> None:
        self._path(notebook_id).unlink(missing_ok=True)

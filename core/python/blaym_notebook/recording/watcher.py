"""The file watcher: changes to notebook files made outside the editor, and the editor's save step.

Telling the editor's saves from other writers: the editor's save step reports when it starts
(`save_started`, *before the file is replaced*) and when it finishes (`save_finished`, right after
the editor's own write, which reads the file back and records its normalised hash as the one the
editor last wrote). The watcher never judges a file while a save of it is in flight, and
throws away a verdict if a save started or finished while it was reading (the generation check).
No timing guesses from a save event.

Watching: watchdog's observer on the folders of recorded notebooks when it is installed and the
files are not under `/mnt/` (no inotify there); otherwise polling by mtime and size every 2 s.
`status()` says which, and why.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from blaym_notebook.recording.outside import (
    NotebookParseError,
    OutsideChangeDetector,
    TipCell,
    choose_mode,
    content_hash,
    parse_notebook,
)
from blaym_notebook.recording.sink import PendingWrite

log = logging.getLogger("blaym_notebook.watcher")

SAVE_STALE_MS = 15_000  # a save whose post hook never came (the save failed) stops blocking after this

# (cell revisions, tip cells); the tip is None while the notebook has no recorded history yet
StateSource = Callable[[str], "tuple[dict[str, int], Optional[list[TipCell]]]"]


class FileWatcher:
    def __init__(
        self,
        registry: Any,
        sink: Any,
        detector: OutsideChangeDetector,
        state: StateSource,
        scheduler: Any,
        ids: Any,
        base_dir: Path,
        *,
        mode: Optional[str] = None,
        debounce_ms: int = 300,
        poll_ms: int = 2000,
        clock_ms: Callable[[], int] = lambda: int(time.monotonic() * 1000),
    ):
        self._reg, self._sink, self._det, self._state = registry, sink, detector, state
        self._sched, self._ids, self._base = scheduler, ids, Path(base_dir)
        self._debounce, self._poll, self._now = debounce_ms, poll_ms, clock_ms
        self._mode, self._why = (mode, "") if mode else choose_mode(self._base)
        self._lock = threading.Lock()
        self._timers: dict[str, Callable[[], None]] = {}
        self._sig: dict[str, Any] = {}
        self._saving: dict[str, list[int]] = {}  # path -> start times of saves in flight
        self._gen: dict[str, int] = {}
        self._gaps: dict[str, str] = {}  # notebook id -> open gap id
        self._observer: Any = None
        self._dirs: set[str] = set()
        self._closed = False
        self.stats = {"outside": 0, "ignored": 0, "gaps": 0}

    def status(self) -> dict[str, str]:
        return {"mode": self._mode, "reason": self._why}

    # -- the editor's own saves -----------------------------------------------------------------

    def save_started(self, path: str) -> None:
        with self._lock:
            self._saving.setdefault(path, []).append(self._now())
            self._gen[path] = self._gen.get(path, 0) + 1

    def save_finished(self, path: str) -> None:
        """Right after the editor's own write: its normalised hash is the last written."""
        digest = None
        try:
            digest = content_hash(parse_notebook((self._base / path).read_bytes()))
        except (OSError, NotebookParseError):
            pass
        with self._lock:
            starts = self._saving.get(path)
            if starts:
                starts.pop(0)
                if not starts:
                    del self._saving[path]
            self._gen[path] = self._gen.get(path, 0) + 1
            if digest is not None:
                self._reg.note_save(path, digest)

    def _is_saving(self, path: str) -> bool:
        starts = self._saving.get(path)
        if not starts:
            return False
        fresh = [t for t in starts if self._now() - t < SAVE_STALE_MS]
        if fresh:
            self._saving[path] = fresh
            return True
        del self._saving[path]
        return False

    # -- watching -------------------------------------------------------------------------------

    def start(self) -> None:
        self._tick()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            timers, self._timers = list(self._timers.values()), {}
        for cancel in timers:
            cancel()
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:  # noqa: BLE001
                pass

    def _tick(self) -> None:
        """Look at the recorded notebooks' files (and, with watchdog, make sure their folders are
        watched). The first sight of a file is checked, which is the start-up scan."""
        if self._closed:
            return
        try:
            for _nb, path, _last in self._reg.watched():
                if self._mode == "watchdog":
                    self._watch_dir(os.path.dirname(path))
                try:
                    st = (self._base / path).stat()
                    sig: Any = (st.st_mtime_ns, st.st_size)
                except OSError:
                    sig = None
                if path not in self._sig or (self._mode == "polling" and sig != self._sig[path]):
                    self._sig[path] = sig
                    self.schedule(path)
        except Exception as exc:  # noqa: BLE001 - the watcher never takes the server down
            log.warning("file watcher tick failed: %s", type(exc).__name__)
        finally:
            if not self._closed:
                self._sched.call_later(self._poll, self._tick)

    def _watch_dir(self, rel_dir: str) -> None:
        if rel_dir in self._dirs:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return
        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                for p in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
                    if p:
                        try:
                            watcher.schedule(os.path.relpath(os.fsdecode(p), watcher._base))
                        except ValueError:
                            pass

        if self._observer is None:
            self._observer = Observer()
            self._observer.daemon = True
            self._observer.start()
        self._observer.schedule(Handler(), str(self._base / rel_dir), recursive=False)
        self._dirs.add(rel_dir)

    def schedule(self, path: str, attempt: int = 0, delay: Optional[int] = None) -> None:
        """Check `path` after the debounce; a newer call for the same path replaces an older one."""
        if not any(p == path for _, p, _ in self._reg.watched()):
            return
        with self._lock:
            if self._closed:
                return
            old = self._timers.pop(path, None)
            if old:
                old()
            self._timers[path] = self._sched.call_later(
                self._debounce if delay is None else delay, lambda: self.check(path, attempt))

    # -- the check ------------------------------------------------------------------------------

    def check(self, path: str, attempt: int = 0) -> None:
        with self._lock:
            self._timers.pop(path, None)
        try:
            self._check(path, attempt)
        except Exception as exc:  # noqa: BLE001
            log.warning("a file check failed: %s", type(exc).__name__, exc_info=True)

    def _check(self, path: str, attempt: int) -> None:
        entry = next((e for e in self._reg.watched() if e[1] == path), None)
        if entry is None:
            return
        nb_id, _, last = entry
        with self._lock:
            if self._is_saving(path):
                busy, gen = True, 0
            else:
                busy, gen = False, self._gen.get(path, 0)
        if busy:
            return self.schedule(path, attempt)
        try:
            raw = (self._base / path).read_bytes()
        except OSError:  # briefly missing (write to a temp file, then rename): look once more
            if attempt == 0:
                self.schedule(path, 1)
            return  # still missing: the registry scan marks the record
        try:
            nb = parse_notebook(raw)
        except NotebookParseError:  # a partial write: look once more, then it is a gap
            if attempt == 0:
                return self.schedule(path, 1)
            return self._open_gap(nb_id)
        revisions, tip = self._state(nb_id)
        decision = (self._det.inspect(nb_id, path, nb, last, revisions, tip) if tip is not None
                    else None)
        digest = decision.hash if decision else content_hash(nb)
        with self._lock:
            if self._is_saving(path) or self._gen.get(path, 0) != gen:
                changed = True  # the editor wrote while we read: its write is judged by its own hook
            else:
                changed = False
        if changed:
            return self.schedule(path, attempt)
        events: list[dict] = []
        blobs: dict[str, bytes] = {}
        if decision is not None and decision.kind == "outside":
            events.append(decision.event)  # type: ignore[arg-type]
            blobs = decision.blobs
            self.stats["outside"] += 1
        else:
            self.stats["ignored"] += 1
        gap = self._gaps.pop(nb_id, None)
        if gap is not None:
            events.append(self._event("recording_gap_ended", nb_id, {"gapId": gap}))
        if events:
            self._sink.submit(PendingWrite(nb_id, events, blobs))
        if digest != last:
            self._reg.note_save(path, digest)
        if decision is not None and decision.kind == "outside":
            self._reg.request_resync(nb_id)

    def _open_gap(self, nb_id: str) -> None:
        if nb_id in self._gaps:
            return
        gap = self._ids.new()
        self._gaps[nb_id] = gap
        self.stats["gaps"] += 1
        self._sink.submit(PendingWrite(nb_id, [self._event(
            "recording_gap_started", nb_id, {"gapId": gap, "source": "files", "reason": "unknown"})], {}))

    def _event(self, type_: str, nb_id: str, payload: dict) -> dict:
        return {"schemaVersion": 2, "eventId": self._ids.new(), "explorationId": nb_id,
                "wallTimeMs": self._det._clock.now_ms(), "userId": "local", "workspaceId": "local",
                "originId": self._det._origin, "editor": self._det._editor, "type": type_,
                "payload": payload}

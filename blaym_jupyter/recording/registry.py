"""The notebook registry (S5-I, S5-J, S5-S).

One small record per notebook, kept behind `RegistryStore`; which recorder instance is the
active one is held in memory only (a server restart forgets it, and the heartbeat's
`needsRegister` brings every recorder back). Records are never deleted. Blaym writes nothing
into notebook files: identity is decided here from the path and the content fingerprint.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from blaym_jupyter_types import (
    NotebookNotice,
    NotebookRegistration,
    NotebookStatus,
    OpenGap,
)

from .identity import fingerprint_dict, match_record
from .ports import Clock, Ids, RegistryStore, StateProbe

RECORD_VERSION = 1
DEFAULT_ALIVE_WINDOW_MS = 90_000

_REQUIRED = ("notebookId", "recording", "createdMs", "fingerprint")


@dataclass
class _Live:
    holder: Optional[str] = None
    seen: dict[str, int] = field(default_factory=dict)  # instance id -> last heard (ms)
    resync: set[str] = field(default_factory=set)  # instances that should send a snapshot
    aliases: dict[str, str] = field(default_factory=dict)


class Registry:
    def __init__(
        self,
        store: RegistryStore,
        clock: Clock,
        ids: Ids,
        *,
        probe: Optional[StateProbe] = None,
        alive_window_ms: int = DEFAULT_ALIVE_WINDOW_MS,
        path_exists: Callable[[str], bool] = os.path.exists,
    ):
        self._store = store
        self._clock = clock
        self._ids = ids
        self._probe = probe
        self._window = alive_window_ms
        self._path_exists = path_exists
        self._lock = threading.RLock()
        self._live: dict[str, _Live] = {}
        self._was_clean: dict[str, bool] = {}
        # counted notes about record files that are left alone
        self.counters = {"future_version": 0, "malformed": 0, "unreadable": 0}
        self._records: dict[str, dict[str, Any]] = {}
        for rec in store.load_all():
            if not isinstance(rec, dict) or any(k not in rec for k in _REQUIRED):
                self.counters["malformed"] += 1
            elif not isinstance(rec.get("version"), int) or rec["version"] > RECORD_VERSION:
                self.counters["future_version"] += 1  # never read, never rewritten
            else:
                self._records[rec["notebookId"]] = rec
        self.counters["unreadable"] = getattr(store, "unreadable", 0)

    # ---- identity ---------------------------------------------------------------------

    def register(self, reg: NotebookRegistration) -> NotebookStatus:
        with self._lock:
            now = self._clock.now_ms()
            rec, notices = self._identify(reg, now)
            nb = rec["notebookId"]
            self._was_clean[nb] = bool(rec.get("cleanClose", True))
            if reg.collaboration:
                return self._status(rec, reg.instanceId, active=False,
                                    reason="collaboration_not_supported", notices=notices,
                                    recording=False)
            live = self._live.setdefault(nb, _Live())
            live.seen[reg.instanceId] = now
            active, gained = self._claim(live, reg.instanceId, bool(reg.takeover), now)
            changed = False
            if active:
                live.aliases = dict(reg.cellAliases)
                if gained:
                    live.resync.add(reg.instanceId)
                if reg.setRecording is not None and reg.setRecording != rec["recording"]:
                    rec["recording"] = reg.setRecording
                    changed = True
            if rec.get("cleanClose", True):
                rec["cleanClose"] = False
                changed = True
            if changed:
                self._save(rec)
            return self._status(rec, reg.instanceId, active=active, notices=notices)

    def _identify(self, reg: NotebookRegistration, now: int) -> tuple[dict[str, Any], list[str]]:
        fp = fingerprint_dict(reg.fingerprint)
        for rec in self._records.values():
            if rec.get("path") == reg.path:
                if rec.get("pathMissing") or rec["fingerprint"] != fp or rec.get("cellIdsStable") != reg.cellIdsStable:
                    rec["pathMissing"] = False
                    rec["fingerprint"] = fp
                    rec["cellIdsStable"] = reg.cellIdsStable
                    self._save(rec)
                return rec, []
        found = match_record(reg.cellIdsStable, fp, self._records.values(), self._path_exists)
        if found is not None:
            old = found.get("path")
            if not old or not self._path_exists(old):
                if old:
                    found.setdefault("previousPaths", []).append(old)
                found["path"] = reg.path
                found["pathMissing"] = False
                found["fingerprint"] = fp
                found["cellIdsStable"] = reg.cellIdsStable
                self._save(found)
                return found, ["moved"]
            notices = ["copied"]
        else:
            notices = []
        rec = {
            "version": RECORD_VERSION,
            "notebookId": self._ids.new(),
            "recording": False,
            "path": reg.path,
            "previousPaths": [],
            "pathMissing": False,
            "cellIdsStable": reg.cellIdsStable,
            "fingerprint": fp,
            "lastWrittenHash": None,
            "createdMs": now,
            "cleanClose": True,
        }
        self._records[rec["notebookId"]] = rec
        self._save(rec)
        return rec, notices

    # ---- the active instance ----------------------------------------------------------

    def _fresh(self, live: _Live, instance_id: Optional[str], now: int) -> bool:
        ts = live.seen.get(instance_id) if instance_id else None
        return ts is not None and now - ts <= self._window

    def _claim(self, live: _Live, instance_id: str, takeover: bool, now: int) -> tuple[bool, bool]:
        """(is active, took the notebook from another instance)."""
        holder = live.holder
        if holder == instance_id:
            return True, False
        if holder is None or takeover or not self._fresh(live, holder, now):
            live.holder = instance_id
            return True, holder is not None
        return False, False

    def status(self, notebook_id: str, instance_id: str) -> NotebookStatus:
        """The heartbeat."""
        with self._lock:
            rec = self._records.get(notebook_id)
            live = self._live.get(notebook_id)
            if rec is None or live is None or instance_id not in live.seen:
                return NotebookStatus(
                    apiVersion=1, notebookId=notebook_id, explorationId=notebook_id,
                    recording=bool(rec and rec["recording"]), active=False, needsRegister=True,
                    needsResync=False, runsOpen=0, openGaps=[], cellRevisions={}, notices=[],
                )
            now = self._clock.now_ms()
            live.seen[instance_id] = now
            active, gained = self._claim(live, instance_id, False, now)
            if gained:
                live.resync.add(instance_id)
            return self._status(rec, instance_id, active=active, notices=[])

    def close(self, notebook_id: str, instance_id: str) -> None:
        with self._lock:
            live = self._live.get(notebook_id)
            rec = self._records.get(notebook_id)
            if live is None or rec is None:
                return
            live.seen.pop(instance_id, None)
            live.resync.discard(instance_id)
            if live.holder == instance_id:
                live.holder = None
            if not live.seen and not rec.get("cleanClose"):
                rec["cleanClose"] = True
                self._save(rec)

    def gate_ingest(self, instance_id: str, exploration_id: str) -> Literal["ok", "not_active_recorder"]:
        with self._lock:
            live = self._live.get(exploration_id)
            if live is not None and live.holder == instance_id:
                return "ok"
            return "not_active_recorder"

    def request_resync(self, notebook_id: str) -> None:
        """Ask the active instance for a fresh snapshot at its next heartbeat."""
        with self._lock:
            live = self._live.get(notebook_id)
            if live is not None and live.holder is not None:
                live.resync.add(live.holder)

    # ---- reads for other recording parts ----------------------------------------------

    def aliases(self, notebook_id: str) -> dict[str, str]:
        with self._lock:
            live = self._live.get(notebook_id)
            return dict(live.aliases) if live else {}

    def alias_for(self, notebook_id: str, model_cell_id: str) -> str:
        with self._lock:
            live = self._live.get(notebook_id)
            return live.aliases.get(model_cell_id, model_cell_id) if live else model_cell_id

    def is_alive(self, notebook_id: str) -> bool:
        with self._lock:
            live = self._live.get(notebook_id)
            now = self._clock.now_ms()
            return bool(live) and any(now - ts <= self._window for ts in live.seen.values())

    def was_clean_close(self, notebook_id: str) -> bool:
        """Whether the notebook's last session ended cleanly, as seen at its latest register."""
        with self._lock:
            return self._was_clean.get(notebook_id, True)

    def recording(self, notebook_id: str) -> bool:
        with self._lock:
            rec = self._records.get(notebook_id)
            return bool(rec and rec["recording"])

    def notebook_id_for_path(self, path: str) -> Optional[str]:
        with self._lock:
            for rec in self._records.values():
                if rec.get("path") == path:
                    return rec["notebookId"]
            return None

    # ---- file events ------------------------------------------------------------------

    def note_save(self, path: str, content_hash: str) -> None:
        with self._lock:
            for rec in self._records.values():
                if rec.get("path") == path and rec.get("lastWrittenHash") != content_hash:
                    rec["lastWrittenHash"] = content_hash
                    self._save(rec)

    def note_rename(self, old: str, new: str) -> None:
        with self._lock:
            mover = next((r for r in self._records.values() if r.get("path") == old), None)
            if mover is None:
                return
            for other in self._records.values():
                if other is not mover and other.get("path") == new:  # overwritten by the rename
                    other["path"] = None
                    other["pathMissing"] = True
                    other.setdefault("previousPaths", []).append(new)
                    self._save(other)
            mover["path"] = new
            mover["pathMissing"] = False
            mover.setdefault("previousPaths", []).append(old)
            self._save(mover)

    def scan(self, paths_exist: Callable[[str], bool]) -> list[str]:
        """Mark records whose file is gone (and unmark those that came back). Keeps every record.
        Returns the ids of the records now marked missing."""
        with self._lock:
            missing: list[str] = []
            for rec in self._records.values():
                path = rec.get("path")
                gone = not path or not paths_exist(path)
                if gone:
                    missing.append(rec["notebookId"])
                if bool(rec.get("pathMissing")) != gone:
                    rec["pathMissing"] = gone
                    self._save(rec)
            return missing

    # ---- helpers ----------------------------------------------------------------------

    def _save(self, rec: dict[str, Any]) -> None:
        self._store.save(rec)

    def _status(
        self,
        rec: dict[str, Any],
        instance_id: str,
        *,
        active: bool,
        notices: list[str],
        reason: Optional[str] = None,
        recording: Optional[bool] = None,
    ) -> NotebookStatus:
        nb = rec["notebookId"]
        live = self._live.get(nb)
        holder = live.holder if live else None
        if not active and reason is None:
            reason = "other_recorder_active"
        needs_resync = False
        if active and live is not None and instance_id in live.resync:
            live.resync.discard(instance_id)
            needs_resync = True
        probe = self._probe
        return NotebookStatus(
            apiVersion=1,
            notebookId=nb,
            explorationId=nb,
            recording=rec["recording"] if recording is None else recording,
            active=active,
            holderInstanceId=holder if not active and holder else None,
            reason=reason if not active else None,
            needsRegister=False,
            needsResync=needs_resync,
            runsOpen=probe.runs_open(nb) if probe else 0,
            openGaps=list(probe.open_gaps(nb)) if probe else [],
            cellRevisions=dict(probe.cell_revisions(nb)) if probe else {},
            notices=[NotebookNotice(code=c) for c in notices],  # type: ignore[arg-type]
        )


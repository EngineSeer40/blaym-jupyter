"""Kernel to notebook binding: which recorded notebook a kernel belongs to.

The chain is kernel id, then the session manager (its notebook path), then the registry (path to
notebook record). It is made **lazily**, on the first run request of a kernel: when Jupyter
announces a kernel start the session record may not exist yet. Only registered notebooks whose
recording is on are bound. A binding follows a rename (`rename_path`; a rename is not a new notebook) and is
dropped when recording is switched off or the kernel goes away.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

log = logging.getLogger("blaym_jupyter.kernels")


class RegistryReads(Protocol):
    def notebook_id_for_path(self, path: str) -> Optional[str]: ...

    def recording(self, notebook_id: str) -> bool: ...


SessionPath = Callable[[str], Awaitable[Optional[str]]]


@dataclass(frozen=True)
class Binding:
    notebook_id: str
    path: str


class KernelBindings:
    def __init__(self, registry: RegistryReads, session_path: SessionPath):
        self._registry, self._session_path = registry, session_path
        self._bound: dict[str, Binding] = {}

    async def resolve(self, kernel_id: str) -> Optional[Binding]:
        """The binding of a kernel, made now if there is none; None when the kernel is not recorded.
        A binding whose notebook stopped recording is dropped (the caller sees `None`)."""
        held = self.current(kernel_id)
        if held is not None:
            return held
        try:
            path = await self._session_path(kernel_id)
        except Exception as exc:  # noqa: BLE001 - no session (yet): not bound, asked again next time
            log.debug("no session for kernel %s: %s", kernel_id, type(exc).__name__)
            return None
        if not path:
            return None
        notebook_id = self._registry.notebook_id_for_path(path)
        if notebook_id is None or not self._registry.recording(notebook_id):
            return None
        binding = self._bound[kernel_id] = Binding(notebook_id, path)
        return binding

    def current(self, kernel_id: str) -> Optional[Binding]:
        """The held binding, if its notebook still records (otherwise it is dropped)."""
        held = self._bound.get(kernel_id)
        if held is not None and not self._registry.recording(held.notebook_id):
            del self._bound[kernel_id]
            return None
        return held

    def was_bound(self, kernel_id: str) -> bool:
        return kernel_id in self._bound

    def unbind(self, kernel_id: str) -> None:
        self._bound.pop(kernel_id, None)

    def rename_path(self, old: str, new: str) -> list[str]:
        """The notebook at `old` was renamed to `new` (the registry has moved its record): bindings
        follow the path, so a run in progress keeps being recorded. Returns the kernel ids."""
        moved = [k for k, b in self._bound.items() if b.path == old]
        for k in moved:
            self._bound[k] = Binding(self._bound[k].notebook_id, new)
        return moved

"""The core must load without the Jupyter server (S3c, Task 10a, Proposal 3)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parent
CORE_FOLDERS = ["history", "ports", "privacy", "store", "service"]
ALLOWED_THIRD_PARTY = {"blaym_jupyter_types"}

# The import matrix of S4 decision L: for each folder, which other blaym_jupyter parts it may
# import. `testing/` and `tests/` may import anything in core. Folders that do not exist yet
# are added to the matrix as they are built.
MAY_IMPORT = {
    "history": set(),
    "ports": set(),
    "privacy": set(),  # a pure module: the standard library and the contracts only
    "store": {"ports"},
    "service": {"history", "ports", "privacy"},  # never store, never sqlite3 (see the test below)
}

_SCRIPT = """
import sys

sys.path.insert(0, {root!r})
BLOCKED = ("jupyter_server", "tornado", "jupyterlab", "notebook")


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked: " + name)
        return None


sys.meta_path.insert(0, Blocker())
from blaym_jupyter.history import Rebuilder
from blaym_jupyter.tests.history.scenario import Scenario

s = Scenario()
s.start(cells=[("c1", "x = 1")])
s.edit("c1", "x = 2")
rb = Rebuilder()
rb.add(s.events)
result = rb.state_at("main", 2)
assert result.model_dump(mode="json"), result
assert not any(m.split(".")[0] in BLOCKED for m in sys.modules), "server module loaded"

# the service over the in-memory ports, and over SQLite through the composition point
import tempfile
from pathlib import Path

from blaym_jupyter import local
from blaym_jupyter.service.auth import FixedTokenAuthorizer
from blaym_jupyter.service.core import Service
from blaym_jupyter.testing.fakes import FakeClock, IdFactory
from blaym_jupyter.testing.memory import MemoryBlobStore, MemoryEventStore
from blaym_jupyter.tests.history.scenario import EXPLORATION
from blaym_jupyter.tests.service.helpers import recorder
from blaym_jupyter_types import (
    EventsSinceRequest, IngestRequest, OutlineRequest, SearchRequest, StateAtRequest,
)

TOKEN = "t"


def exercise(service):
    exploration = EXPLORATION
    events = Scenario()
    events.start(cells=[("c1", "needle = 1")])
    events.edit("c1", "needle = 2")
    out = service.ingest(TOKEN, IngestRequest(
        explorationId=exploration, editor="jupyterlab", instanceId="tab-1",
        events=[recorder(e) for e in events.events]))
    assert [o.root.status for o in out.outcomes] == ["accepted", "accepted"], out
    branch = out.outcomes[0].root.branchId
    state = service.state_at(TOKEN, StateAtRequest(explorationId=exploration, branchId=branch, tick=2))
    assert state.model_dump(mode="json"), state
    assert service.outline(TOKEN, OutlineRequest(explorationId=exploration)) is not None
    hits = service.search(TOKEN, SearchRequest(explorationId=exploration, query="needle"))
    assert hits.model_dump(mode="json"), hits
    since = service.events_since(TOKEN, EventsSinceRequest(explorationId=exploration, branchId=branch, afterTick=1))
    assert [e.root.tick for e in since.events] == [2], since


exercise(Service(MemoryEventStore(), MemoryBlobStore(), FixedTokenAuthorizer(TOKEN), FakeClock(),
                 new_id=IdFactory("svc")))

with tempfile.TemporaryDirectory() as tmp:
    service = local.open_service(Path(tmp), FixedTokenAuthorizer(TOKEN), recover=True)
    try:
        exercise(service)
    finally:
        service.close()
assert not any(m.split(".")[0] in BLOCKED for m in sys.modules), "server module loaded by the service"
print("ok")
"""


def test_history_loads_without_jupyter_server():
    done = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT.format(root=str(ROOT))],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def _top_level_imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_core_folders_import_only_stdlib_and_contracts():
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for folder in CORE_FOLDERS:
        for path in sorted((PACKAGE / folder).rglob("*.py")):
            for name in _top_level_imports(path):
                if name not in stdlib and name not in ALLOWED_THIRD_PARTY and name != "blaym_jupyter":  # own parts: see the matrix
                    bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, bad


def _parts_imported(source: str, package: str):
    """Parts of blaym_jupyter that `source` (in module package `package`) imports, for
    `blaym_jupyter.<part>` in absolute or relative form."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "blaym_jupyter" and len(parts) > 1:
                    yield parts[1]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                parts = (node.module or "").split(".")
                if parts[0] != "blaym_jupyter":
                    continue
            else:
                pkg = package.split(".")
                parts = pkg[: len(pkg) - (node.level - 1)] + (node.module.split(".") if node.module else [])
            if len(parts) > 1:
                yield parts[1]
            else:  # `from blaym_jupyter import local`
                yield from (a.name for a in node.names)


def _package_imports(path: Path):
    package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    return _parts_imported(path.read_text(), package)


def _core_files(folder: str):
    return sorted((PACKAGE / folder).rglob("*.py"))


def test_import_matrix():
    """history imports nothing of ports or store; ports only the contracts; store only ports."""
    bad = []
    for folder, allowed in MAY_IMPORT.items():
        for path in _core_files(folder):
            for part in _package_imports(path):
                if part != folder and part not in allowed:
                    bad.append(f"{path.relative_to(ROOT)} imports blaym_jupyter.{part}")
    assert not bad, bad


def test_only_the_composition_point_imports_store():
    """Nothing outside `store/` itself, `local.py` (the composition point), `testing/` and
    `tests/` may import `blaym_jupyter.store`."""
    bad = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).parts
        if rel[0] in ("store", "testing", "tests") or rel == ("local.py",):
            continue
        if "store" in set(_package_imports(path)):
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, bad


def test_matrix_helper_reads_absolute_and_relative_imports():
    """The matrix check bites: each form of a forbidden import is seen."""
    forms = [
        "from blaym_jupyter.store import x",
        "import blaym_jupyter.store.events",
        "from blaym_jupyter import store",
        "from ..store import x",
        "from ..store.events import x",
        "from .. import store",
    ]
    for source in forms:
        assert "store" in set(_parts_imported(source, "blaym_jupyter.history")), source
    assert set(_parts_imported("from .log import EventLog\nimport json", "blaym_jupyter.history")) == {"history"}


def test_only_the_composition_point_imports_service():
    """Nothing outside `service/` itself, `local.py` (the composition point), `api/` and
    `adapters/jupyter/` (S5, section 2), `testing/` and `tests/` may import `blaym_jupyter.service`."""
    bad = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).parts
        if rel[0] in ("service", "api", "testing", "tests") or rel[:2] == ("adapters", "jupyter") \
                or rel == ("local.py",):
            continue
        if "service" in set(_package_imports(path)):
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, bad


def test_service_does_not_use_sqlite_or_store():
    bad = []
    for path in _core_files("service"):
        names = set(_top_level_imports(path))
        if "sqlite3" in names or "store" in set(_package_imports(path)):
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, bad


def test_only_the_service_and_the_composition_point_import_privacy():
    """Nothing outside `privacy/` itself, `service/`, `local.py`, `testing/` and `tests/` may
    import `blaym_jupyter.privacy` (the store and the ports never know what a secret is)."""
    bad = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).parts
        if rel[0] in ("privacy", "service", "testing", "tests") or rel == ("local.py",):
            continue
        if "privacy" in set(_package_imports(path)):
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, bad


# -- the editor-neutral recording layer (S5, section 2 of the spec) ------------------------------

RECORDING_FORBIDDEN_PARTS = {"service", "store", "history", "privacy", "api", "adapters", "local", "ports"}
RECORDING_FORBIDDEN_MODULES = {
    "jupyter_server", "jupyter_client", "tornado", "traitlets", "watchdog", "ipykernel", "sqlite3",
    "jupyterlab", "notebook", "nbformat",
}


def _recording_violations(source: str, package: str = "blaym_jupyter.recording") -> list[str]:
    """What `source`, a module of `recording/`, imports that it may not: another part of the
    package than `recording`, a Jupyter or web library, SQLite, or any third party but the contracts."""
    stdlib = set(sys.stdlib_module_names)
    bad = []
    for part in _parts_imported(source, package):
        if part != "recording":
            bad.append(f"blaym_jupyter.{part}")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            if name in RECORDING_FORBIDDEN_MODULES or (
                name not in stdlib and name not in ALLOWED_THIRD_PARTY and name != "blaym_jupyter"
            ):
                bad.append(name)
    return bad


def test_recording_imports_only_stdlib_contracts_and_itself():
    bad = []
    for path in sorted((PACKAGE / "recording").rglob("*.py")):
        package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
        for name in _recording_violations(path.read_text(), package):
            bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, bad


def test_recording_boundary_bites():
    """A planted forbidden import in `recording/` is seen, in every form."""
    for source in [
        "from blaym_jupyter.service import core",
        "import blaym_jupyter.service.core",
        "from ..service import core",
        "from .. import service",
        "from ..store.events import x",
        "from ..history import apply",
        "from ..privacy.redact import redact",
        "from ..api import handle",
        "from ..adapters.jupyter import host",
        "import sqlite3",
        "import tornado.web",
        "from jupyter_server.base import handlers",
        "import traitlets",
        "from watchdog.observers import Observer",
        "import ipykernel",
        "import numpy",
    ]:
        assert _recording_violations(source), source
    assert not _recording_violations(
        "import json\nfrom pathlib import Path\nfrom blaym_jupyter_types import OutputItem\nfrom .metadata import apply_policy\nfrom . import ports"
    )


# -- the framework-free API dispatcher (S5, Task 8) -------------------------------------------------

API_MAY_IMPORT_PARTS = {"api", "service", "recording", "ports"}  # `ports`: the blob errors only
API_FORBIDDEN_MODULES = {
    "tornado", "jupyter_server", "jupyter_client", "traitlets", "jupyterlab", "notebook", "flask",
    "fastapi", "starlette", "aiohttp", "django", "sanic", "bottle", "falcon", "werkzeug", "sqlite3",
    "watchdog", "ipykernel",
}


def _api_violations(source: str, package: str = "blaym_jupyter.api") -> list[str]:
    """What `source`, a module of `api/`, imports that it may not: a part of the package other than
    `api`, `service`, `recording` (and `ports`), a web framework, SQLite, or any third party but
    the contracts and pydantic (the contracts' own base)."""
    stdlib = set(sys.stdlib_module_names)
    bad = [f"blaym_jupyter.{p}" for p in _parts_imported(source, package) if p not in API_MAY_IMPORT_PARTS]
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            if name in API_FORBIDDEN_MODULES or (
                name not in stdlib and name not in ALLOWED_THIRD_PARTY | {"pydantic", "blaym_jupyter"}
            ):
                bad.append(name)
    return bad


def test_api_imports_only_stdlib_contracts_service_and_recording():
    bad = []
    for path in sorted((PACKAGE / "api").rglob("*.py")):
        package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
        for name in _api_violations(path.read_text(), package):
            bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, bad


def test_api_boundary_bites():
    """A planted forbidden import in `api/` is seen, in every form."""
    for source in [
        "import tornado.web",
        "from tornado import web",
        "from jupyter_server.base.handlers import APIHandler",
        "import aiohttp",
        "import flask",
        "from fastapi import FastAPI",
        "import sqlite3",
        "from ..store.events import x",
        "from blaym_jupyter.store import blobs",
        "from ..adapters.jupyter import host",
        "from .. import local",
        "from ..history import apply",
        "from ..privacy.redact import redact",
        "import numpy",
    ]:
        assert _api_violations(source), source
    assert not _api_violations(
        "import json\nfrom blaym_jupyter_types import OutputItem\nfrom ..service.core import Service\n"
        "from ..recording.registry import Registry\nfrom . import codec\nfrom pydantic import BaseModel"
    )


# -- the Jupyter adapter (S5, Task 9) ---------------------------------------------------------------


def test_only_the_extension_entry_points_import_adapters():
    """Nothing outside `adapters/`, `__init__.py` and `routes.py` (the extension entry points,
    which import it lazily or only when the server loads the extension) and `tests/` may import
    `blaym_jupyter.adapters`."""
    bad = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).parts
        if rel[0] in ("adapters", "tests") or rel in (("__init__.py",), ("routes.py",)):
            continue
        if "adapters" in set(_package_imports(path)):
            bad.append(str(path.relative_to(ROOT)))
    assert not bad, bad


def test_adapters_boundary_bites():
    assert "adapters" in set(_parts_imported("from ..adapters.jupyter import host", "blaym_jupyter.service"))
    assert "adapters" in set(_parts_imported("import blaym_jupyter.adapters.jupyter", "blaym_jupyter.api"))

"""The core must load without the Jupyter server (S3c, Task 10a, Proposal 3)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parent
CORE_FOLDERS = ["history"]  # S4 adds "store"
ALLOWED_THIRD_PARTY = {"blaym_jupyter_types"}

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
                if name not in stdlib and name not in ALLOWED_THIRD_PARTY:
                    bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, bad

import pytest

import blaym_jupyter.tests.history.conftest as _profiles  # noqa: F401

from blaym_jupyter.tests.ports.harness import Harness


@pytest.fixture
def sqlite_harness():
    h = Harness("sqlite")
    yield h
    h.close()

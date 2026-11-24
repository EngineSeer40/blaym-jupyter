"""Profiles come from the history tests' conftest (one set of Hypothesis settings)."""

import pytest

import blaym_jupyter.tests.history.conftest as _profiles  # noqa: F401  (registers and loads the profiles)

from .harness import KINDS, Harness


@pytest.fixture(params=KINDS)
def harness(request):
    h = Harness(request.param)
    yield h
    h.close()

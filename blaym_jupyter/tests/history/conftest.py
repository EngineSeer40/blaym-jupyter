"""Hypothesis profiles for the history tests (S3a, Task 4).

`default`: 100 examples for the slowest properties (more for the cheap ones); the whole
history suite stays at about a minute. `deep`: 1,000 examples, for gates and on demand. Choose with the
HYPOTHESIS_PROFILE environment variable, for example
`HYPOTHESIS_PROFILE=deep pytest blaym_jupyter/tests/history`.

A failure prints the reproduce blob and the seed; shrinking stays on. The deadline is
off because rebuilding long histories is slow by design.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

_COMMON = dict(
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                           HealthCheck.large_base_example, HealthCheck.filter_too_much],
)

settings.register_profile("default", max_examples=300, **_COMMON)
settings.register_profile("deep", max_examples=1000, **_COMMON)
PROFILE = os.environ.get("HYPOTHESIS_PROFILE", "default")
settings.load_profile(PROFILE)

# Size knobs for the expensive equivalence properties (real rebuilder against the
# reference): the default profile keeps them small so the suite stays about a minute;
# `deep` keeps the full sizes and example counts.
DEEP = PROFILE == "deep"
POSITION_LIMIT = 14 if DEEP else 8  # positions checked per branch (see test_generator._positions)
MAX_EVENTS = 80 if DEEP else 40  # longest generated history in the equivalence properties
DELIVERY_SETTINGS = settings() if DEEP else settings(max_examples=100)


def pytest_report_header(config):
    return f"hypothesis profile: {PROFILE} (max_examples={settings.default.max_examples})"

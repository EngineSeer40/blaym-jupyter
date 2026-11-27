"""The editor-neutral recording layer (S5).

Imports the standard library and `blaym_notebook_types` only (never the service, the store,
history, privacy, the API, the adapters or any Jupyter library): an adapter for another
editor reuses it unchanged. Enforced by `tests/test_core_boundary.py`.
"""

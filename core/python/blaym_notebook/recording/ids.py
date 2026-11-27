"""Random ids for notebook records."""

from __future__ import annotations

import uuid


class RandomIds:
    """Random ids for notebook records (never a hash of a path)."""

    def new(self) -> str:
        return uuid.uuid4().hex

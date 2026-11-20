import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from blaym_jupyter.contracts import BlaymEventEnvelope

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "envelope"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_valid_envelope_parses():
    BlaymEventEnvelope.model_validate(_load("valid-envelope.json"))


def test_valid_envelope_round_trips_to_same_json():
    raw = _load("valid-envelope.json")
    model = BlaymEventEnvelope.model_validate(raw)
    assert model.model_dump(mode="json", exclude_none=True) == raw


INVALID = _load("invalid-envelopes.json")


@pytest.mark.parametrize("case", INVALID, ids=[c["name"] for c in INVALID])
def test_invalid_envelope_rejected(case):
    with pytest.raises(ValidationError):
        BlaymEventEnvelope.model_validate(case["envelope"])

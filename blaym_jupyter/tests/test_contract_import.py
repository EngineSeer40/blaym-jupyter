from blaym_jupyter_types import BlaymEventEnvelope


def test_contracts_import_from_types_package():
    assert BlaymEventEnvelope.__module__.startswith("blaym_jupyter_types")

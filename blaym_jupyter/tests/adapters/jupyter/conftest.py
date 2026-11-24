import pytest


@pytest.fixture
def jp_server_config(jp_server_config, tmp_path):
    """The root conftest's config plus a Blaym data folder under the test's own temp folder."""
    jp_server_config["BlaymConfig"] = {"root": str(tmp_path / "blaym")}
    return jp_server_config


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: real server and kernel; run at checkpoints and the slice gate only")

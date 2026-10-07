import pytest


@pytest.fixture(autouse=True)
def triage_logs_in_the_test_directory(tmp_path, monkeypatch):
    """No test writes a triage log into the checkout: the log directory is under tmp_path."""
    monkeypatch.setenv("AZSQLCD_LOG_DIR", str(tmp_path / "triage-logs"))

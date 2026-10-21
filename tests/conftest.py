import pytest


@pytest.fixture(autouse=True)
def no_actions_run_id(monkeypatch):
    """Inside GitHub Actions every result would share the workflow's run id and
    merge would treat a test's second night as a duplicate. Tests that care set
    the variables themselves."""
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)

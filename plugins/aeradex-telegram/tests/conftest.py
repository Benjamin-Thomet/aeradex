import pytest


@pytest.fixture(autouse=True)
def _no_real_claude_code(monkeypatch):
    """Starting a call warms up Claude Code in the background; tests never start the real CLI."""
    from aeradex_anruf.agent import RemoteCodeAgent
    monkeypatch.setattr(RemoteCodeAgent, "warm_up", lambda self: None)

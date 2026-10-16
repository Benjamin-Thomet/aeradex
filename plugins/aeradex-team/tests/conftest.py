import pytest


@pytest.fixture(autouse=True)
def no_real_agent(monkeypatch):
    """Tests never call a real agent (Claude Code/Codex would use the developer's login): the spelling correction
    sees no answer unless a test provides one. `korrektur.real_ask` keeps the real function for tests that fake
    the layer below it (HTTP)."""
    from aeradex_team import korrektur
    monkeypatch.setattr(korrektur, "real_ask", korrektur.ask, raising=False)
    monkeypatch.setattr(korrektur, "ask", lambda book, texts: {})

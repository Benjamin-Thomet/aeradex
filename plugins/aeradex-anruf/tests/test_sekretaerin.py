"""Susi as a secretary who knows her boss: persona, memory across calls, greeting, a note at the end of a call."""
import time

from starlette.testclient import TestClient

from aeradex.testing import make_book

from aeradex_anruf import gateway, gedaechtnis
from aeradex_anruf.agent import RemoteAgent, make_agent
from aeradex_anruf.anruf import Feed
from aeradex_anruf.config import Config


def _cfg(**kw):
    return Config(llm_secret="s" * 32, public_url="https://x.ts.net", elevenlabs_key="k", elevenlabs_agent_id="a",
                  boss_name="Marco", assistant_name="Susi", **kw)


def test_persona_knows_boss_company_and_memory(tmp_path):
    b = make_book(tmp_path, firma="Muster GmbH")
    gedaechtnis.remember_note(b.root, "Marco will Offerten immer mit 30 Tagen Gültigkeit.")
    gedaechtnis.add_call(b.root, "Offerte O-2026-0002 für Beispiel Bau erstellt, Rückmeldung von Peter Beispiel offen.")
    prompt = make_agent(b.root, None, _cfg(), voice=True).system_prompt_text()
    assert "Susi, die Sekretärin von Marco bei der Muster GmbH" in prompt
    assert "30 Tagen Gültigkeit" in prompt and "Rückmeldung von Peter Beispiel offen" in prompt
    assert "Erfinde nichts" in prompt


def test_memory_tool_and_limits(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    assert gedaechtnis.remember("Bei Beispiel Bau immer Peter Beispiel anschreiben.")["ok"]
    gedaechtnis.remember("Bei Beispiel Bau immer Peter Beispiel anschreiben.")           # no duplicates
    for i in range(gedaechtnis.MAX_CALL_LINES + 5):
        gedaechtnis.add_call(b.root, f"Gespräch {i}")
    notes, calls = gedaechtnis._read(b.root)
    assert notes == ["Bei Beispiel Bau immer Peter Beispiel anschreiben."]
    assert len(calls) == gedaechtnis.MAX_CALL_LINES and calls[-1].endswith(f"Gespräch {gedaechtnis.MAX_CALL_LINES + 4}")
    assert len(gedaechtnis.for_prompt(b.root)) <= gedaechtnis.PROMPT_CHARS + 200
    assert "Gespräch 0" not in gedaechtnis.for_prompt(b.root)                        # oldest dropped first
    assert ".aeradex/lokal/" in str(gedaechtnis.path(b.root))                        # never part of the books


def test_greeting_by_time_of_day():
    cfg = _cfg()
    morning = time.mktime((2026, 10, 9, 8, 0, 0, 0, 0, -1))
    evening = time.mktime((2026, 10, 9, 20, 0, 0, 0, 0, -1))
    assert gateway.greeting(cfg, morning).startswith(("Guten Morgen Marco", "Morgen Marco"))
    assert "Marco" in gateway.greeting(cfg, evening) and "Morgen" not in gateway.greeting(cfg, evening)
    cfg.boss_name = ""
    assert "  " not in gateway.greeting(cfg, morning) and " !" not in gateway.greeting(cfg, morning)


def test_hang_up_writes_a_note(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    cfg = _cfg()
    monkeypatch.setattr(gateway.elevenlabs, "signed_url", lambda c: "wss://signed")
    feed = Feed()
    app = gateway.create_app(b.root, cfg, feed=feed, page_key="k1")

    def fake_ask(self, prompt, on_event=None):
        self.items.append({"type": "me", "text": prompt})
        if prompt.startswith("[Der Anruf ist beendet"):
            return "Rechnung R-2026-0002 an Beispiel Bau besprochen, Marco schickt sie morgen."
        if on_event:
            on_event({"type": "text", "raw": "Gern."})
        return "Gern."
    monkeypatch.setattr(RemoteAgent, "ask", fake_ask)
    from aeradex_anruf.agent import RemoteCodeAgent
    monkeypatch.setattr(RemoteCodeAgent, "warm_up", lambda self: None)          # no real Claude Code in tests
    monkeypatch.setattr(RemoteCodeAgent, "start", lambda self: None)
    with TestClient(app) as c:
        sess = c.post("/anruf/session", headers={"X-Aeradex-Key": "k1"}).json()
        assert sess["begruessung"]
        body = {"messages": [{"role": "system", "content": f"Sitzung: {sess['session']}"},
                             {"role": "user", "content": "Danke Susi."}]}
        c.post("/llm/v1", json=body, headers={"Authorization": f"Bearer {cfg.llm_secret}"})
        assert c.post("/anruf/ende", json={"key": "wrong", "session": sess["session"]}).status_code == 403
        assert c.post("/anruf/ende", json={"key": "k1", "session": sess["session"]}).status_code == 200
        for _ in range(50):
            if gedaechtnis._read(b.root)[1]:
                break
            time.sleep(0.05)
    calls = gedaechtnis._read(b.root)[1]
    assert calls and "R-2026-0002" in calls[-1]
    assert any(e["type"] == "hinweis" and "R-2026-0002" in e["text"] for e in feed.since(0, 0))

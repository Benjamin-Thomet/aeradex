"""The call page without Telegram: access key, session for the local person, feed, cards confirmed on the page."""
import hashlib
import hmac
import json
import subprocess
import os
import time

import pytest
from starlette.testclient import TestClient

from aeradex import api
from aeradex.book import Book
from aeradex.testing import make_book

from aeradex_anruf import actions, gateway
from aeradex_anruf.agent import RemoteAgent
from aeradex_anruf.anruf import Feed, PageChannel
from aeradex_anruf.config import Config

IBAN = "CH9300762011623852957"
KEY = "page-key-123"


@pytest.fixture
def book(tmp_path, monkeypatch):
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, iban=IBAN, git=True,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", plugins=["anruf"])
    api.customer_add(Book(b.root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    subprocess.run(["git", "config", "user.name", "Marco Muster"], cwd=b.root, check=True)
    subprocess.run(["git", "config", "user.email", "b@example.ch"], cwd=b.root, check=True)
    return Book(b.root)


@pytest.fixture
def cfg():
    # no Telegram at all: no bot token, no allowed users
    return Config(llm_secret="s" * 32, webhook_secret="whsec", public_url="https://x.ts.net",
                  elevenlabs_key="k", elevenlabs_agent_id="agent")


@pytest.fixture
def app(book, cfg, monkeypatch):
    monkeypatch.setattr(gateway.elevenlabs, "signed_url", lambda c: "wss://signed")
    feed = Feed()
    return gateway.create_app(book.root, cfg, feed=feed, page_key=KEY), feed


def test_config_for_calls_needs_no_telegram(cfg, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert cfg.backend == "claude-code" and cfg.problems_call() == []      # the subscription needs no API key
    assert any("TELEGRAM" in p for p in cfg.problems_telegram())
    cfg.backend = "api"
    assert any("ANTHROPIC_API_KEY" in p for p in cfg.problems_call())


def test_page_and_session_need_the_key(app, cfg):
    app, _ = app
    with TestClient(app) as c:
        assert c.get("/anruf").status_code == 403
        assert c.get("/anruf?key=wrong").status_code == 403
        assert c.get(f"/anruf?key={KEY}").status_code == 200
        assert c.post("/anruf/session").status_code == 403
        r = c.post("/anruf/session", headers={"X-Aeradex-Key": KEY})
        assert c.get("/app").status_code == 404                    # no Telegram Mini App in this mode
    assert r.json()["signed_url"] == "wss://signed"
    assert gateway.read_session(cfg, r.json()["session"], local=True)[0] == gateway.LOCAL_USER
    with pytest.raises(ValueError):                                # the local session is useless without the page
        gateway.read_session(cfg, r.json()["session"])



def test_ticket_opens_the_page_once_and_sets_a_cookie(app, monkeypatch):
    app, _ = app
    with TestClient(app, base_url="https://testserver") as c:
        assert c.post("/anruf/ticket").status_code == 403
        assert c.post("/anruf/ticket", headers={"X-Aeradex-Key": "wrong"}).status_code == 403
        ticket = c.post("/anruf/ticket", headers={"X-Aeradex-Key": KEY}).json()["ticket"]
        r = c.get(f"/anruf?ticket={ticket}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/anruf"
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=strict" in cookie.lower() and KEY not in cookie
        assert c.get("/anruf").status_code == 200                       # the cookie stands in for the key
        assert c.post("/anruf/session", headers={"X-Aeradex-Key": ""}).json()["signed_url"] == "wss://signed"
        assert c.post("/anruf/ende", json={"key": "", "session": "x"}).status_code == 403   # bad session, but past the key check
    with TestClient(app) as other:                                      # a ticket works only once
        assert other.get(f"/anruf?ticket={ticket}", follow_redirects=False).status_code == 403
        assert other.get("/anruf").status_code == 403
        old = other.post("/anruf/ticket", headers={"X-Aeradex-Key": KEY}).json()["ticket"]
        monkeypatch.setattr(gateway.time, "time", lambda: 10**12)          # expired
        assert other.get(f"/anruf?ticket={old}", follow_redirects=False).status_code == 403

def test_call_reports_to_the_page(app, cfg, monkeypatch):
    app, feed = app

    def fake_ask(self, prompt, on_event=None):
        assert isinstance(self.channel, PageChannel) and self.voice
        on_event({"type": "tool", "text": "invoices()"})
        on_event({"type": "text", "raw": "Zwei Rechnungen sind offen."})
        return ""
    monkeypatch.setattr(RemoteAgent, "ask", fake_ask)
    body = {"messages": [{"role": "system", "content": f"Sitzung: {gateway.make_session(cfg, gateway.LOCAL_USER)}"},
                         {"role": "user", "content": "Was ist offen?"}]}
    with TestClient(app) as c:
        r = c.post("/llm/v1", json=body, headers={"Authorization": f"Bearer {cfg.llm_secret}"})
    assert "Zwei Rechnungen" in r.text and r.text.rstrip().endswith("data: [DONE]")
    assert [e["type"] for e in feed.since(0, 0)] == ["tool"]


def test_card_confirmed_on_the_page_as_the_person(app, book):
    app, feed = app
    token = actions.CHANNEL.set(PageChannel(feed))
    try:
        res = actions.request_invoice("K0001", [{"text": "Malerarbeiten", "menge": 8, "einheit": "h", "preis": "95"}],
                                      "2026-03-02")
    finally:
        actions.CHANNEL.reset(token)
    card = feed.since(0, 0)[-1]
    assert card["type"] == "karte" and "760.00" in card["text"]
    with TestClient(app) as c:
        assert c.post("/anruf/aktion", json={"key": "wrong", "data": card["knoepfe"][0]["data"]}).status_code == 403
        assert api.invoice_list(Book(book.root)) == []
        assert c.post("/anruf/aktion", json={"key": KEY, "data": card["knoepfe"][0]["data"]}).status_code == 200
        assert c.post("/anruf/aktion", json={"key": KEY, "data": card["knoepfe"][0]["data"]}).status_code == 409
        pdf = next(e for e in feed.since(0, 0) if e["type"] == "datei")
        assert c.get(f"/anruf/datei/{pdf['id']}").status_code == 403
        got = c.get(f"/anruf/datei/{pdf['id']}?key={KEY}")
    assert got.status_code == 200 and got.content.startswith(b"%PDF")
    assert len(api.invoice_list(Book(book.root))) == 1
    author = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=book.root, capture_output=True, text=True).stdout
    assert author.strip() == "Marco Muster"


def test_webhook_summary_lands_on_the_page(app, cfg):
    app, feed = app
    event = {"type": "post_call_transcription", "data": {
        "analysis": {"transcript_summary": "Offerte erstellt."},
        "conversation_initiation_client_data": {"dynamic_variables": {"session": gateway.make_session(cfg, 0)}}}}
    raw = json.dumps(event).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    with TestClient(app) as c:
        assert c.post("/elevenlabs/webhook", content=raw, headers={"ElevenLabs-Signature": f"t={t},v0={sig}"}).status_code == 200
    note = feed.since(0, 0)[-1]
    assert note["type"] == "hinweis" and "Offerte erstellt" in note["text"]


def test_feed_waits_and_trims():
    feed = Feed()
    feed.MAX = 3
    for i in range(5):
        feed.push({"type": "text", "text": str(i)})
    assert [e["text"] for e in feed.since(0, 0)] == ["2", "3", "4"]
    assert feed.since(5, 0.05) == []


def test_slow_answer_gets_a_filler_first(app, cfg, monkeypatch):
    app, _ = app
    monkeypatch.setattr(gateway, "FIRST_WORDS", 0.2)
    monkeypatch.setattr(gateway, "STILL_WORKING", 0.4)

    def slow_ask(self, prompt, on_event=None):
        time.sleep(0.8)                          # longer than FIRST_WORDS + STILL_WORKING
        on_event({"type": "text", "raw": "Zwei Kunden."})
        return ""
    monkeypatch.setattr(RemoteAgent, "ask", slow_ask)
    body = {"messages": [{"role": "system", "content": f"Sitzung: {gateway.make_session(cfg, gateway.LOCAL_USER)}"},
                         {"role": "user", "content": "Kunden?"}]}
    with TestClient(app) as c:
        r = c.post("/llm/v1", json=body, headers={"Authorization": f"Bearer {cfg.llm_secret}"})
    said = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                   for line in r.text.splitlines() if line.startswith("data: {"))
    assert said.startswith(gateway.FILLER) and gateway.STILL_WORKING_LINES[0] in said and said.rstrip().endswith("Zwei Kunden.")


def test_long_web_search_keeps_talking(app, cfg, monkeypatch):
    app, _ = app
    monkeypatch.setattr(gateway, "FIRST_WORDS", 0.1)
    monkeypatch.setattr(gateway, "STILL_WORKING", 0.15)

    def searching(self, prompt, on_event=None):
        on_event({"type": "tool", "text": "WebSearch(query='Beispiel Bau Thun')"})
        on_event({"type": "tool", "text": "WebSearch(query='Beispiel Bau AG')"})       # announced only once
        time.sleep(0.8)
        on_event({"type": "text", "raw": "Gefunden."})
        return ""
    monkeypatch.setattr(RemoteAgent, "ask", searching)
    body = {"messages": [{"role": "system", "content": f"Sitzung: {gateway.make_session(cfg, gateway.LOCAL_USER)}"},
                         {"role": "user", "content": "Such Beispiel Bau."}]}
    with TestClient(app) as c:
        r = c.post("/llm/v1", json=body, headers={"Authorization": f"Bearer {cfg.llm_secret}"})
    said = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                   for line in r.text.splitlines() if line.startswith("data: {"))
    assert said.startswith("Ich schau kurz im Internet nach.") and said.count("im Internet") == 1
    assert sum(said.count(line) for line in gateway.STILL_WORKING_LINES) >= 3      # kept talking while it searched
    assert said.rstrip().endswith("Gefunden.")


FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write("start\n")
for line in sys.stdin:
    msg = json.loads(line)["message"]["content"]
    print(json.dumps({"type": "system", "session_id": "s1"}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Echo: " + msg.splitlines()[-1]}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "result": "ok", "session_id": "s1"}), flush=True)
"""


def test_claude_code_process_is_reused_across_turns(book, tmp_path, monkeypatch):
    from aeradex_anruf.agent import RemoteCodeAgent
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    log = tmp_path / "starts.log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    agent = RemoteCodeAgent(book.root, PageChannel(Feed()), voice=True)
    agent.start()                                            # warm start before the first sentence
    assert agent.ask("Hallo") == "Echo: Hallo"
    assert agent.ask("Kunden?") == "Echo: Kunden?"
    assert log.read_text().count("start") == 1               # one process for the whole call
    agent.close()
    assert agent.proc is None
    assert agent.ask("Wieder da?") == "Echo: Wieder da?"     # restarts (with --resume) after a crash/close
    assert log.read_text().count("start") == 2
    agent.close()


def test_only_the_newest_request_is_answered(app, cfg, monkeypatch):
    """ElevenLabs re-sends the growing sentence while the caller talks; older waiting requests are skipped."""
    app, _ = app
    asked = []

    def slow_ask(self, prompt, on_event=None):
        with self.lock:
            self.busy = True
        try:
            asked.append(prompt)
            time.sleep(0.6)
            on_event({"type": "text", "raw": f"Antwort auf {prompt}"})
        finally:
            self.busy = False
        return ""
    monkeypatch.setattr(RemoteAgent, "ask", slow_ask)
    session = gateway.make_session(cfg, gateway.LOCAL_USER)

    def body(text):
        return {"messages": [{"role": "system", "content": f"Sitzung: {session}"}, {"role": "user", "content": text}]}
    import threading as th
    results = {}
    with TestClient(app) as c:
        headers = {"Authorization": f"Bearer {cfg.llm_secret}"}
        def post(text, delay):
            time.sleep(delay)
            results[text] = c.post("/llm/v1", json=body(text), headers=headers).text
        threads = [th.Thread(target=post, args=(t, d)) for t, d in
                   [("eins", 0), ("eins zwei", 0.15), ("eins zwei drei", 0.3)]]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert asked == ["eins", "eins zwei drei"]                 # the middle one was superseded while waiting
    assert "Antwort auf eins zwei drei" in results["eins zwei drei"]
    assert "Antwort" not in results["eins zwei"]

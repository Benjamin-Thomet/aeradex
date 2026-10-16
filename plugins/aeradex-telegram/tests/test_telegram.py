import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from aeradex import api, plugins, tools
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from aeradex.web import chat

from aeradex_anruf import actions, gateway
from aeradex_anruf.agent import RemoteAgent
from aeradex_anruf.config import Config
from aeradex_telegram.bot import create_app
from aeradex_telegram.telegram import check_init_data

IBAN = "CH9300762011623852957"
TOKEN = "123456:TEST-token"
USER = 4242


@pytest.fixture
def book(tmp_path, monkeypatch):
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, iban=IBAN, git=True,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", plugins=["anruf", "telegram"])
    api.customer_add(Book(b.root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    return Book(b.root)


@pytest.fixture
def cfg():
    return Config(bot_token=TOKEN, allowed_users={USER}, llm_secret="s" * 32, webhook_secret="whsec",
                  public_url="https://tg.example.ts.net", elevenlabs_key="k", elevenlabs_agent_id="agent")


def test_installed():
    assert plugins.installed()["telegram"].ok


def _init_data(user_id=USER, auth_date=None, token=TOKEN):
    fields = {"auth_date": str(int(auth_date or time.time())), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Marco"})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data():
    assert check_init_data(_init_data(), TOKEN)["id"] == USER
    with pytest.raises(ValueError):
        check_init_data(_init_data().replace("Marco", "Mallory"), TOKEN)
    with pytest.raises(ValueError):
        check_init_data(_init_data(auth_date=time.time() - 3600), TOKEN)
    with pytest.raises(ValueError):
        check_init_data(_init_data(token="other:token"), TOKEN)


class FakeTG:
    def __init__(self, root):
        from aeradex_anruf.agent import Registry
        self.registry = Registry(root)
        self.bot = None
        self.notes = []

    def notify(self, uid, text):
        self.notes.append((uid, text))


def test_llm_endpoint_streams_and_requires_secret(book, cfg, monkeypatch):
    from starlette.testclient import TestClient

    seen = {}

    def fake_ask(self, prompt, on_event=None):
        seen["prompt"], seen["voice"] = prompt, self.voice
        on_event({"type": "tool", "text": "balance()"})
        on_event({"type": "text", "raw": "Auf der Bank sind **20000 Franken**."})
        return "ok"
    monkeypatch.setattr(RemoteAgent, "ask", fake_ask)
    tg = FakeTG(book.root)
    app = create_app(book.root, cfg, tg)
    body = {"model": "aeradex", "stream": True,
            "messages": [{"role": "system", "content": f"Sitzung: {gateway.make_session(cfg, USER)}"},
                         {"role": "user", "content": "Wie viel ist auf der Bank?"}]}
    with TestClient(app) as c:
        assert c.post("/llm/v1/chat/completions", json=body).status_code == 401
        assert c.post("/llm/v1/chat/completions", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
        bad = {**body, "messages": [{"role": "system", "content": "Sitzung: 1.2.3.4"}, body["messages"][1]]}
        assert c.post("/llm/v1/chat/completions", json=bad,
                      headers={"Authorization": f"Bearer {cfg.llm_secret}"}).status_code == 403
        r = c.post("/llm/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {cfg.llm_secret}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    chunks = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    assert chunks[-1] == "[DONE]"
    said = "".join(json.loads(c)["choices"][0]["delta"].get("content", "") for c in chunks[:-1])
    assert not said.startswith(gateway.FILLER)                  # fast answer: no filler
    assert "20000 Franken" in said and "**" not in said
    assert seen == {"prompt": "Wie viel ist auf der Bank?", "voice": True}


def test_app_session_checks_telegram_user(book, cfg, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(gateway.elevenlabs, "signed_url", lambda c: "wss://signed")
    app = create_app(book.root, cfg, FakeTG(book.root))
    with TestClient(app) as c:
        assert c.post("/app/session", json={"initData": "hash=abc"}).status_code == 401
        assert c.post("/app/session", json={"initData": _init_data(user_id=7)}).status_code == 403
        r = c.post("/app/session", json={"initData": _init_data()})
        assert c.get("/app").status_code == 200
    assert r.json()["signed_url"] == "wss://signed"
    assert gateway.read_session(cfg, r.json()["session"])[0] == USER


def test_webhook_posts_summary(book, cfg):
    from starlette.testclient import TestClient

    tg = FakeTG(book.root)
    app = create_app(book.root, cfg, tg)
    event = {"type": "post_call_transcription", "data": {
        "metadata": {"call_duration_secs": 75}, "analysis": {"transcript_summary": "Offerte für Kunde AG erstellt."},
        "conversation_initiation_client_data": {"dynamic_variables": {"session": gateway.make_session(cfg, USER)}}}}
    raw = json.dumps(event).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    with TestClient(app) as c:
        assert c.post("/elevenlabs/webhook", content=raw).status_code == 401
        assert c.post("/elevenlabs/webhook", content=raw, headers={"ElevenLabs-Signature": f"t={t},v0={sig}"}).status_code == 200
    assert tg.notes and tg.notes[0][0] == USER and "1:15" in tg.notes[0][1] and "Kunde AG" in tg.notes[0][1]


class FakeBot:
    def __init__(self):
        self.sent, self.docs, self.calls = [], [], []

    def send_text(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))

    def send_document(self, chat_id, path, caption=""):
        self.docs.append((chat_id, Path(path)))

    def answer_callback(self, *a, **k):
        pass

    def drop_buttons(self, *a, **k):
        pass

    def typing(self, chat_id):
        pass

    def call(self, method, **params):
        self.calls.append((method, params))


def _bot(book, cfg):
    from aeradex_telegram.bot import TelegramBot
    fake = FakeBot()
    return TelegramBot(book.root, cfg, bot=fake), fake


def test_bot_rejects_strangers(book, cfg, monkeypatch):
    tg, fake = _bot(book, cfg)
    monkeypatch.setattr(RemoteAgent, "ask", lambda *a, **k: pytest.fail("agent must not run"))
    msg = {"message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "Saldo?"}}
    tg.handle(msg)
    tg.handle(msg)
    assert len(fake.sent) == 1 and "Nicht berechtigt" in fake.sent[0][1]


def test_bot_text_goes_to_agent(book, cfg, monkeypatch):
    tg, fake = _bot(book, cfg)
    monkeypatch.setattr(RemoteAgent, "ask", lambda self, prompt, on_event=None: f"Antwort auf: {prompt}")
    tg.handle({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "text": "Saldo?"}})
    assert fake.sent[-1][1] == "Antwort auf: Saldo?"


def test_button_issues_invoice_as_human(book, cfg):
    tg, fake = _bot(book, cfg)
    res = actions.request_invoice("K0001", [{"text": "Montage", "menge": 1, "preis": "100"}], "2026-03-02")
    cb = {"id": "cb1", "from": {"id": 7}, "data": f"ok:{res['aktion']}",
          "message": {"message_id": 1, "chat": {"id": USER}}}
    tg.handle({"callback_query": cb})                                  # stranger: refused
    assert api.invoice_list(Book(book.root)) == []
    tg.handle({"callback_query": {**cb, "from": {"id": USER, "first_name": "Marco"}}})
    assert len(api.invoice_list(Book(book.root))) == 1
    assert fake.docs and fake.docs[0][1].suffix == ".pdf"
    tg.handle({"callback_query": {**cb, "from": {"id": USER, "first_name": "Marco"}}})   # tapped twice
    assert len(api.invoice_list(Book(book.root))) == 1 and "schon ausgeführt" in fake.sent[-1][1]


def test_bot_errors_never_contain_the_token():
    from aeradex_telegram.telegram import Bot, TelegramError, httpx

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
    bot = Bot(TOKEN, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(TelegramError) as err:
        bot.call("getMe")
    assert TOKEN not in str(err.value) and "<token>" in str(err.value)


def test_mini_app_warms_up_and_ends_only_own_call(book, cfg, monkeypatch):
    from starlette.testclient import TestClient
    from aeradex_anruf.anruf import Feed

    monkeypatch.setattr(gateway.elevenlabs, "signed_url", lambda c: "wss://signed")
    warmed, ended = [], []
    from aeradex_anruf.agent import RemoteCodeAgent
    monkeypatch.setattr(RemoteCodeAgent, "warm_up", lambda self: warmed.append(self))
    tg = FakeTG(book.root)
    app = create_app(book.root, cfg, tg, feed=Feed(), page_key="k1")
    with TestClient(app) as c:
        r = c.post("/app/session", json={"initData": _init_data()})
        session = r.json()["session"]
        assert r.json()["begruessung"]
        for _ in range(40):
            if warmed:
                break
            time.sleep(0.05)
        assert warmed                                                     # agent started before the first word
        app.state.calls.end = lambda s: ended.append(s) or True
        other = gateway.make_session(cfg, USER).replace(f"{USER}.", "999.", 1)
        assert c.post("/app/ende", json={"initData": _init_data(), "session": other}).status_code == 403
        assert c.post("/app/ende", json={"initData": _init_data(), "session": session}).json()["ok"]
        assert c.get("/anruf?key=k1").status_code == 200                  # browser call page in the same process
    assert ended == [session]


def test_message_during_warm_up_waits_instead_of_refusing(book, cfg, monkeypatch):
    import threading as th
    tg, fake = _bot(book, cfg)
    agent = tg.agent_for(USER)
    agent.busy = True                                         # warm-up still running
    th.Timer(0.5, lambda: setattr(agent, "busy", False)).start()
    monkeypatch.setattr(type(agent), "ask", lambda self, prompt, on_event=None: f"Hoi! ({prompt})")
    tg.handle({"message": {"from": {"id": USER}, "chat": {"id": USER, "type": "private"}, "text": "hallo"}})
    assert fake.sent[-1][1] == "Hoi! (hallo)"

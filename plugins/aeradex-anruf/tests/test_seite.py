"""The Nora page in aeradex: status, call button, cards, memory, settings."""
import os

import pytest

from aeradex import api
from aeradex.book import Book
from aeradex.testing import make_book

from aeradex_anruf import actions, dienst, gedaechtnis

IBAN = "CH9300762011623852957"


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    monkeypatch.setenv("AERADEX_ANRUF_ENV", str(tmp_path / "leer.env"))
    monkeypatch.setenv("AERADEX_ANRUF_NAME", "Nora")
    monkeypatch.setenv("AERADEX_ANRUF_CHEF", "Marco")
    b = make_book(tmp_path, iban=IBAN, git=True, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", plugins=["anruf"])
    api.customer_add(Book(b.root), name="Peter Beispiel", firma="Beispiel Bau AG", strasse="Werkstrasse", nr="10",
                     plz="3600", ort="Thun")
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    app = create_app(b.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        c.headers.update({"X-CSRF": app.state.ui.csrf})
        yield c, Book(b.root)


def post(c, url, data):
    r = c.post(url, headers={"HX-Request": "true"}, data=data)
    assert r.status_code == 204, r.text
    return r


def test_page_without_service(client):
    c, book = client
    assert 'href="/p/anruf/sekretaerin"' in c.get("/einstellungen").text              # Einstellungen › Agent & KI
    html = c.get("/p/anruf/sekretaerin").text
    assert "<h1>Telegram-Agent</h1>" in html and "<h1>Nora</h1>" not in html
    assert "Nora" in html and "pill ok\">läuft" not in html and "pill muted\">aus" in html and "telegram serve" in html
    assert "Noch keine Gespräche" in html


def test_page_with_service_cards_and_memory(client):
    c, book = client
    dienst.announce(book.root, "http://127.0.0.1:8765/anruf?key=abc", telegram=True, port=8765)
    assert oct(dienst.path(book.root).stat().st_mode & 0o777) == "0o600"
    gedaechtnis.add_call(book.root, "Offerte für Beispiel Bau besprochen.")
    res = actions.request_invoice("K0001", [{"text": "Montage", "menge": 1, "preis": "100"}], "2026-03-02")
    html = c.get("/p/anruf/sekretaerin").text
    assert "pill ok\">läuft" in html and 'hx-post="/p/anruf/sekretaerin/anrufen"' in html and "key=abc" not in html and "mit Telegram" in html
    assert "Offerte für Beispiel Bau besprochen." in html and "100.00" in html

    post(c, "/p/anruf/sekretaerin/notizen", {"notizen": "Offerten immer 30 Tage gültig\n\n- Bei Beispiel Bau Peter anschreiben"})
    assert gedaechtnis.entries(book.root)[0] == ["Offerten immer 30 Tage gültig", "Bei Beispiel Bau Peter anschreiben"]
    assert gedaechtnis.entries(book.root)[1]                                          # calls untouched

    post(c, "/p/anruf/sekretaerin/karte", {"data": f"ok:{res['aktion']}"})
    assert len(api.invoice_list(Book(book.root))) == 1 and not actions.pending(book.root)
    post(c, "/p/anruf/sekretaerin/verlauf_leeren", {})
    assert gedaechtnis.entries(book.root)[1] == []
    dienst.withdraw(book.root, os.getpid())
    assert dienst.status(book.root) is None


def test_stale_announcement_is_ignored(client):
    c, book = client
    dienst.path(book.root).write_text('{"pid": 999999999, "seit": 0, "anrufseite": "x", "telegram": false}')
    assert dienst.status(book.root) is None
    assert "pill ok\">läuft" not in c.get("/p/anruf/sekretaerin").text


def test_access_form_stores_outside_the_book_and_masks(client, tmp_path, monkeypatch):
    from aeradex_anruf import config
    env = tmp_path / "bot.env"
    monkeypatch.setattr(config, "ENV_FILE", env)
    c, book = client
    post(c, "/p/anruf/sekretaerin/zugaenge", {"telegram_token": "123456789:AAH" + "x" * 30, "telegram_ids": "111, 222",
                                               "elevenlabs_key": "", "voice_id": "", "name": "Susi", "chef": "Sandra",
                                               "backend": "openai"})
    values = config.read_env_file(env)
    assert values["TELEGRAM_BOT_TOKEN"].startswith("123456789:") and values["TELEGRAM_ALLOWED_USER_IDS"] == "111 222"
    assert values["AERADEX_ANRUF_BACKEND"] == "openai" and len(values["LLM_SHARED_SECRET"]) >= 24
    assert oct(env.stat().st_mode)[-3:] == "600" and not list(book.root.rglob("bot.env"))
    # empty secret field keeps the stored token; a bad token is refused
    post(c, "/p/anruf/sekretaerin/zugaenge", {"telegram_token": "", "telegram_ids": "111", "backend": "openai",
                                               "name": "Susi", "chef": "Sandra"})
    assert config.read_env_file(env)["TELEGRAM_BOT_TOKEN"].startswith("123456789:")
    r = c.post("/p/anruf/sekretaerin/zugaenge", headers={"HX-Request": "true"},
               data={"telegram_token": "nope", "backend": "openai"})
    assert r.status_code != 204
    monkeypatch.setenv("AERADEX_ANRUF_ENV", str(env))
    page = c.get("/p/anruf/sekretaerin").text
    assert "…" + "x" * 4 in page and "x" * 30 not in page                 # only the end of the token is shown


def test_anrufen_gets_a_ticket_and_goes_to_the_public_call_page(client, monkeypatch):
    c, book = client
    from aeradex_anruf import elevenlabs
    dienst.announce(book.root, "http://127.0.0.1:8765/anruf?key=abc", telegram=True, port=8765)
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "ticket": "T1"}

    def fake_post(url, headers, timeout):
        seen.update(url=url, key=headers["X-Aeradex-Key"])
        return Resp()
    monkeypatch.setattr(elevenlabs.httpx, "post", fake_post)
    r = post(c, "/p/anruf/sekretaerin/anrufen", {})
    assert seen == {"url": "http://127.0.0.1:8765/anruf/ticket", "key": "abc"}
    assert r.headers["HX-Redirect"] == "http://127.0.0.1:8765/anruf?ticket=T1"
    monkeypatch.setenv("PUBLIC_URL", "https://buch.example.ch/")
    assert post(c, "/p/anruf/sekretaerin/anrufen", {}).headers["HX-Redirect"] == "https://buch.example.ch/anruf?ticket=T1"

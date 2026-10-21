"""Phone view (/m): phones land there, every page renders with real data, writes go through the same api and the
same rights, scanned pages become one PDF and a draft, voice input goes through the plugin hook."""
from __future__ import annotations

import io
import json

import pytest

from aeradex import api, plugins, scan, sprache
from aeradex.book import Book, BookError
from aeradex.testing import make_book

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from aeradex.web.app import create_app  # noqa: E402

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/126.0 Mobile Safari/537.36"


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, iban="CH9300762011623852957", strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", git=True)
    root = b.root
    api.customer_add(b, name="Anna Meier", firma="Sonnenhof AG", strasse="Bahnhofstrasse", nr="12", plz="3011",
                     ort="Bern", telefon="031 123 45 67", kategorie="Verwaltung")
    api.customer_add(Book(root), name="Peter Keller", strasse="Lindenweg", nr="3", plz="3600", ort="Thun")
    api.invoice_create(Book(root), "K0001", [{"text": "Fassade", "menge": 1, "preis": "4800"}], "2026-01-10")
    api.invoice_create(Book(root), "K0002", [{"text": "Wohnzimmer", "menge": 1, "preis": "1250"}], "2026-01-12")
    api.invoice_create(Book(root), "K0001", [{"text": "Treppenhaus", "menge": 1, "preis": "2100"}], "2026-01-20")
    api.invoice_pay(Book(root), "R-2026-0003", None, "2026-01-30")
    api.reminder_create(Book(root), ["R-2026-0001"], "2026-02-20")
    api.customer_log_add(Book(root), "K0001", "Offerte Garage besprechen", "anruf", None, "2026-01-05")
    return Book(root)


def client(book, ua=IPHONE):
    c = TestClient(create_app(book.root, token="tok"), headers={"user-agent": ua})
    c.get("/?t=tok", follow_redirects=False)
    return c


def csrf(c, url="/m"):
    import re
    return re.search(r'"X-CSRF": "([^"]+)"', c.get(url).text).group(1)


def jpeg(w=600, h=800) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), (250, 250, 250)).save(out, "JPEG")
    return out.getvalue()


def test_phones_land_in_the_phone_view_unless_they_chose_the_desktop(book):
    with client(book) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/m"
        r = c.get("/m/ansicht?zu=desktop", follow_redirects=False)
        assert r.headers["location"] == "/" and "ansicht=desktop" in r.headers["set-cookie"]
        assert c.get("/", follow_redirects=False).status_code == 200
    with client(book, ANDROID) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/m"
    with client(book, "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0") as c:
        assert c.get("/", follow_redirects=False).status_code == 200
        assert "Handy-Ansicht" in c.get("/").text
        c.get("/m/ansicht?zu=handy")
        assert c.get("/", follow_redirects=False).headers["location"] == "/m"


def test_every_page_renders(book):
    with client(book) as c:
        heute = c.get("/m").text
        assert "Sonnenhof AG" in heute and "Überfällige Rechnungen" in heute and "Beleg scannen" in heute
        assert "Sonnenhof AG" in c.get("/m/kunden").text and "Peter Keller" in c.get("/m/kunden").text
        kunde = c.get("/m/kunden/K0001").text
        assert "tel:0311234567" in kunde and "maps.apple.com/?q=Bahnhofstrasse%2012%2C%203011%20Bern" in kunde
        assert "Offerte Garage besprechen" in kunde and "R-2026-0001" in kunde and "1. Mahnung" in kunde
        assert "Stammdaten bearbeiten" in kunde
        assert "google.com/maps" in client(book, ANDROID).get("/m/kunden/K0001").text
        assert c.get("/m/kunden/K9999").status_code == 404
        assert "Neuer Kunde" in c.get("/m/kunden/neu").text
        assert "R-2026-0003" in c.get("/m/rechnungen/R-2026-0003").text
        for page in ("/m/scannen", "/m/belege", "/m/agent", "/m/agent?kunde=K0001", "/m/rechnungen?status=entwurf"):
            assert c.get(page).status_code == 200, page
        assert 'data-ctx="Kunde Sonnenhof AG (K0001)"' in c.get("/m/agent?kunde=K0001").text


def test_invoice_filters(book):
    with client(book) as c:
        offen = c.get("/m/rechnungen").text
        assert "R-2026-0001" in offen and "R-2026-0002" in offen and "R-2026-0003" not in offen
        gemahnt = c.get("/m/rechnungen?status=gemahnt").text
        assert "R-2026-0001" in gemahnt and "R-2026-0002" not in gemahnt
        assert "R-2026-0002" in c.get("/m/rechnungen?status=ueberfaellig").text
        bezahlt = c.get("/m/rechnungen?status=bezahlt").text
        assert "R-2026-0003" in bezahlt and "R-2026-0001" not in bezahlt
        assert "R-2026-0003" not in c.get("/m/rechnungen?status=alle&jahr=2025").text


def test_customer_writes_stay_in_the_phone_view(book):
    with client(book) as c:
        h = {"X-CSRF": csrf(c), "HX-Request": "true"}
        r = c.post("/m/kunden/neu", data={"name": "Lea Muster", "ort": "Biel", "plz": "2502"}, headers=h)
        assert r.status_code == 204 and r.headers["HX-Redirect"] == "/m/kunden/K0003"
        r = c.post("/m/kunden/K0003/log", data={"text": "Will Offerte", "art": "anruf", "nachfassen": "2026-12-01"}, headers=h)
        assert r.headers["HX-Redirect"] == "/m/kunden/K0003"
        r = c.post("/m/kunden/K0003", data={"name": "Lea Muster", "ort": "Biel", "plz": "2502", "mobil": "079 1"}, headers=h)
        assert r.headers["HX-Redirect"] == "/m/kunden/K0003"
        assert "Will Offerte" in c.get("/m/kunden/K0003").text
    assert api.customer_dossier(Book(book.root), "K0003")["kunde"]["mobil"] == "079 1"


def test_pages_to_pdf():
    from pypdf import PdfReader
    pdf = scan.pages_to_pdf([jpeg(600, 800), jpeg(800, 600)])
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 2
    first, second = (p.mediabox for p in reader.pages)
    assert round(float(first.width)) == 595 and float(first.height) > float(first.width)
    assert float(second.height) < float(second.width)
    with pytest.raises(BookError):
        scan.pages_to_pdf([b"<svg/>"])
    with pytest.raises(BookError):
        scan.pages_to_pdf([])


def test_scanned_pages_become_a_draft(book):
    with client(book) as c:
        h = {"X-CSRF": csrf(c)}
        r = c.post("/m/scannen", data={"art": "quittung"}, headers=h,
                   files=[("seite", ("s1.jpg", jpeg(), "image/jpeg")), ("seite", ("s2.jpg", jpeg(), "image/jpeg"))])
        d = r.json()
        assert d["ok"] and d["url"] == f"/m/belege/{d['entwurf']}"
        page = c.get(d["url"]).text
        assert "Quittung" in page and "Beleg öffnen (PDF)" in page
        bad = c.post("/m/scannen", data={"art": "x"}, headers=h, files=[("seite", ("s.jpg", jpeg(), "image/jpeg"))])
        assert bad.status_code == 400 and not bad.json()["ok"]
        assert c.post("/m/scannen", data={}, headers=h).json()["fehler"] == "Nichts gescannt"
    from aeradex import erfassung
    draft = erfassung.draft(Book(book.root), d["entwurf"])
    assert draft["art"] == "quittung" and draft["datei"].startswith("inbox/scan-") and draft["datei"].endswith(".pdf")


def test_draft_actions_from_the_phone(book):
    with client(book) as c:
        h = {"X-CSRF": csrf(c), "HX-Request": "true"}
        d = c.post("/m/scannen", data={"art": "kreditor"}, headers={"X-CSRF": h["X-CSRF"]},
                   files=[("seite", ("s.jpg", jpeg(), "image/jpeg"))]).json()
        r = c.post(f"/m/belege/{d['entwurf']}/konto", data={"konto": "6500"}, headers=h)
        assert r.status_code == 204
        assert "6500" in c.get(d["url"]).text
        # not ready (no amount, no supplier): booking is refused and the page says why
        r = c.post(f"/m/belege/{d['entwurf']}/buchen", headers=h)
        assert "nicht bereit" in r.text
        r = c.post(f"/m/belege/{d['entwurf']}/verwerfen", headers=h)
        assert r.headers["HX-Redirect"] == "/m/belege"
        assert f'href="/m/belege/{d["entwurf"]}"' not in c.get("/m/belege").text


def test_read_only_users_see_but_cannot_write(book, tmp_path):
    from aeradex.web.auth import UserStore
    users = UserStore(tmp_path / "cfg")
    users.add("leser", "geheim-passwort-1", "lesen")
    app = create_app(book.root, auth_dir=tmp_path / "cfg")
    with TestClient(app, headers={"user-agent": IPHONE}) as c:
        c.post("/login", data={"name": "leser", "passwort": "geheim-passwort-1"})
        page = c.get("/m/kunden/K0001").text
        assert "Sonnenhof AG" in page and "/m/scannen" not in page and "Notieren" not in page
        token = csrf(c, "/m/kunden")
        r = c.post("/m/scannen", data={}, headers={"X-CSRF": token}, files=[("seite", ("s.jpg", jpeg(), "image/jpeg"))])
        assert r.status_code == 403
        r = c.post("/chat/diktat", headers={"X-CSRF": token}, files={"audio": ("a.webm", b"x", "audio/webm")})
        assert r.status_code == 403
        # the app manifest and the service worker are there before the login (the browser fetches them without cookies)
    with TestClient(app) as anon:
        m = anon.get("/manifest.webmanifest")
        assert m.status_code == 200 and json.loads(m.text)["start_url"] == "/m"
        assert anon.get("/sw.js").status_code == 200
        assert anon.get("/m", follow_redirects=False).status_code == 303


class _Voice:
    @plugins.hookimpl
    def aeradex_sprache_zu_text(self, book):
        return lambda audio, filename: f"Neuer Kunde Hans Muster ({len(audio)} Bytes, {filename})"


def test_voice_input_through_the_plugin_hook(book, monkeypatch):
    assert not sprache.available(book)
    with client(book) as c:
        token = csrf(c)
        r = c.post("/chat/diktat", headers={"X-CSRF": token}, files={"audio": ("sprache.webm", b"abc", "audio/webm")})
        assert r.status_code == 400 and "nicht eingerichtet" in r.json()["fehler"]
        assert 'id="chatMic"' not in c.get("/m/agent").text
    monkeypatch.setattr(plugins, "sprache_zu_text", lambda book: _Voice().aeradex_sprache_zu_text(book))
    assert sprache.available(book)
    with client(book) as c:
        token = csrf(c)
        assert 'id="chatMic"' in c.get("/m/agent").text and 'id="chatMic"' in c.get("/kreditoren").text
        r = c.post("/chat/diktat", headers={"X-CSRF": token}, files={"audio": ("sprache.m4a", b"abc", "audio/mp4")})
        assert r.json() == {"ok": True, "text": "Neuer Kunde Hans Muster (3 Bytes, sprache.m4a)"}
    with pytest.raises(sprache.SpeechError):
        sprache.transcribe(book, b"")

"""The letter layout of invoices and offers (`rechnung: {layout: brief}`): place and date, salutation from the
customer's Anrede, numbered positions with unit, «Ihre Offerte vom», payment terms with the bank, greeting with the
signer and the company in the footer; without the setting the compact layout stays as it was."""
from __future__ import annotations

import io

import pytest

from aeradex import api, invoices, pdf
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book

pypdf = pytest.importorskip("pypdf")


def _text(data: bytes) -> str:
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(data)).pages)


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, iban="CH9300762011623852957", strasse="Hauptstrasse", nr="1", plz="3600", ort="Thun",
                  git=True)
    api.customer_add(b, name="Anna Beispiel", firma="Muster AG", anrede="Frau", strasse="Seeweg", nr="2",
                     plz="3600", ort="Thun")
    api.customer_add(Book(b.root), name="Peter Keller", strasse="Lindenweg", nr="3", plz="3600", ort="Thun")
    return Book(b.root)


LETTER = {"layout": "brief", "ort": "Spiez", "einleitung": "Für die ausgeführten Arbeiten stellen wir Rechnung:",
          "dank": "Wir danken Ihnen für Ihren Auftrag.", "unterzeichner": "Eva Muster", "funktion": "Administration",
          "bank": "Muster Bank", "logo_hoehe_mm": "22"}


def test_compact_layout_stays_without_the_setting(book):
    r = api.invoice_create(book, "K0001", [{"text": "Fassade", "menge": 1, "preis": "4800"}], "2026-01-10")
    text = _text((book.root / r["pdf"]).read_bytes())
    assert "Rechnungsdatum" in text and "Freundliche Grüsse" not in text and "Frau Anna" not in text


def test_letter_layout(book):
    api.settings_update(book, web="www.muster.ch", rechnung=LETTER)
    assert Book(book.root).settings.get("rechnung")["logo_hoehe_mm"] == 22
    r = api.invoice_create(Book(book.root), "K0001",
                           [{"text": "Wände streichen", "menge": "42", "einheit": "m²", "preis": "18.50"},
                            {"text": "Abdecken", "menge": 1, "einheit": "pauschal", "preis": "180"}],
                           "2026-03-10", leistung_von="2026-03-02", leistung_bis="2026-03-06", rabatt="5",
                           objekt="MFH Seeweg 2, 3600 Thun", offerte={"nummer": "O-2026-0003", "datum": "2026-02-01"})
    assert r["rechnung"]["offerte"] == {"nummer": "O-2026-0003", "datum": "2026-02-01"}
    text = _text((book.root / r["pdf"]).read_bytes())
    for part in ("Spiez, 10.03.2026", "Frau Anna Beispiel", "Rechnungs-Nr.", "Objekt / Baustelle",
                 "02.03.2026 bis 06.03.2026", "Ihre Offerte vom", "01.02.2026", "Sehr geehrte Frau Beispiel",
                 "Für die ausgeführten Arbeiten", "Pos.", "Einheit", "pauschal", "Zwischentotal", "Rabatt 5 %",
                 "Zahlbar innert 30 Tagen netto.", "Muster Bank", "CH93 0076 2011 6238 5295 7",
                 "Freundliche Grüsse", "Eva Muster", "Administration", "www.muster.ch"):
        assert part in text, part
    assert_clean(Book(book.root))          # the offer reference is part of the frozen invoice


def test_salutation_without_anrede_and_private_customer(book):
    assert pdf.salutation({"name": "Peter Keller"}) == "Sehr geehrte Damen und Herren"
    assert pdf.salutation({"name": "Keller", "anrede": "Familie"}) == "Sehr geehrte Familie Keller"
    assert pdf.brief_recipient({"name": "Peter Keller", "anrede": "Herr", "strasse": "Lindenweg", "nr": "3",
                                "plz": "3600", "ort": "Thun"}, {"layout": "brief"})[0] == "Herr Peter Keller"
    with pytest.raises(BookError):
        api.customer_update(book, "K0002", anrede="Dr.")
    api.customer_update(Book(book.root), "K0002", anrede="Herr")
    assert invoices.customer(Book(book.root), "K0002")["anrede"] == "Herr"


def test_layout_settings_are_checked_and_can_be_reset(book):
    with pytest.raises(BookError):
        api.settings_update(book, rechnung={"layout": "bunt"})
    with pytest.raises(BookError):
        api.settings_update(Book(book.root), rechnung={"logo_hoehe_mm": "200"})
    with pytest.raises(BookError):
        api.settings_update(Book(book.root), rechnung={"farbe": "rot"})
    api.settings_update(Book(book.root), rechnung=LETTER)
    api.settings_update(Book(book.root), rechnung={"layout": "kompakt", "bank": ""})
    assert "layout" not in Book(book.root).settings.get("rechnung") and "bank" not in Book(book.root).settings.get("rechnung")


def test_sample_invoice_and_settings_form(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert "Rechnung und Offerte" in c.get("/einstellungen").text
        r = c.post("/einstellungen/rechnung", data={**LETTER}, headers=h)
        assert r.status_code == 204, r.text
        sample = c.get("/einstellungen/rechnung.pdf")
        assert sample.headers["content-type"] == "application/pdf"
        assert "Sehr geehrte Frau Beispiel" in _text(sample.content)
        r = c.post("/debitoren/kunden/K0002", data={"name": "Peter Keller", "anrede": "Herr", "rechnung_an": "person"},
                   headers=h)
        assert r.status_code == 204, r.text
    assert invoices.customer(Book(book.root), "K0002")["anrede"] == "Herr"
    assert not list(book.root.glob("rechnungen/**/*.md"))           # the sample writes nothing

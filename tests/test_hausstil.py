"""House style of the documents a customer gets: a typeface bundled with aeradex, the company's colours and the
customer number left off — each set per book under `rechnung:`; without them the documents stay as they were."""
from __future__ import annotations

import io

import pytest

from aeradex import api, pdf
from aeradex.book import Book, BookError
from aeradex.testing import make_book

pypdf = pytest.importorskip("pypdf")


def _read(data: bytes) -> tuple[str, set[str]]:
    reader = pypdf.PdfReader(io.BytesIO(data))
    fonts = set()
    for page in reader.pages:
        for f in (page["/Resources"].get("/Font") or {}).values():
            fonts.add(str(f.get_object()["/BaseFont"]).split("+")[-1])
    return "\n".join(p.extract_text() for p in reader.pages), fonts


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, iban="CH9300762011623852957", strasse="Hauptstrasse", nr="1", plz="4053", ort="Basel",
                  git=True)
    api.customer_add(b, name="Anna Beispiel", firma="Muster AG", strasse="Seeweg", nr="2", plz="3600", ort="Thun")
    return Book(b.root)


STIL = {"schrift": "Jost", "farbe_text": "#1C2B24", "farbe_grau": "#4A5A52", "farbe_linie": "#D7C8B4",
        "farbe_flaeche": "#E3E7E0", "kundennummer": "nein"}


def _invoice(book):
    r = api.invoice_create(Book(book.root), "K0001", [{"text": "Buchhaltung 2026", "menge": 1, "preis": "800"}],
                           "2026-11-19")
    return _read((book.root / r["pdf"]).read_bytes())


def test_without_settings_nothing_changes(book):
    text, fonts = _invoice(book)
    assert "Kundennummer" in text and "K0001" in text
    assert not any(f.startswith("Jost") for f in fonts)


def test_house_style_and_no_customer_number(book):
    api.settings_update(book, rechnung=STIL)
    cfg = Book(book.root).settings.get("rechnung")
    assert cfg["schrift"] == "jost" and "kundennummer" in cfg
    text, fonts = _invoice(book)
    assert "Kundennummer" not in text and "Buchhaltung 2026" in text
    assert {"Jost-Regular", "Jost-Medium"} <= fonts 
    # the module defaults are back for the next book (e.g. another company in the same process)
    assert pdf.FONT == "Helvetica" and pdf.ST["base"].fontName == "Helvetica"
    assert pdf.INK.hexval() == "0x1c1917"


def test_letter_layout_in_house_style(book):
    api.settings_update(book, rechnung={**STIL, "layout": "brief", "unterzeichner": "Benjamin Thomet"})
    text, fonts = _invoice(book)
    assert "Freundliche Grüsse" in text and "Kundennummer" not in text and "Jost-Regular" in fonts


def test_settings_are_checked(book):
    for bad in ({"schrift": "Comic Sans"}, {"farbe_text": "grün"}, {"kundennummer": "vielleicht"}):
        with pytest.raises(BookError):
            api.settings_update(Book(book.root), rechnung=bad)
    api.settings_update(Book(book.root), rechnung=STIL)
    api.settings_update(Book(book.root), rechnung={"schrift": "", "kundennummer": "ja"})
    cfg = Book(book.root).settings.get("rechnung")
    assert "schrift" not in cfg and "kundennummer" not in cfg and cfg["farbe_text"] == "#1C2B24"

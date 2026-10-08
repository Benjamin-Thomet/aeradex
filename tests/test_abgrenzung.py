"""Service date/period (Leistung) on documents and in booking texts, and the year-end Rechnungsabgrenzung."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import abgrenzung, api, check, erfassung, kreditoren, leistung, pdf
from aeradex.book import Book, BookError

D = Decimal


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Weg", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7", uid="CHE-123.456.789 MWST")
    api.customer_add(Book(root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    api.supplier_add(Book(root), name="Hostpunkt GmbH", iban="CH7409000000300012345", konto="6570")
    return Book(root)


def errors(book: Book) -> list[str]:
    return [str(i) for i in check.run(Book(book.root)) if i.level == "fehler"]


def saldo(book: Book, konto: str, year: int) -> Decimal:
    b = Book(book.root)
    return sum((r.betrag if r.soll == konto else -r.betrag for r in b.rows
                if r.datum.year == year and konto in (r.soll, r.haben)), D(0))


# ---- reading and writing the period ----

@pytest.mark.parametrize("text, expected", [
    ("Leistungszeitraum: 01.10.2026 – 30.09.2027", (date(2026, 10, 1), date(2027, 9, 30))),
    ("Abo Oktober – Dezember 2026", (date(2026, 10, 1), date(2026, 12, 31))),
    ("Hosting 01.11.–31.01.2027", (date(2026, 11, 1), date(2027, 1, 31))),
    ("Lieferdatum 15.03.2026", (date(2026, 3, 15), None)),
    ("Leistungsperiode\n10/2026 - 09/2027", (date(2026, 10, 1), date(2027, 9, 30))),
    ("Abrechnungsperiode: September 2026", (date(2026, 9, 1), date(2026, 9, 30))),
    ("Période: 1.1.2027 au 31.12.2027", (date(2027, 1, 1), date(2027, 12, 31))),
    ("Rechnungsdatum 01.10.2026\nZahlbar bis 31.10.2026", None),
])
def test_period_is_read_from_document_text(text, expected):
    assert leistung.parse(text, 2026) == expected


def test_label_round_trips_through_the_booking_text():
    text = leistung.with_text("Kreditor E-2026-0001 – Hostpunkt GmbH", "2026-10-01", "2027-09-30")
    assert text.endswith(" · Leistung 01.10.2026–30.09.2027")
    assert leistung.with_text(text, "2026-10-01", "2027-09-30") == text            # never twice
    assert leistung.from_text(text) == (date(2026, 10, 1), date(2027, 9, 30))
    assert leistung.from_text("x · Leistung 15.03.2026") == (date(2026, 3, 15), date(2026, 3, 15))
    with pytest.raises(BookError, match="vor «von»"):
        leistung.normalize("2026-10-01", "2026-09-01")


def test_draft_reader_takes_the_period():
    out = erfassung.parse_text("Hostpunkt GmbH\nRechnungsdatum 01.10.2026\nLeistungszeitraum 01.10.2026 - 30.09.2027\n"
                               "Total CHF 1200.00")
    assert out["leistung_von"] == "2026-10-01" and out["leistung_bis"] == "2027-09-30"


# ---- documents ----

def test_supplier_bill_carries_the_period_in_its_rows_and_fingerprint(book):
    nr = api.bill_add(book, "L0001", "1200.00", datum="2026-10-01", rechnungsnr="H-1",
                      leistung_von="2026-10-01", leistung_bis="2027-09-30")["kreditor"]["nummer"]
    meta = kreditoren.bill(Book(book.root), nr)
    assert meta["leistung_von"] == "2026-10-01" and kreditoren.bill_fingerprint_ok(meta)
    rows = [r for r in Book(book.root).rows if r.quelle == f"kreditor:{nr}"]
    assert all(r.text.endswith("· Leistung 01.10.2026–30.09.2027") for r in rows)
    old = api.bill_add(Book(book.root), "L0001", "50.00", datum="2026-10-02")["kreditor"]["nummer"]
    assert "Leistung" not in next(r.text for r in Book(book.root).rows if r.quelle == f"kreditor:{old}")
    assert "leistung_von" not in kreditoren.bill(Book(book.root), old)
    assert errors(book) == []


def test_own_invoice_prints_the_period_and_books_it(book):
    from pypdf import PdfReader
    res = api.invoice_create(book, "K0001", [{"text": "Wartung", "menge": 1, "preis": "600"}], "2026-12-15",
                             leistung_von="2026-12-01", leistung_bis="2026-12-31")
    meta = res["rechnung"]
    text = "".join(p.extract_text() for p in PdfReader(book.root / res["pdf"]).pages)
    assert "Leistungszeitraum" in text and "01.12.2026 – 31.12.2026" in text
    assert any("Leistung 01.12.2026–31.12.2026" in r.text for r in Book(book.root).rows if r.beleg == meta["nummer"])
    single = api.invoice_create(Book(book.root), "K0001", [{"text": "Montage", "menge": 1, "preis": "100"}],
                                "2026-12-15", leistung_von="2026-12-03")
    text = "".join(p.extract_text() for p in PdfReader(book.root / single["pdf"]).pages)
    assert "Leistungsdatum" in text and "03.12.2026" in text
    assert errors(book) == []


# ---- year-end Abgrenzung ----

def test_prepaid_annual_subscription_is_deferred_and_reversed(book):
    api.post_entry(book, "2026-10-01", "6570", "1020", "1200.00",
                   leistung.with_text("Hosting Jahresabo", "2026-10-01", "2027-09-30"))
    (p,) = api.accruals(Book(book.root), 2026)["vorschlaege"]
    assert p["art"] == "aufwand_voraus" and p["soll"] == "1300" and p["haben"] == "6570"
    assert D(p["betrag"]) == (D("1200") * 273 / 365).quantize(D("0.01")) == D("897.53")
    assert p["tage_anteil"] == 273 and p["tage"] == 365

    res = api.accruals_book(Book(book.root), 2026, [p["id"]])
    assert saldo(book, "6570", 2026) == D("1200") - D("897.53")
    assert saldo(book, "1300", 2026) == D("897.53")
    assert saldo(book, "6570", 2027) == D("897.53")            # reversal moves the expense into 2027
    reversal = [r for r in Book(book.root).rows if r.beleg == res["abgrenzung"]["aufloesung"]]
    assert reversal[0].datum == date(2027, 1, 1)
    again = api.accruals(Book(book.root), 2026)
    assert again["offen"] == 0 and again["vorschlaege"][0]["gebucht"] == res["abgrenzung"]["beleg"]
    assert api.accruals(Book(book.root), 2027)["vorschlaege"] == []   # own rows carry no service label
    with pytest.raises(BookError, match="Bereits abgegrenzt"):
        api.accruals_book(Book(book.root), 2026, [p["id"]])
    assert errors(book) == []


def test_income_invoiced_next_year_for_this_year_is_accrued_net_of_vat(book):
    api.settings_update(book, mwst={"methode": "effektiv"})
    api.invoice_create(Book(book.root), "K0001", [{"text": "Dezember", "menge": 1, "preis": "1000", "mwst": "U81"}],
                       "2027-01-10", leistung_von="2026-12-01", leistung_bis="2026-12-31")
    (p,) = api.accruals(Book(book.root), 2026)["vorschlaege"]
    assert p["art"] == "ertrag_ausstehend" and (p["soll"], p["haben"]) == ("1301", "3400")
    assert D(p["betrag"]) == D("1000.00")                     # the net, VAT stays with the invoice


def test_supplier_bill_next_year_for_december_is_accrued(book):
    api.bill_add(book, "L0001", "310.00", datum="2027-01-05", leistung_von="2026-12-01", leistung_bis="2027-01-31")
    (p,) = api.accruals(Book(book.root), 2026)["vorschlaege"]
    assert p["art"] == "aufwand_ausstehend" and (p["soll"], p["haben"]) == ("6570", "2300")
    assert D(p["betrag"]) == D("155.00")                      # 31 of 62 days


def test_nothing_chosen_or_locked_next_year_is_refused(book):
    api.post_entry(book, "2026-10-01", "6570", "1020", "1200.00",
                   leistung.with_text("Abo", "2026-10-01", "2027-09-30"))
    with pytest.raises(BookError, match="Keine Abgrenzung"):
        api.accruals_book(Book(book.root), 2026, [])
    assert abgrenzung.open_count(Book(book.root), 2026) == 1

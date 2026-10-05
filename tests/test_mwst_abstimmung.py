"""MWST: vereinnahmte Entgelte, year-end Umsatzabstimmung and the Abgrenzung of open items."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, check, mwst
from allkvitt.book import Book, BookError
from allkvitt.ledger import BalanceEngine

D = Decimal


def make_book(tmp_path: Path, art: str) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7", uid="CHE-123.456.789 MWST")
    api.settings_update(Book(root), mwst={"methode": "effektiv", "abrechnungsart": art})
    api.customer_add(Book(root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    api.supplier_add(Book(root), name="Papeterie AG", strasse="Marktgasse", nr="1", plz="3011", ort="Bern",
                     konto="6500", mwst="V81", iban="CH56 0483 5012 3456 7800 9")
    return Book(root)


def errors(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level != "hinweis"]


def at(root: Path, nr: str, when: str) -> Decimal:
    return BalanceEngine(Book(root)).balance_at(nr, date.fromisoformat(when))


def test_vereinnahmt_counts_on_payment(tmp_path):
    book = make_book(tmp_path, "vereinnahmt")
    root = book.root
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 10, "preis": "100"}], "2026-02-01")
    api.invoice_pay(Book(root), "R-2026-0001", "540.50", "2026-03-15")
    api.invoice_pay(Book(root), "R-2026-0001", "540.50", "2026-04-10")
    bill = api.bill_add(Book(root), "L0001", "108.10", datum="2026-03-03")["kreditor"]["nummer"]
    api.bill_pay(Book(root), bill, "2026-04-05")
    q1 = mwst.report(Book(root), "2026-Q1")["ziffern"]
    q2 = mwst.report(Book(root), "2026-Q2")["ziffern"]
    assert (q1["303"], q1["303_steuer"], q1["400"]) == (D("500.00"), D("40.50"), D("0"))
    assert (q2["303"], q2["303_steuer"], q2["400"]) == (D("500.00"), D("40.50"), D("8.10"))
    # Booking Q1 clears only the declared part; the rest waits on 2200 for the payment.
    api.mwst_book(Book(root), "2026-Q1")
    assert at(root, "2200", "2026-03-31") == D("-40.50")
    api.mwst_book(Book(root), "2026-Q2")
    assert at(root, "2200", "2026-06-30") == 0 and at(root, "1170", "2026-06-30") == 0
    assert errors(root) == []
    # The same book on vereinbarte Entgelte would have declared everything in Q1.
    api.settings_update(Book(root), mwst={"methode": "effektiv", "abrechnungsart": "vereinbart"})
    assert mwst.report(Book(root), "2026-Q1")["veraendert"]


def test_umsatzabstimmung_vereinnahmt_with_abgrenzung(tmp_path):
    book = make_book(tmp_path, "vereinnahmt")
    root = book.root
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "1000"}], "2026-05-04")
    api.invoice_pay(Book(root), "R-2026-0001", None, "2026-05-30")
    api.invoice_create(Book(root), "K0001", [{"text": "Projekt", "menge": 1, "preis": "2000"}], "2026-12-01")
    bill = api.bill_add(Book(root), "L0001", "54.05", datum="2026-12-10")["kreditor"]["nummer"]
    for q in ("2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"):
        api.mwst_book(Book(root), q)

    rep = mwst.abstimmung(Book(root), 2026)
    z = {r["ziffer"]: r for r in rep["zeilen"]}
    assert z["200"]["buchhaltung"] == D("3000.00") and z["200"]["offen_ende"] == D("2000.00")
    assert z["200"]["soll"] == z["200"]["deklariert"] == D("1000.00") and z["200"]["differenz"] == 0
    assert z["400"]["buchhaltung"] == D("4.05") and z["400"]["soll"] == 0
    konten = {k["konto"]: k for k in rep["steuerkonten"]}
    assert konten["2200"]["saldo"] == konten["2200"]["erwartet"] == D("-162.00")
    assert konten["1170"]["saldo"] == konten["1170"]["erwartet"] == D("4.05")
    assert rep["ok"] and any("abgrenzen" in h for h in rep["hinweise"])
    assert [b["nummer"] for b in rep["offen"]["ende"]["belege"]] == [bill, "R-2026-0002"]

    api.mwst_abgrenzung(Book(root), 2026)
    assert at(root, "2200", "2026-12-31") == 0 and at(root, "2209", "2026-12-31") == D("-162.00")
    assert at(root, "1170", "2026-12-31") == 0 and at(root, "1172", "2026-12-31") == D("4.05")
    assert at(root, "2200", "2027-01-01") == D("-162.00") and at(root, "2209", "2027-01-01") == 0
    assert errors(root) == []
    assert mwst.abstimmung(Book(root), 2026)["ok"]          # the Abgrenzung does not disturb the Abstimmung
    from io import BytesIO
    from pypdf import PdfReader
    exported = api.mwst_abstimmung(Book(root), 2026, als_pdf=True)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(Path(exported["pdf"]).read_bytes())).pages)
    assert "MWST-Umsatzabstimmung 2026" in text and "2'000.00" in text
    assert bill in text and "R-2026-0002" in text and "28.08.2027" in text
    with pytest.raises(BookError):
        api.mwst_abgrenzung(Book(root), 2026)

    # Paid in the new year: owed then, and the reversed Abgrenzung is what the Abrechnung clears.
    api.invoice_pay(Book(root), "R-2026-0002", None, "2027-01-20")
    api.bill_pay(Book(root), bill, "2027-01-25")
    q = mwst.report(Book(root), "2027-Q1")["ziffern"]
    assert (q["303"], q["303_steuer"], q["400"]) == (D("2000.00"), D("162.00"), D("4.05"))
    api.mwst_book(Book(root), "2027-Q1")
    assert at(root, "2200", "2027-03-31") == 0 and at(root, "1170", "2027-03-31") == 0
    assert errors(root) == []


def test_abstimmung_finds_differences_and_uncoded_revenue(tmp_path):
    book = make_book(tmp_path, "vereinbart")
    root = book.root
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "1000"}], "2026-02-01")
    for q in ("2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"):
        api.mwst_book(Book(root), q)
    rep = mwst.abstimmung(Book(root), 2026)
    assert rep["ok"] and rep["offen"] is None
    assert rep["ertrag"]["mit_code"] == D("1000.00") and rep["ertrag"]["ohne_code"] == 0

    # Forgotten afterwards in Q1, and revenue without a code.
    api.post_entry(Book(root), "2026-03-20", "1020", "3400", "540.50", "Barverkauf", mwst="U81")
    api.post_entry(Book(root), "2026-06-01", "1020", "3400", "100.00", "Bar ohne Code")
    rep = mwst.abstimmung(Book(root), 2026)
    z = {r["ziffer"]: r for r in rep["zeilen"]}
    assert z["200"]["soll"] == D("1500.00") and z["200"]["deklariert"] == D("1000.00")
    assert z["200"]["differenz"] == D("500.00") and z["303_steuer"]["differenz"] == D("40.50")
    konten = {k["konto"]: k for k in rep["steuerkonten"]}
    assert konten["2200"]["differenz"] == D("-40.50")      # tax booked but never declared
    assert rep["ertrag"]["ohne_code"] == D("100.00")
    assert not rep["ok"]
    assert any("Berichtigungsabrechnung" in h and "28.08.2027" in h for h in rep["hinweise"])
    assert any("ohne MWST-Code" in h for h in rep["hinweise"])
    with pytest.raises(BookError):
        api.mwst_abgrenzung(Book(root), 2026)                # only for vereinnahmte Entgelte


def test_cash_credit_and_opening_items(tmp_path):
    root = make_book(tmp_path, "vereinnahmt").root
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "1000"}], "2026-12-01")
    api.invoice_credit(Book(root), "R-2026-0001", "540.50", "2027-01-10")
    api.invoice_pay(Book(root), "R-2026-0001", None, "2027-02-10")
    rep = mwst.abstimmung(Book(root), 2027)
    z = {row["ziffer"]: row for row in rep["zeilen"]}
    assert z["200"]["buchhaltung"] == D("-500.00")
    assert z["200"]["offen_anfang"] == D("1000.00")
    assert z["200"]["offen_ende"] == 0
    assert z["200"]["soll"] == D("500.00")
    assert mwst.report(Book(root), "2027-Q1")["ziffern"]["303_steuer"] == D("40.50")


def test_saldo_cash_reconciliation_pdf(tmp_path):
    from io import BytesIO
    from pypdf import PdfReader

    root = make_book(tmp_path, "vereinnahmt").root
    api.settings_update(Book(root), mwst={"methode": "saldo", "periode": "semester", "saldosteuersatz": "6.2"})
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "1000"}], "2026-02-01")
    api.invoice_pay(Book(root), "R-2026-0001", None, "2026-08-01")
    for period in ("2026-S1", "2026-S2"):
        api.mwst_book(Book(root), period)
    rep = api.mwst_abstimmung(Book(root), 2026, pdf_out=str(tmp_path / "abstimmung.pdf"))
    assert rep["ok"] and rep["methode"] == "saldo"
    z = {row["ziffer"]: row for row in rep["zeilen"]}
    assert D(z["322"]["deklariert"]) == D("1081.00")
    assert D(z["322_steuer"]["deklariert"]) == D("67.02")
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(Path(rep["pdf"]).read_bytes())).pages)
    assert "Saldosteuer" in text and "67.02" in text

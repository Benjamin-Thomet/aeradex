"""MWST: automatic split, invoices, credit notes and the Abrechnung — both methods."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from batzen import api, check, mwst
from batzen.book import Book, BookError

D = Decimal


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7", uid="CHE-123.456.789 MWST")
    b = Book(root)
    api.settings_update(b, mwst={"methode": "effektiv"})
    api.customer_add(Book(root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    return Book(root)


def errors(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level != "hinweis"]


def balance(book_root: Path, nr: str) -> Decimal:
    from batzen.ledger import BalanceEngine
    return BalanceEngine(Book(book_root)).balance(nr, 2026)


def test_expense_with_vorsteuer_is_split(book):
    api.post_entry(book, "2026-01-10", "6500", "1020", "108.10", "Büromaterial", mwst="V81")
    rows = Book(book.root).rows
    assert [(r.soll, r.haben, r.betrag, r.mwst) for r in rows] == [
        ("6500", "", D("100.00"), "V81"), ("1170", "", D("8.10"), "V81"), ("", "1020", D("108.10"), "")]
    rep = mwst.report(Book(book.root), "2026-Q1")
    assert rep["ziffern"]["400"] == D("8.10") and rep["zahllast"] == D("-8.10")
    assert errors(book.root) == []


def test_invoice_with_mwst_and_abrechnung(book):
    res = api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 10, "preis": "100"}], "2026-02-01")
    inv = res["rechnung"]
    assert inv["netto"] == "1000.00" and inv["total"] == "1081.00" and inv["mwst"][0]["steuer"] == "81.00"
    api.post_entry(Book(book.root), "2026-02-05", "6570", "1020", "54.05", "Software", mwst="I81")
    rep = mwst.report(Book(book.root), "2026-Q1")
    z = rep["ziffern"]
    assert (z["200"], z["299"], z["303"], z["303_steuer"]) == (D("1000.00"), D("1000.00"), D("1000.00"), D("81.00"))
    assert z["405"] == D("4.05") and z["500"] == D("76.95")
    api.mwst_book(Book(book.root), "2026-Q1")
    assert balance(book.root, "2200") == 0 and balance(book.root, "1171") == 0
    assert balance(book.root, "2201") == D("-76.95")              # Zahllast owed to the ESTV
    assert errors(book.root) == []
    with pytest.raises(BookError):
        api.mwst_book(Book(book.root), "2026-Q1")
    # A later booking in the closed period is flagged for a correction.
    api.post_entry(Book(book.root), "2026-03-01", "6500", "1020", "10.81", "Nachtrag", mwst="V81")
    assert any("Korrekturabrechnung" in e for e in errors(book.root))


def test_credit_note_reduces_revenue_and_tax(book):
    api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "1000"}], "2026-02-01")
    api.invoice_credit(Book(book.root), "R-2026-0001", "108.10", "2026-02-10", grund="Kulanz")
    z = mwst.report(Book(book.root), "2026-Q1")["ziffern"]
    assert z["303"] == D("900.00") and z["303_steuer"] == D("72.90")
    assert api.invoice_list(Book(book.root))[0]["offen"] == "972.90"
    assert errors(book.root) == []


def test_saldo_method(book):
    api.settings_update(Book(book.root), mwst={"methode": "saldo", "saldosteuersatz": "6.2"})
    res = api.invoice_create(Book(book.root), "K0001", [{"text": "Malerarbeiten", "menge": 1, "preis": "1000"}],
                             "2026-03-01")
    assert res["rechnung"]["total"] == "1081.00"
    assert balance(book.root, "3400") == D("-1081.00")           # revenue booked gross
    with pytest.raises(BookError):
        api.post_entry(Book(book.root), "2026-03-02", "6500", "1020", "10.81", "x", mwst="V81")
    rep = mwst.report(Book(book.root), "2026-S1")
    assert rep["ziffern"]["322"] == D("1081.00") and rep["ziffern"]["322_steuer"] == D("67.02")
    api.mwst_book(Book(book.root), "2026-S1")
    assert balance(book.root, "3809") == D("67.02") and balance(book.root, "2201") == D("-67.02")
    assert errors(book.root) == []


def test_not_registered_refuses_codes(tmp_path):
    root = tmp_path / "b"
    api.init_book(root, "Klein GmbH", 2026)
    with pytest.raises(BookError):
        api.post_entry(Book(root), "2026-01-10", "6500", "1020", "10", "x", mwst="V81")


def test_proposal_with_mwst_splits_on_approval(book):
    api.propose(book, "2026-01-12", "6510", "1020", "59.00", "Swisscom", "Telefon", mwst="V81")
    api.approve(Book(book.root), ["V-001"])
    rows = Book(book.root).rows
    assert sum(r.betrag for r in rows if r.soll == "1170") == D("4.42")
    assert errors(book.root) == []

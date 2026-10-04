"""Spesen: claims booked against 2210, paid with the next payslip, Lohnausweis 13.1.2."""
from __future__ import annotations

from decimal import Decimal

import pytest

from batzen import api, erfassung, lohnausweis, payroll, spesen
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, strasse="Hauptstrasse", nr="1", plz="3000",
                  ort="Bern")
    api.employee_add(b, "Lea", "Muster", monatslohn=5000)
    return Book(b.root)


def saldo(book, konto):
    return Decimal(api.ledger(Book(book.root), konto, 2026)["saldo"])


def test_claim_paid_with_payslip(book):
    api.expense_add(book, "M0001", "2026-03-10", "Zugbillett Zürich", "80.00", "5820", art="reise")
    assert saldo(book, "5820") == Decimal("80.00") and saldo(book, "2210") == Decimal("-80.00")
    api.payroll_run(Book(book.root), "2026-03")
    slip = payroll.load_payslip(Book(book.root), 2026, 3, "M0001")
    w = slip["werte"]
    assert w["spesen"] == Decimal("80.00") and Decimal(str(w["auszahlung"])) == Decimal(str(w["nettolohn"])) + 80
    assert "SP-2026-0001" in slip["eingaben"]["spesen"]
    api.payslip_close(Book(book.root), "2026-03", "M0001")
    b = Book(book.root)
    assert saldo(b, "2210") == 0
    assert spesen.assignments(b)["SP-2026-0001"] == "2026-03:M0001"
    with pytest.raises(BookError, match="Lohnabrechnung"):
        api.expense_remove(b, "SP-2026-0001")
    assert_clean(b)


def test_late_claim_goes_to_next_month_and_reopen_keeps_it(book):
    api.payroll_run(book, "2026-03")
    api.payslip_close(Book(book.root), "2026-03", "M0001")
    api.expense_add(Book(book.root), "M0001", "2026-03-28", "Apéro Kunde", "45.00", "6641")
    api.payroll_run(Book(book.root), "2026-04")
    assert payroll.load_payslip(Book(book.root), 2026, 4, "M0001")["werte"]["spesen"] == Decimal("45.00")
    api.payroll_run(Book(book.root), "2026-04")                      # re-run: not counted twice
    assert payroll.load_payslip(Book(book.root), 2026, 4, "M0001")["eingaben"]["spesen"] == ["SP-2026-0001"]


def test_open_claim_can_be_withdrawn(book):
    api.expense_add(book, "M0001", "2026-03-10", "x", "10.00", "5820")
    api.expense_remove(Book(book.root), "SP-2026-0001")
    assert saldo(book, "2210") == 0 and api.expense_list(Book(book.root)) == []


def test_lohnausweis_other_expenses(book):
    api.expense_add(book, "M0001", "2026-03-10", "Fachbuch", "120.00", "5810", art="uebrige")
    api.expense_add(Book(book.root), "M0001", "2026-03-12", "Hotel", "180.00", "5820", art="reise")
    api.payroll_run(Book(book.root), "2026-03")
    api.payslip_close(Book(book.root), "2026-03", "M0001")
    totals = lohnausweis.annual_totals(Book(book.root), 2026, "M0001")
    assert totals["spesen_uebrige"] == Decimal("120.00")
    pdf = lohnausweis.build_pdf(Book(book.root), 2026, "M0001")
    assert pdf.startswith(b"%PDF")


def test_receipt_paid_privately_becomes_claim(book):
    (book.root / "inbox").mkdir(exist_ok=True)
    (book.root / "inbox" / "hotel.txt").write_text("Hotel Bern AG\nQuittung\nDatum 12.03.2026\nTotal CHF 180.00\nbezahlt")
    d = api.bill_draft_create(Book(book.root), "inbox/hotel.txt")["entwurf"]
    api.bill_draft_update(Book(book.root), d["id"], "Hand", "5820", mitarbeiter="M0001")
    meta = erfassung.draft(Book(book.root), d["id"])
    assert meta["zahlung"]["art"] == "spesen"
    api.receipt_book(Book(book.root), d["id"], "2026-03-12", "Hotel Bern", "180.00", "5820", zahlung=meta["zahlung"])
    b = Book(book.root)
    claim = spesen.claim(b, "SP-2026-0001")
    assert claim["datei"].startswith("belege/") and saldo(b, "2210") == Decimal("-180.00")
    assert api.bill_drafts(b) == []


def test_payroll_without_expenses_is_unchanged(book):
    api.payroll_run(book, "2026-03")
    slip = payroll.load_payslip(Book(book.root), 2026, 3, "M0001")
    assert "spesen" not in slip["werte"] and "spesen" not in slip["eingaben"]

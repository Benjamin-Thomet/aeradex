"""Mahnwesen: overdue invoices, three steps with deadlines, PDF with QR-bill for the open amount."""
from __future__ import annotations

from decimal import Decimal

import pytest

from batzen import api, mahnungen
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, iban="CH44 3199 9123 0008 8901 2", strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  eroeffnung={"1020": 1000, "2800": -1000})
    api.customer_add(b, name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    api.invoice_create(Book(b.root), "K0001", [{"text": "Beratung", "menge": 2, "preis": "500"}], "2026-01-10")
    return Book(b.root)


def test_three_steps_with_deadlines(book):
    nr = "R-2026-0001"                                       # due 09.02.2026
    assert mahnungen.overdue(book, __import__("datetime").date(2026, 2, 9)) == []
    api.invoice_pay(book, nr, "200", "2026-02-01")
    rows = api.reminders(Book(book.root), "2026-02-20")
    assert rows[0]["nummer"] == nr and rows[0]["bereit"] and rows[0]["naechste_bezeichnung"] == "Zahlungserinnerung"
    out = api.reminder_create(Book(book.root), [nr], "2026-02-20")
    m = out["mahnungen"][0]
    assert m["stufe"] == 1 and m["frist"] == "2026-03-02" and m["offen"] == "800.00"
    pdf = book.root / m["pdf"]
    assert pdf.read_bytes().startswith(b"%PDF")
    with pytest.raises(BookError, match="Frist"):
        api.reminder_create(Book(book.root), [nr], "2026-02-25")
    api.reminder_create(Book(book.root), [nr], "2026-03-05")
    api.reminder_create(Book(book.root), [nr], "2026-03-20")
    with pytest.raises(BookError, match="Betreibung"):
        api.reminder_create(Book(book.root), [nr], "2026-04-10")
    assert [h["bezeichnung"] for h in mahnungen.history(Book(book.root), nr)] == [
        "Zahlungserinnerung", "2. Mahnung", "3. Mahnung"]
    assert_clean(Book(book.root))


def test_not_due_and_paid_invoices_are_refused(book):
    with pytest.raises(BookError, match="erst am"):
        api.reminder_create(book, ["R-2026-0001"], "2026-02-01")
    api.invoice_pay(Book(book.root), "R-2026-0001", "1000", "2026-02-05")
    with pytest.raises(BookError, match="bezahlt"):
        api.reminder_create(Book(book.root), ["R-2026-0001"], "2026-03-01")


def test_reminder_page_and_detail(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from batzen.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert "R-2026-0001" in c.get("/debitoren/mahnungen").text
        r = c.post("/debitoren/mahnungen", headers=h, data={"nr": "R-2026-0001", "datum": "2026-02-20", "frist": "14"})
        assert r.status_code == 204, r.text
        assert "Zahlungserinnerung" in c.get("/debitoren/rechnung/R-2026-0001").text
    assert mahnungen.history(Book(book.root), "R-2026-0001")[0]["frist"] == "2026-03-06"

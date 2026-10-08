"""Opening the next business year before its first booking, and the checklist for the change of year."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from aeradex import api, check, jahreswechsel, journal, statements
from aeradex.book import Book, BookError


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Weg", nr="1", plz="3000", ort="Bern")
    api.post_entry(Book(root), "2026-03-02", "1020", "2800", "20000.00", "Einzahlung Stammkapital")
    api.post_entry(Book(root), "2026-05-02", "6500", "1020", "500.00", "Büromaterial")
    return Book(root)


def test_open_year_before_first_booking_carries_balances(book):
    res = api.year_open(book)
    assert res["jahr"] == 2027
    b = Book(book.root)
    assert b.years() == [2026, 2027]
    assert journal.next_beleg(b, 2027).startswith("27-")
    st = statements.year_end_statement(b, 2027)
    total = lambda rows, col: next(r[col] for r in rows if r["stil"] == "total")  # noqa: E731
    assert total(st["aktiven"], "aktuell") == total(st["aktiven"], "vorjahr") == 19500
    assert [str(i) for i in check.run(b) if i.level != "hinweis"] == []


def test_years_open_in_order_once_and_not_far_ahead(book):
    with pytest.raises(BookError, match="bereits eröffnet"):
        jahreswechsel.open_year(book, 2026)
    with pytest.raises(BookError, match="der Reihe nach"):
        jahreswechsel.open_year(book, 2028)
    with pytest.raises(BookError, match="Zukunft"):
        jahreswechsel.open_year(book, 2027, today=date(2025, 6, 1))


def test_checklist_names_what_is_open_from_the_prior_year(book):
    api.year_open(book)
    items = {i["titel"]: i["status"] for i in jahreswechsel.checklist(Book(book.root), 2027)}
    assert items["Eröffnungsbilanz per 01.01.2027"] == "ok"
    assert items["Geschäftsjahr 2026 abschliessen und sperren"] == "offen"
    assert items["Gewinnverwendung 2026"] == "offen"
    api.lock(Book(book.root), "2026-12-31")
    items = {i["titel"]: i["status"] for i in jahreswechsel.checklist(Book(book.root), 2027)}
    assert items["Geschäftsjahr 2026 gesperrt"] == "ok"
    assert jahreswechsel.checklist(Book(book.root), 2026) == []          # the first year has no predecessor

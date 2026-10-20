"""Year-end proposals from the recorded work: open work at cost, holidays and overtime."""
from datetime import date
from decimal import Decimal

import pytest

from aeradex import api
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book
from aeradex_leistungen import abwesenheit, daten


def W(book, fn, *args, **kw):
    return api.write(Book(book.root), "test", fn, *args, **kw).get("ergebnis")


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["leistungen"], eroeffnung={"1020": 10000, "2800": -10000},
                  strasse="Hauptstrasse", nr="1", plz="3600", ort="Thun", iban="CH93 0076 2011 6238 5295 7")
    api.customer_add(b, name="Anna Beispiel", firma="Beispiel AG", strasse="Marktgasse", nr="5", plz="3011", ort="Bern")
    api.employee_add(Book(b.root), "Lea", "Muster", monatslohn=6000, pensum=100, vollzeit_stunden_woche=42,
                     eintritt="2026-01-01")
    api.employee_add(Book(b.root), "Tom", "Stunde", lohnart="stunde", stundenlohn=32, eintritt="2026-01-01")
    W(b, daten.set_person, "M0001", 120, 70)          # Lea: rate 120, cost 70
    W(b, daten.set_person, "M0002", 80)               # Tom: no cost rate
    return Book(b.root)


def test_open_work_per_31_12_at_cost(book):
    W(book, daten.add_time, "2026-12-15", "M0001", "10", "Fassade", "K0001")
    W(book, daten.add_time, "2026-12-16", "M0002", "5", "Fassade", "K0001")
    W(book, daten.add_time, "2027-01-05", "M0001", "8", "Fassade", "K0001")       # next year: not in it
    v = api.closing_entry(Book(book.root), 2026, "arbeiten")
    [p] = [p for p in v["vorschlaege"] if p.get("quelle") == "leistungen"]
    assert Decimal(p["betrag"]) == Decimal("700.00") + Decimal("400.00")          # 10 h × 70 + 5 h × 80 (no cost)
    assert "Beispiel AG" in p["text"] and "ohne Kostensatz für M0002" in p["detail"]


def test_holidays_left_and_overtime(book):
    abwesenheit.add(Book(book.root), "M0001", "Ferien", "2026-07-06", "2026-07-17")   # 10 days taken
    v = api.closing_entry(Book(book.root), 2026, "ferien")
    lea = next(p for p in v["vorschlaege"] if p["text"] == "Lea Muster")
    days = abwesenheit.holiday_account(Book(book.root), "M0001", 2026, today=date(2026, 12, 31))["rest"]
    assert Decimal(lea["betrag"]) == (days * Decimal("8.4") * 70).quantize(Decimal("0.01"))
    assert not any(p["text"] == "Tom Stunde" for p in v["vorschlaege"])           # hourly, no overtime
    api.closing_entry_book(Book(book.root), 2026, "ferien", [{"text": lea["text"], "betrag": lea["betrag"]}])
    b = Book(book.root)
    assert b.accounts["2331"].klasse == "passiv"
    assert_clean(b)

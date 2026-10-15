"""Pensum changes during the year: the target follows the Pensum in force on each day."""
import calendar
from datetime import date
from decimal import Decimal

import pytest

from aeradex.book import Book, BookError
from aeradex.testing import assert_clean
from aeradex_leistungen import abwesenheit, daten, kontrolle

from test_leistungen import W, book  # noqa: F401  (fixture: Lea M0001, 42 h, 100 %, from 1.1.2026)

D = Decimal


def workdays(book, year, month):
    hols = abwesenheit.holidays(Book(book.root), year)
    return sum(1 for d in range(1, calendar.monthrange(year, month)[1] + 1)
               if date(year, month, d).weekday() < 5 and date(year, month, d) not in hols)


def test_without_changes_nothing_differs(book):
    p = daten.person(Book(book.root), "M0001")
    assert p["wochen"] == [] and p["woche"] == D(42)
    assert daten.week_hours(p, date(2026, 3, 1)) == D(42)


def test_change_from_july(book):
    W(book, daten.set_pensum, "M0001", "2026-07-01", "80")
    b = Book(book.root)
    p = daten.person(b, "M0001")
    assert [(d.isoformat(), h) for d, h in p["wochen"]] == [("2026-01-01", D("42")), ("2026-07-01", D("33.6"))]
    assert daten.week_hours(p, date(2026, 6, 30)) == D(42)
    assert daten.week_hours(p, date(2026, 7, 1)) == D("33.6")
    hols = abwesenheit.holidays(b, 2026)
    assert kontrolle.target(p, 2026, 6, hols) == (D("8.4") * workdays(book, 2026, 6)).quantize(D("0.01"))
    assert kontrolle.target(p, 2026, 7, hols) == (D("6.72") * workdays(book, 2026, 7)).quantize(D("0.01"))
    # a holiday day in August counts the hours of the Pensum then in force
    W(book, abwesenheit.add, "M0001", "Ferien", "2026-08-11")
    b = Book(book.root)
    assert abwesenheit.day_hours(b, daten.person(b, "M0001"), date(2026, 8, 11), hols)["stunden"] == D("6.72")
    # the year's balance adds the months at their own target
    rows = {r["monat"]: r for r in kontrolle.year(b, 2026, "M0001")}
    assert rows[6]["soll"] == kontrolle.target(p, 2026, 6, hols)
    assert rows[8]["soll"] == kontrolle.target(p, 2026, 8, hols)
    assert_clean(b)


def test_change_within_a_month(book):
    W(book, daten.set_pensum, "M0001", "2026-09-16", "50")
    b = Book(book.root)
    p = daten.person(b, "M0001")
    hols = abwesenheit.holidays(b, 2026)
    full = sum(1 for d in range(1, 16) if date(2026, 9, d).weekday() < 5 and date(2026, 9, d) not in hols)
    half = workdays(book, 2026, 9) - full
    assert kontrolle.target(p, 2026, 9, hols) == (D("8.4") * full + D("4.2") * half).quantize(D("0.01"))


def test_remove_and_validation(book):
    W(book, daten.set_pensum, "M0001", "2026-07-01", "80")
    W(book, daten.set_pensum, "M0001", "2026-07-01", None, True)
    p = daten.person(Book(book.root), "M0001")
    assert p["wochen"] == [] and p["woche"] == D(42)
    with pytest.raises(BookError):
        W(book, daten.set_pensum, "M0001", "2026-07-01", "120")
    with pytest.raises(BookError):
        W(book, daten.set_pensum, "M0001", "2026-07-01", None, True)


def test_person_without_payroll(book):
    nr = W(book, daten.set_person, "", None, None, "Inhaberin", 40)["nummer"]
    W(book, daten.set_pensum, nr, "2026-05-01", "20")
    p = daten.person(Book(book.root), nr)
    assert daten.week_hours(p, date(2026, 4, 30)) == D(40)
    assert daten.week_hours(p, date(2026, 5, 4)) == D(20)


def test_hourly_paid_have_no_target(book):
    W(book, daten.set_pensum, "M0002", "2026-07-01", "50")
    p = daten.person(Book(book.root), "M0002")
    assert abwesenheit.daily_target(p, date(2026, 8, 3)) is None


def test_cli_builds_and_sets_pensum(book, capsys):
    """Every help text must survive argparse's format check (Python 3.14 checks them when they are added)."""
    import argparse
    from aeradex_leistungen import _run, _setup
    parser = argparse.ArgumentParser()
    _setup(parser)
    parser.format_help()
    for action in parser._subparsers._group_actions:
        for sub in action.choices.values():
            sub.format_help()
    args = parser.parse_args(["pensum", "--wer", "M0001", "--ab", "2026-07-01", "--wert", "80"])
    _run(Book(book.root), args)
    assert daten.week_hours(daten.person(Book(book.root), "M0001"), date(2026, 7, 2)) == D("33.6")

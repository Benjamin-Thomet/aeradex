"""Quellensteuer: liability from the permit, canton from the residence (or the employer's seat)."""
from __future__ import annotations

from pathlib import Path

import pytest

from aeradex import api, payroll, qst_estv
from aeradex.book import Book, BookError


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Weg", nr="1", plz="4051", ort="Basel")
    return Book(root)


def emp(**kw):
    base = {"vorname": "Ana", "nachname": "Test", "adresse": {"plz": "4051", "ort": "Basel", "land": "CH"}}
    base.update(kw)
    return base


def test_liability_follows_the_permit_and_the_override():
    assert payroll.qst_pflichtig(emp(aufenthalt="B"))[0] is True
    assert payroll.qst_pflichtig(emp(aufenthalt="G"))[0] is True
    assert payroll.qst_pflichtig(emp(aufenthalt="C"))[0] is False
    assert payroll.qst_pflichtig(emp(aufenthalt="CH"))[0] is False
    assert payroll.qst_pflichtig(emp(aufenthalt="B", qst_pflicht="nein"))[0] is False     # married to a Swiss
    abroad = emp(adresse={"plz": "79539", "ort": "Lörrach", "land": "DE"}, aufenthalt="CH")
    assert payroll.qst_pflichtig(abroad) == (True, "Wohnsitz im Ausland (DE)")
    assert payroll.qst_pflichtig(emp(qst={"code": "A0N"}))[0] is True                    # older record, no permit


def test_canton_from_residence_override_or_employer_seat(book):
    assert payroll.qst_kanton(book, emp(adresse={"plz": "3600", "ort": "Thun", "land": "CH"}))[0] == "BE"
    assert payroll.qst_kanton(book, emp(qst={"code": "A0N", "kanton": "ZH"}))[0] == "ZH"   # Wochenaufenthalt
    abroad = emp(adresse={"plz": "79539", "ort": "Lörrach", "land": "DE"})
    assert payroll.qst_kanton(book, abroad)[0] == "BS"                                   # firm in Basel
    with pytest.raises(BookError, match="GE und VD"):
        payroll.qst_kanton(book, emp(adresse={"plz": "1290", "ort": "", "land": "CH"}))


def test_pay_run_uses_residence_canton_and_old_records_still_work(book):
    api.employee_add(book, "Ana", "Basel", monatslohn=5000, plz="4051", ort="Basel", aufenthalt="B",
                     qst={"code": "A0N"})
    api.employee_add(Book(book.root), "Old", "Record", monatslohn=5000, qst={"kanton": "BS", "jahr": 2026, "code": "A0N"})
    api.employee_add(Book(book.root), "Cleo", "C", monatslohn=5000, plz="4051", ort="Basel", aufenthalt="C",
                     qst={"code": "A0N"})
    api.payroll_run(Book(book.root), "2026-03")
    slips = {s["mitarbeiter"]: s["werte"] for s in (api.payslip_show(Book(book.root), "2026-03", n)
                                                    for n in ("M0001", "M0002", "M0003"))}
    assert slips["M0001"]["qst_kanton"] == "BS" and float(slips["M0001"]["quellensteuer"]) > 0
    assert slips["M0001"]["quellensteuer"] == slips["M0002"]["quellensteuer"]
    assert float(slips["M0003"]["quellensteuer"]) == 0                                    # C permit: no tax


def test_liable_without_tariff_is_refused(book):
    api.employee_add(book, "Ana", "Basel", monatslohn=5000, plz="4051", ort="Basel", aufenthalt="B")
    with pytest.raises(BookError, match="Tarifcode"):
        api.payroll_run(Book(book.root), "2026-03")


def test_missing_table_is_loaded_in_the_same_run(book, monkeypatch):
    from test_payroll_review import records
    calls = []

    def fake(canton, year):
        calls.append((canton, year))
        return qst_estv.parse(records(canton, year), canton, year, "fixture")
    monkeypatch.setattr(qst_estv, "download", fake)
    api.employee_add(book, "Ben", "Thun", monatslohn=5000, plz="3600", ort="Thun", aufenthalt="L", qst={"code": "A0N"})
    res = api.payroll_run(Book(book.root), "2026-03")
    assert calls == [("BE", 2026)] and "Quellensteuertarife 2026 geladen: BE" in res["meldung"]
    assert (book.root / "lohn" / "qst_tarife" / "BE-2026.json").exists()
    api.payroll_run(Book(book.root), "2026-03")
    assert calls == [("BE", 2026)]                                                     # not loaded twice

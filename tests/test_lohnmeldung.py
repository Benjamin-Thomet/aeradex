"""Company Lohnkonto and the annual wage declarations (Lohnmeldungen)."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, dossier, lohnmeldung, payroll
from aeradex.book import Book, BookError


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    b = Book(root)
    api.employee_add(b, "Lea", "Muster", monatslohn=6000, geschlecht="w", geburtsdatum="1990-05-02",
                     ahv_nr="756.1234.5678.97", kinderzulagen=215, eintritt="2026-01-01")
    api.employee_add(Book(root), "Tom", "Test", monatslohn=4000, bvg_betrag=200, geschlecht="m",
                     geburtsdatum="1985-01-10", ahv_nr="756.9876.5432.10", eintritt="2026-01-01")
    for month in ("2026-01", "2026-02"):
        api.payroll_run(Book(root), month)
        api.payroll_close(Book(root), month)
    api.payroll_run(Book(root), "2026-03")              # draft: not in the Lohnkonto
    return Book(root)


def _row(lk, key):
    return next(r for r in lk["zeilen"] + lk["ag_zeilen"] if r["key"] == key)


def test_company_matrix_is_sum_of_people(book):
    firma = payroll.lohnkonto_matrix(book, 2026)
    lea, tom = (payroll.lohnkonto_matrix(book, 2026, nr) for nr in ("M0001", "M0002"))
    assert firma["bereich"] == "Ganze Firma" and firma["abrechnungen"] == 4 and firma["entwuerfe"] == 2
    for key in ("bruttolohn", "ahv", "bvg", "kinderzulagen", "nettolohn", "auszahlung", "ag_total", "ag_kosten"):
        assert _row(firma, key)["total"] == _row(lea, key)["total"] + _row(tom, key)["total"]
    assert _row(firma, "bruttolohn")["monate"][1] == Decimal("10000.00")
    assert _row(firma, "bruttolohn")["monate"][3] == 0                   # March is a draft
    assert _row(firma, "bruttolohn")["total"] == Decimal("20000.00")
    # Kinderzulagen are not an employer cost: Brutto + Korrektur + AG contributions
    assert _row(firma, "ag_kosten")["total"] == Decimal("20000.00") + _row(firma, "ag_total")["total"]
    assert _row(lea, "auszahlung")["total"] == _row(lea, "nettolohn")["total"]
    # the per-employee Lohnkonto (Lohnausweis basis) agrees
    assert payroll.lohnkonto(book, 2026, "M0001")["total"]["nettolohn"] == _row(lea, "nettolohn")["total"]


def test_declaration_totals_and_ledger_check(book):
    m = lohnmeldung.jahresmeldung(book, 2026)
    ak = m["ausgleichskasse"]
    assert ak["ahv_lohn"] == Decimal("20000.00") == ak["fak_lohn"]
    assert ak["nach_geschlecht"] == {"Frauen": Decimal("12000.00"), "Männer": Decimal("8000.00")}
    assert ak["kinderzulagen"] == Decimal("430.00")
    b = ak["beitraege"]
    assert ak["saldo"] == b["ahv_an"] + b["ahv_ag"] + b["alv_an"] + b["alv_ag"] + b["fak"] + b["verwaltung"] - 430
    assert m["bvg"]["beitraege"] == {"an": Decimal("400.00"), "ag": Decimal("400.00")}
    assert m["uvg"]["uvg_lohn"] == Decimal("20000.00")
    lea = next(p for p in m["personen"] if p["nummer"] == "M0001")
    assert (lea["von"], lea["bis"]) == (date(2026, 1, 1), date(2026, 2, 28))
    assert all(c["differenz"] == 0 for c in m["abgleich"]) and m["abgleich"]
    assert any("Entwurf 03/2026" in h for h in m["hinweise"])
    assert not any("fehl" in h and "Personalstamm" in h for h in m["hinweise"])
    # same Bruttolohn as the Lohnjournal of the closing documents
    journal = dossier.LOHN_COLS[0][0]
    assert sum(Decimal(str(p["werte"][journal])) for p in payroll.payslips(book, 2026)
               if p["status"] == "abgeschlossen") == ak["ahv_lohn"]


def test_ledger_check_flags_bookings_outside_payroll(book):
    api.post_entry(book, "2026-02-15", "5000", "1000", "300", "Aushilfe bar")
    m = lohnmeldung.jahresmeldung(Book(book.root), 2026)
    diff = next(c for c in m["abgleich"] if c["konto"] == "5000")
    assert diff["differenz"] == Decimal("300.00")
    assert any("Konto 5000" in h for h in m["hinweise"])


def test_hints_pensioner_youth_missing_data_and_cap(tmp_path):
    root = tmp_path / "b"
    api.init_book(root, "Test GmbH", 2026, strasse="H", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    api.employee_add(Book(root), "Rita", "Rente", monatslohn=3000, geschlecht="w", geburtsdatum="1961-05-02",
                     ahv_nr="756.1111.1111.11", eintritt="2026-01-01")
    api.employee_add(Book(root), "Jan", "Jung", monatslohn=1000, geschlecht="m", geburtsdatum="2010-03-01",
                     ahv_nr="756.2222.2222.22", eintritt="2026-01-01")
    api.employee_add(Book(root), "Max", "Chef", monatslohn=15000, eintritt="2026-01-01")
    api.payroll_run(Book(root), "2026-01")
    api.payroll_close(Book(root), "2026-01")
    hints = lohnmeldung.jahresmeldung(Book(root), 2026)["hinweise"]
    text = "\n".join(hints)
    assert "M0001 Rita Rente: Rentner ab 01.09.2025" in text          # 64 y 3 m (AHV 21, Jahrgang 1961)
    assert "M0002 Jan Jung: erst ab 1.1.2028 AHV-beitragspflichtig" in text
    assert "M0003 Max Chef: AHV-Nr., Geburtsdatum, Geschlecht fehlen" in text
    assert "M0003 Max Chef: Höchstlohn" in text                       # 15'000 > 148'200 / 12
    m = lohnmeldung.jahresmeldung(Book(root), 2026)
    max_ = next(p for p in m["personen"] if p["nummer"] == "M0003")
    assert max_["uvg_lohn_max"] == Decimal("12350.00") and max_["uvg_lohn"] == Decimal("15000.00")


def test_reference_age_table():
    assert lohnmeldung.rentner_ab(date(1960, 6, 15), "w") == date(2024, 7, 1)
    assert lohnmeldung.rentner_ab(date(1962, 1, 1), "w") == date(2026, 8, 1)       # 64 y 6 m
    assert lohnmeldung.rentner_ab(date(1964, 3, 1), "w") == date(2029, 4, 1)
    assert lohnmeldung.rentner_ab(date(1961, 3, 1), "m") == date(2026, 4, 1)


def test_documents_and_api(book, tmp_path):
    for fmt in ("pdf", "csv", "xlsx"):
        if fmt == "xlsx":
            pytest.importorskip("openpyxl")
        name, data = lohnmeldung.meldung_document(book, 2026, fmt)
        assert name == f"Lohnmeldungen 2026.{fmt}" and data
        name, data = lohnmeldung.lohnkonto_document(book, 2026, None, fmt)
        assert name == f"Lohnkonto 2026 Ganze Firma.{fmt}"
    assert lohnmeldung.meldung_document(book, 2026, "pdf")[1].startswith(b"%PDF")
    res = api.lohn_dokument(book, "meldungen", 2026, "csv", ziel=str(tmp_path))
    assert Path(res["datei"]).read_bytes().decode("utf-8-sig").startswith("Ausgleichskasse")
    assert api.lohnkonto_firma(book, 2026)["bereich"] == "Ganze Firma"
    assert api.lohnmeldungen(book, 2026)["ausgleichskasse"]["ahv_lohn"] == "20000.00"
    with pytest.raises(BookError):
        lohnmeldung.meldung_document(book, 2026, "doc")


def test_dossier_contains_lohnkonto_and_meldungen(book):
    k = dossier.Kontext(book, 2026)
    names = [n for n, _ in dossier._lohn(k, "pdf")]
    assert "Lohn 2026/Lohnkonto 2026 Ganze Firma.pdf" in names and "Lohn 2026/Lohnmeldungen 2026.pdf" in names


def test_geschlecht_is_validated(book):
    with pytest.raises(BookError):
        api.employee_add(book, "X", "Y", geschlecht="x")
    api.employee_update(Book(book.root), "M0002", geschlecht="w")
    assert payroll.employee(Book(book.root), "M0002")["geschlecht"] == "w"
    with pytest.raises(BookError):
        api.employee_update(Book(book.root), "M0002", geschlecht="q")

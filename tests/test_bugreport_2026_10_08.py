"""Regressions for the review of 2026-10-08 (BZ-01 … BZ-07), each with the reproduction from the report."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, buchungsimport, check, invoices, payroll, qst_estv
from aeradex.book import Book, BookError
from aeradex.testing import make_book

D = Decimal


@pytest.fixture
def book(tmp_path: Path) -> Book:
    b = make_book(tmp_path, strasse="Testweg", nr="1", plz="4051", ort="Basel", iban="CH9300762011623852957")
    api.customer_add(b, name="Test Person", strasse="Testweg", nr="2", plz="4051", ort="Basel")
    return Book(b.root)


def errors(book: Book) -> list[str]:
    return [str(i) for i in check.run(Book(book.root)) if i.level == "fehler"]


# ---- BZ-01: files that can carry script are downloads, never same-origin documents ----

def test_bz01_active_files_are_downloads_and_passive_ones_stay_inline(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    inbox = book.root / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "review.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>document.title="x"</script></svg>')
    (inbox / "page.xhtml").write_text('<html xmlns="http://www.w3.org/1999/xhtml"><script>1</script></html>')
    (inbox / "beleg.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (inbox / "foto.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with TestClient(create_app(book.root, token="tok")) as c:
        c.get("/?t=tok")
        for name in ("review.svg", "page.xhtml"):
            r = c.get(f"/datei/inbox/{name}")
            assert r.headers["content-disposition"].startswith("attachment")
            assert r.headers["content-type"] == "application/octet-stream"
            assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
        pdf = c.get("/datei/inbox/beleg.pdf")
        assert pdf.headers["content-disposition"].startswith("inline") and pdf.headers["content-type"] == "application/pdf"
        assert "content-security-policy" not in pdf.headers          # the browser's PDF viewer must still work
        png = c.get("/datei/inbox/foto.png")
        assert png.headers["content-disposition"].startswith("inline") and "sandbox" in png.headers["content-security-policy"]


# ---- BZ-02: nothing is paid or deducted outside the employment period ----

def test_bz02_future_hire_gets_no_payslip_and_no_allowances(book):
    api.employee_add(book, "Future", "Employee", monatslohn=5000, eintritt="2026-04-01", bvg_betrag=200, kinderzulagen=250)
    res = api.payroll_run(Book(book.root), "2026-03")
    assert "Nichts zu rechnen" in res["meldung"]
    with pytest.raises(BookError, match="nicht angestellt"):
        api.payroll_run(Book(book.root), "2026-03", "M0001")
    api.payroll_run(Book(book.root), "2026-04")
    w = api.payslip_show(Book(book.root), "2026-04", "M0001")["werte"]
    assert D(w["bruttolohn"]) == 5000 and D(w["bvg"]) == 200 and D(w["kinderzulagen"]) == 250


def test_bz02_calculation_outside_employment_has_no_recurring_amounts(book):
    cfg = payroll.config(book)
    emp = {"monatslohn": 5000, "pensum": 100, "eintritt": "2026-01-01", "austritt": "2026-02-28",
           "bvg_betrag": 200, "kinderzulagen": 250}
    w = payroll.calculate(emp, cfg, 2026, 3, {})
    assert w["bruttolohn"] == w["bvg"] == w["kinderzulagen"] == w["nettolohn"] == 0
    w = payroll.calculate(emp, cfg, 2026, 3, {"kinderzulagen": 100})     # an explicit entry still applies
    assert w["kinderzulagen"] == 100


def test_bz02_exit_before_entry_is_refused(book):
    api.employee_add(book, "Ana", "C", monatslohn=5000, eintritt="2026-03-20")
    with pytest.raises(BookError, match="Austritt liegt vor dem Eintritt"):
        api.employee_update(Book(book.root), "M0001", austritt="2026-03-10")


# ---- BZ-03: the frozen invoice recomputes to its own figures ----

@pytest.mark.parametrize("preis, betrag", [("0.335", "1.01"), ("0.333", "1.00"), ("1.005", "3.02")])
def test_bz03_fractional_unit_price_is_kept_and_round_trips(book, preis, betrag):
    res = api.invoice_create(book, "K0001", [{"text": "fractional price", "menge": 3, "preis": preis}], "2026-03-01")
    pos = res["rechnung"]["positionen"][0]
    assert D(str(pos["preis"])) == D(preis) and D(str(pos["betrag"])) == D(betrag)
    again = invoices.normalize_positions([{**p} for p in invoices.invoice(Book(book.root), res["rechnung"]["nummer"])["positionen"]],
                                         "3400")
    assert again[0]["betrag"] == D(betrag)
    assert errors(book) == []


def test_bz03_cent_prices_unchanged_and_too_fine_prices_refused(book):
    assert invoices.normalize_positions([{"text": "x", "menge": 2, "preis": "12.5"}], "3400")[0]["preis"] == D("12.50")
    with pytest.raises(BookError, match="vier Nachkommastellen"):
        invoices.normalize_positions([{"text": "x", "menge": 1, "preis": "0.12345"}], "3400")


# ---- BZ-04: foreign currency rounds half-up like every other amount ----

def test_bz04_fx_conversion_rounds_half_up(book):
    beleg = api.post_entry(book, "2026-03-01", "6500", "1020", "1", "FX rounding", waehrung="EUR", kurs="0.905")["buchung"]["beleg"]
    row = next(r for r in Book(book.root).rows if r.beleg == beleg)
    assert row.betrag == D("0.91") and row.fw == D("1") and row.kurs == D("0.905")
    assert errors(book) == []


# ---- BZ-05: commas inside a quoted description do not change the delimiter ----

def test_bz05_semicolon_csv_with_quoted_commas():
    data = ("Datum;Text;Soll;Haben;Betrag\n"
            '01.10.2026;"' + "comma, " * 15 + '";6500;1020;10,50\n'
            '02.10.2026;"zweizeilig\nBeschreibung";6500;1020;3.00\n').encode()
    rows = buchungsimport.read(data, "test.csv")
    assert [r[4] for r in rows] == ["10,50", "3.00"] and rows[0][1].startswith("comma,")
    assert buchungsimport.read(b"Datum,Text,Soll,Haben,Betrag\n01.10.2026,Miete,6000,1020,1800\n", "x.csv")[0][2] == "6000"


# ---- BZ-06: malformed live status input answers with a message ----

@pytest.mark.parametrize("params", [{"qst_satz_pct": "abc"}, {"aufenthalt": "invalid"}, {"qst_satz_pct": "NaN"},
                                    {"qst_satz_pct": "Infinity"}, {"qst_tarif": "Z", "aufenthalt": "B"}])
def test_bz06_status_box_never_crashes_or_shows_non_finite_rates(book, params):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    with TestClient(create_app(book.root, token="tok")) as c:
        c.get("/?t=tok")
        r = c.get("/lohn/qst-status", params={"plz": "4051", "ort": "Basel", **params})
    assert r.status_code == 200 and "qstline warn" in r.text and "NaN %" not in r.text and "Infinity %" not in r.text


# ---- BZ-07: a rerun of a closed month needs no tariff download ----

def test_bz07_closed_payslip_triggers_no_download(book, monkeypatch):
    api.employee_add(book, "Ana", "Basel", monatslohn=5000, plz="4051", ort="Basel", aufenthalt="B", qst={"code": "A0N"})
    api.payroll_run(Book(book.root), "2026-03")
    api.payslip_close(Book(book.root), "2026-03", "M0001")
    api.employee_update(Book(book.root), "M0001", plz="8001", ort="Zürich")
    calls = []

    def offline(canton, year):
        calls.append((canton, year))
        raise BookError("offline")
    monkeypatch.setattr(qst_estv, "download", offline)
    res = api.payroll_run(Book(book.root), "2026-03")
    assert calls == [] and "Nichts zu rechnen" in res["meldung"]

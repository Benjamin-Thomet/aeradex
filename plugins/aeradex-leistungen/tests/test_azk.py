"""SMGV Arbeitszeitkontrolle: a template stand-in with the same cells (the association's file is not shipped), filled
from the hours, travel time and absences; another year made from it; cells written into the XML, not re-saved."""
import io
import zipfile
from decimal import Decimal

import pytest

from aeradex import api
from aeradex.book import Book, BookError
from aeradex.testing import make_book
from aeradex_leistungen import abwesenheit, azk, daten

openpyxl = pytest.importorskip("openpyxl")


def template(year=2026) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Admin_Konfig")
    ws["F3"], ws["G17"] = year, "komplett vergütet"
    base = wb.create_sheet("Basisblatt")
    base["G3"] = "=H_Label!B162"                        # the template's example text: replaced by the value
    labels = wb.create_sheet("H_Label")
    for (sheet, ref), text in azk.EXPECTED.items():
        labels[ref] = text
    for m in azk.MONTHS:
        s = wb.create_sheet(m)
        for row, code in zip(range(36, 50), ["f", "kv", "kj", "a", "k", "u", "s", "e", "m", "bs", "ku", "ka", "ba", "fu"]):
            s.cell(row, azk.CODE_COL, code)
        s["AL28"] = "=SUM(F28:AJ28)"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["leistungen"], strasse="Hauptstrasse", nr="1", plz="3600", ort="Thun")
    api.employee_add(Book(b.root), "Lea", "Muster", monatslohn=5000, pensum=80, vollzeit_stunden_woche=40,
                     eintritt="2026-03-01", geburtsdatum="1995-05-04", ahv_nr="756.1234.5678.97",
                     strasse="Seeweg", nr="2", plz="3600", ort="Thun")

    def W(fn, *a, **k):
        return api.write(Book(b.root), "t", fn, *a, **k)
    W(daten.set_options, feiertage_kanton="BE")
    W(daten.add_time, "2026-03-02", "M0001", "8", "Werkstatt", "", "", False, "Intern")
    W(daten.add_time, "2026-03-02", "M0001", "0.75", "Reisezeit", "", "", False, "Reisezeit")
    W(abwesenheit.add, "M0001", "Ferien", "2026-03-09", "2026-03-10")
    return Book(b.root)


def test_template_is_checked(book):
    with pytest.raises(BookError, match="keine Excel-Datei"):
        api.write(book, "t", azk.store_template, b"kein excel")
    bad = openpyxl.load_workbook(io.BytesIO(template()))
    bad["H_Label"]["C258"] = "06.00-19.00h"
    buf = io.BytesIO()
    bad.save(buf)
    with pytest.raises(BookError, match="anderes Format"):
        azk.inspect(buf.getvalue())
    with pytest.raises(BookError, match="Vorlage hochladen"):
        azk.fill(book, "M0001", 2026)


def test_filled_year_and_the_next_one(book):
    res = api.write(book, "t", azk.store_template, template())["ergebnis"]
    assert res["jahr"] == 2026 and res["variante"] == "ohne Abzug Reisezeit"
    data = azk.fill(Book(book.root), "M0001", 2026)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert 'fullCalcOnLoad="1"' in z.read("xl/workbook.xml").decode()
        assert "xl/calcChain.xml" not in z.namelist()
    wb = openpyxl.load_workbook(io.BytesIO(data))
    mrz, base = wb["MRZ"], wb["Basisblatt"]
    assert mrz["G24"].value == 8 and mrz["G26"].value == 0.75          # 2 March: work and travel
    assert mrz["N36"].value == 6.4 and mrz["O36"].value == 6.4        # Ferien 9/10 March: 80 % of 8 h
    assert mrz["AL28"].value == "=SUM(F28:AJ28)"                       # formulas stay
    assert base["G3"].value == "Test GmbH" and base["G6"].value == "Lea Muster"
    assert base["G9"].value == 7561234567897 and base["E43"].value == 80
    assert base["E24"].value == "x" and base["H24"].value == "x"      # Karfreitag 3.4., Ostermontag 6.4.2026
    # 2027: the association has not published it yet → made from 2026
    data27 = azk.fill(Book(book.root), "M0001", 2027)
    wb27 = openpyxl.load_workbook(io.BytesIO(data27))
    assert wb27["Admin_Konfig"]["F3"].value == 2027
    assert wb27["Basisblatt"]["AB23"].value == "x" and wb27["Basisblatt"]["AE23"].value == "x"   # 26./29.3.2027
    months = azk.summary(Book(book.root), "M0001", 2026)
    assert months[2]["arbeit"] == Decimal("8") and months[2]["reise"] == Decimal("0.75")


def test_cells_keep_their_style_and_rows_are_added_in_order():
    xml = ('<worksheet><sheetData><row r="2"><c r="B2" s="5"><f>A1</f><v>1</v></c><c r="D2" s="6"/></row>'
           '<row r="9"/></sheetData></worksheet>')
    out = azk.set_cells(xml, {"B2": 3, "C2": "x & y", "A5": Decimal("1.5"), "D9": 2})
    assert '<c r="B2" s="5"><v>3</v></c><c r="C2" t="inlineStr"><is><t xml:space="preserve">x &amp; y</t></is></c><c r="D2" s="6"/>' in out
    assert out.index('<row r="2"') < out.index('<row r="5">') < out.index('<row r="9"')
    assert '<row r="9"><c r="D9"><v>2</v></c></row>' in out


def test_page(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert "Noch keine Vorlage" in c.get("/p/leistungen/arbeitszeit?jahr=2026").text
        r = c.post("/p/leistungen/arbeitszeit/vorlage", files={"datei": ("azk.xlsx", template(), "application/octet-stream")},
                   data={"jahr": "2027"}, headers=h)
        assert r.status_code in (200, 204), r.text
        assert (book.root / "leistungen/azk/vorlage-2027.xlsx").exists()
        page = c.get("/p/leistungen/arbeitszeit?jahr=2026&wer=M0001").text
        assert "Lea Muster" in page and "je Monat" in page
        r = c.post("/p/leistungen/arbeitszeit/excel", data={"wer": "M0001", "jahr": "2027"}, headers=h)
        assert r.status_code in (200, 204), r.text
        assert "Arbeitszeitkontrolle" in r.headers.get("HX-Redirect", "")

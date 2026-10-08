"""Bookings from a spreadsheet: the Excel template round-trips into grid rows, which then book."""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest

from aeradex import api
from aeradex.book import Book, BookError

openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Weg", nr="1", plz="3000", ort="Bern")
    return Book(root)


def test_template_filled_in_reads_into_grid_rows_and_books(book):
    wb = openpyxl.load_workbook(io.BytesIO(api.journal_template(book)))
    assert wb.sheetnames[:2] == ["Buchungen", "Konten"] and "Anleitung" in wb.sheetnames
    ws = wb["Buchungen"]
    assert [c.value for c in ws[1]][:5] == ["Datum", "Beschreibung", "Soll", "Haben", "Betrag"]
    konten = [r[0] for r in wb["Konten"].iter_rows(min_row=2, values_only=True)]
    assert any(k.startswith("1020 ") for k in konten)
    ws.delete_rows(2)                                       # the example row
    ws.append([date(2026, 3, 5), "Büromaterial", "6500 Büromaterial", "1020 Bank", 45.8])
    ws.append([None, "Porto", 6500, 1020, "12,50"])         # numbers as numbers, empty date = as above
    buf = io.BytesIO()
    wb.save(buf)

    res = api.journal_import_read(book, buf.getvalue(), "Buchungen.xlsx")
    assert res["zeilen"] == [["05.03.2026", "Büromaterial", "6500 Büromaterial", "1020 Bank", "45.8", ""],
                             ["", "Porto", "6500", "1020", "12,50", ""]]

    entries = [{"datum": "2026-03-05", "text": z[1], "soll": z[2].split(" ")[0], "haben": z[3].split(" ")[0],
                "betrag": z[4].replace(",", "."), "mwst": ""} for z in res["zeilen"]]
    api.post_entries(book, entries)
    rows = [r for r in Book(book.root).rows if r.datum == date(2026, 3, 5)]
    assert sorted(str(r.betrag) for r in rows) == ["12.50", "45.80"]


def test_csv_with_title_line_and_swiss_formats(book):
    data = ("Buchungen März\n"
            "Datum;Text;Soll;Haben;Betrag\n"
            "05.03.2026;Miete;6000;1020;1'800.00\n"
            ";;;;\n").encode()
    res = api.journal_import_read(book, data, "export.csv")
    assert res["zeilen"] == [["05.03.2026", "Miete", "6000", "1020", "1'800.00", ""]]


def test_unknown_layout_and_old_excel_are_explained(book):
    with pytest.raises(BookError, match="Kopfzeile"):
        api.journal_import_read(book, b"a;b;c\n1;2;3\n", "x.csv")
    with pytest.raises(BookError, match=r"\.xlsx"):
        api.journal_import_read(book, b"\xd0\xcf", "alt.xls")

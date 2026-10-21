"""Datensicherung: every kind of data as CSV/PDF and originals, one by one or as a ZIP with hashes — and no file of
a book with every feature in use is left out (a new feature that stores data must add its Datensatz)."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from aeradex import api, datensicherung
from aeradex.book import Book
from aeradex.testing import make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, iban=IBAN, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", git=True,
                  eroeffnung={"1020": 30000, "2800": -30000}, plugins=["anlagen", "leistungen"])
    root = b.root
    api.customer_add(b, name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 2, "preis": "500"}], "2026-01-10")
    api.invoice_create(Book(root), "K0001", [{"text": "Fassade", "menge": 1, "preis": "800"}], "2026-01-12")
    api.invoice_pay(Book(root), "R-2026-0002", None, "2026-01-30")
    api.reminder_create(Book(root), ["R-2026-0001"], "2026-02-20")
    api.supplier_add(Book(root), name="Farben AG", iban="CH7409000000300012345", konto="4000")
    api.bill_add(Book(root), "L0001", "430.00", datum="2026-02-03")
    api.post_entry(Book(root), "2026-02-05", "6500", "1020", "45.80", "Papeterie")
    api.employee_add(Book(root), "Lea", "Muster", eintritt="2026-01-01", monatslohn=5000)
    api.expense_add(Book(root), "M0001", "2026-03-10", "Zugbillett", "80.00", "5820")
    api.payroll_run(Book(root), "2026-03")
    api.payslip_close(Book(root), "2026-03", "M0001")
    api.budget_set(Book(root), 2026, "3400", "60000")
    xml = statement(IBAN, "30000.00", [(entry("12.00", "DBIT", "2026-03-31", party="UBS", ustrd="Kontoführung",
                                              acct_ref="B-1"), "-12.00")], "2026-03-31", "2026-03-31", stmt_id="S1")
    (tmp_path / "s1.xml").write_bytes(xml)
    api.bank_import(Book(root), str(tmp_path / "s1.xml"))
    from aeradex_anlagen import anlagen
    api.write(Book(root), "Anlage", anlagen.add, "Spritzgerät", "2026-02-01", "4500", "", "1500", nutzungsdauer="5")
    return Book(root)


def test_every_file_of_the_book_is_covered(book):
    assert datensicherung.ungesichert(book) == []
    keys = {d["key"] for d in datensicherung.overview(book)["datensaetze"]}
    assert {"journal", "kunden", "rechnungen", "mahnungen", "lieferanten", "kreditoren", "bank", "personal",
            "lohnabrechnungen", "spesen", "budget", "einstellungen", "anlagen"} <= keys


def test_every_datensatz_builds_in_every_format(book):
    for d in datensicherung.datensaetze(book):
        for fmt in datensicherung.formats(d):
            for jahr in ([2026] if d.jahresweise else [None]):
                files = datensicherung._files(book, d, jahr, fmt)
                for name, data in files:
                    if fmt == "pdf":
                        assert data[:4] == b"%PDF", (d.key, name)
                    if fmt == "csv":
                        assert data.decode("utf-8-sig").splitlines(), (d.key, name)


def test_content_of_the_master_data(book):
    name, data = datensicherung.build_one(book, "kunden", "csv")
    text = data.decode("utf-8-sig")
    assert name == "Kunden.csv" and "Kunde AG" in text and "Bern" in text
    text = datensicherung.build_one(book, "rechnungen", "csv", 2026)[1].decode("utf-8-sig")
    assert "R-2026-0001" in text and "R-2026-0002" in text
    text = datensicherung.build_one(book, "personal", "csv")[1].decode("utf-8-sig")
    assert "Muster" in text
    name, data = datensicherung.build_one(book, "lohnabrechnungen", "pdf", 2026)
    assert name.endswith(".pdf") and data[:4] == b"%PDF"
    name, data = datensicherung.build_one(book, "rechnungen", "dateien", 2026)
    assert name.endswith(".zip") and any(n.startswith("rechnungen/") for n in zipfile.ZipFile(io.BytesIO(data)).namelist())


def test_zip_with_contents_manifest_and_hashes(book):
    name, data, manifest = datensicherung.build(book, originale_mit=True)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    folder = names[0].split("/")[0]
    assert f"{folder}/00 Inhalt.pdf" in names and f"{folder}/manifest.json" in names
    m = json.loads(z.read(f"{folder}/manifest.json"))
    for rel, digest in m["dateien"].items():
        assert hashlib.sha256(z.read(f"{folder}/{rel}")).hexdigest() == digest
    assert any("/Verkauf/Kunden/Kunden.csv" in n for n in names)
    assert any("/Lohn/Lohnabrechnungen/2026/" in n for n in names)
    assert any("/Originale/" in n for n in names)
    _, data, m = datensicherung.build(book, ["journal", "kunden"], 2026, ["csv"])
    assert m["datensaetze"] == ["journal", "kunden"] and all(not k.endswith(".pdf") or k == "00 Inhalt.pdf"
                                                             for k in m["dateien"])


def test_a_new_data_folder_is_reported(book):
    (book.root / "neu").mkdir()
    (book.root / "neu" / "daten.yaml").write_text("x: 1\n")
    assert datensicherung.ungesichert(Book(book.root)) == ["neu/daten.yaml"]
    _, data, _ = datensicherung.build(Book(book.root), formate=["csv"])
    assert any(n.endswith("Rohdaten/neu/daten.yaml") for n in zipfile.ZipFile(io.BytesIO(data)).namelist())


def test_page_and_downloads(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/datensicherung").text
        assert "Lieferantenrechnungen" in page and "vertraulich" in page and ".aeradex" in page
        r = c.get("/datensicherung/datei?key=kunden&format=csv")
        assert r.status_code == 200 and "text/csv" in r.headers["content-type"] and b"Kunde AG" in r.content
        r = c.get("/datensicherung/zip?jahr=2026&key=journal&key=kunden&format=csv")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert "Mitarbeitende" in c.get("/datensicherung?jahr=2026").text
        assert c.get("/datensicherung/datei?key=gibtsnicht").status_code == 400

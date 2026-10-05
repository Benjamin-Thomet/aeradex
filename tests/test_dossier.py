"""Abschlussunterlagen: every part builds, the ZIP is complete and hashed, and the
Belegordner stamps each page with the Beleg number of its booking — also after
cancellations, voided documents and rejected proposals."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from allkvitt import api, check, dossier, gitlog
from allkvitt.book import Book, BookError

STAMP = re.compile(r"Beleg (\S+) · (\d\d\.\d\d\.\d{4}) · CHF")


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    text = (root / "kontenplan.yaml").read_text()
    text = text.replace('{nr: "1020", name: Bank, klasse: aktiv}',
                        '{nr: "1020", name: Bank, klasse: aktiv, eroeffnung: 20000}')
    text = text.replace('{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv}',
                        '{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv, eroeffnung: -20000}')
    (root / "kontenplan.yaml").write_text(text)
    gitlog.commit(root, "Eröffnung")
    return Book(root)


def receipt_pdf(path: Path, text: str, pages: int = 1) -> Path:
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(100, 700, f"{text} Seite {i + 1}")
        c.showPage()
    c.save()
    return path


def receipt_png(path: Path) -> Path:
    from PIL import Image
    Image.new("RGB", (400, 600), "white").save(path)
    return path


def stamps(data: bytes) -> list[tuple[str, str]]:
    """(stamp beleg, page text) for every stamped page."""
    out = []
    for page in PdfReader(io.BytesIO(data)).pages:
        text = page.extract_text() or ""
        m = STAMP.search(text)
        if m:
            out.append((m.group(1), text))
    return out


def booked(book: Book, root: Path) -> Book:
    """Three bookings with receipts: a two-page PDF, a photo, and one without a file."""
    inbox = root / "inbox"
    api.post_entry(book, "2026-01-05", "6500", "1020", "45.80", "Papeterie",
             datei=str(receipt_pdf(inbox / "papeterie.pdf", "QUITTUNG PAPETERIE", pages=2)))
    api.post_entry(Book(root), "2026-01-31", "6000", "1020", "1800", "Miete Januar",
             datei=str(receipt_png(inbox / "miete.png")))
    api.post_entry(Book(root), "2026-02-03", "6640", "1020", "120", "Bahnbillett")
    return Book(root)


def test_belegordner_stamps_every_page_with_the_booking_number(book):
    b = booked(book, book.root)
    data = dossier.belegordner_pdf(dossier.Kontext(b, 2026))
    found = stamps(data)
    assert [s for s, _ in found] == ["26-001", "26-001", "26-002", "26-003"]
    assert all("QUITTUNG PAPETERIE" in text for s, text in found if s == "26-001")
    assert "Seite 2/2" in found[1][1]
    assert "Keine Belegdatei vorhanden" in found[3][1]
    outline = [o.title for o in PdfReader(io.BytesIO(data)).outline]
    assert outline[0] == "Belegverzeichnis" and outline[1].startswith("26-001")


def test_numbers_stay_after_storno_rejection_and_removal(book):
    b = booked(book, book.root)
    before = dict(stamps(dossier.belegordner_pdf(dossier.Kontext(b, 2026))))
    # a rejected proposal reserves 26-004 and leaves it unused
    api.propose(Book(b.root), "2026-02-10", "6500", "1020", "10", "Vorschlag", "Test")
    api.reject(Book(b.root), ["V-001"])
    # the Bahnbillett is cancelled: 26-005 is the counter entry, 26-003 stays
    api.reverse_entry(Book(b.root), "26-003", "2026-02-11")
    # someone removes 26-002 from the journal by hand and commits it
    b = Book(b.root)
    paths = b.remove_rows(lambda r: r.beleg == "26-002")
    gitlog.commit(b.root, "Miete Januar entfernt", paths)
    b = Book(b.root)
    after = stamps(dossier.belegordner_pdf(dossier.Kontext(b, 2026)))
    numbers = [s for s, _ in after]
    assert numbers == ["26-001", "26-001", "26-003", "26-005"]
    assert dict(after)["26-001"] == before["26-001"]
    gaps = {g["beleg"]: g for g in dossier.belegluecken(b, 2026)}
    assert gaps["26-002"]["art"] == "entfernt" and "Miete Januar entfernt" in gaps["26-002"]["grund"]
    assert gaps["26-004"]["art"] == "verworfen"
    assert set(gaps) == {"26-002", "26-004"}
    # the receipt of the removed booking is now an orphan: a warning, and next_beleg never reuses 26-002
    warnings = [str(i) for i in check.run(b) if i.level == "warnung"]
    assert any("belege/2026/26-002 miete.png" in w for w in warnings)
    api.post_entry(b, "2026-03-01", "6500", "1020", "5", "Neu")
    assert Book(b.root).rows[-1].beleg == "26-006"


def test_voided_invoice_is_explained(book):
    api.customer_add(book, name="Anna")
    for _ in range(2):
        api.invoice_create(Book(book.root), "K0001", [{"text": "X", "menge": 1, "preis": "100"}], "2026-02-10")
    api.invoice_void(Book(book.root), "R-2026-0001", "Doppelt")
    gaps = dossier.belegluecken(Book(book.root), 2026)
    assert gaps == [{"beleg": "R-2026-0001", "art": "storniert", "grund": gaps[0]["grund"]}]
    assert "Doppelt" in gaps[0]["grund"]
    assert not [i for i in check.run(Book(book.root)) if i.level in ("fehler", "warnung")]


def test_zip_contains_every_part_with_hashes(book):
    b = booked(book, book.root)
    name, data, manifest = dossier.build_zip(b, 2026)
    assert name == "Test GmbH Abschluss 2026.zip"
    z = zipfile.ZipFile(io.BytesIO(data))
    folder = "Test GmbH Abschluss 2026/"
    names = {n[len(folder):] for n in z.namelist()}
    for expected in ("00 Inhalt.pdf", "01 Jahresrechnung 2026.pdf", "01 Bilanz 2026.csv", "01 Erfolgsrechnung 2026.csv",
                     "02 Saldenliste 2026.pdf", "02 Saldenliste 2026.csv", "03 Journal 2026.pdf", "03 Journal 2026.csv",
                     "04 Kontoblätter 2026.pdf", "04 Kontoblätter 2026.csv", "05 Belegordner 2026.pdf",
                     "05 Belegverzeichnis 2026.csv", "05 Belege/26-001 papeterie.pdf", "05 Belege/26-002 miete.png",
                     "07 Offene Debitoren 31.12.2026.pdf", "09 Kontenplan.csv", "manifest.json"):
        assert expected in names, expected
    assert not any(n.startswith(("06 ", "08 ")) for n in names)       # no MWST, no payroll in this book
    saved = json.loads(z.read(folder + "manifest.json"))
    assert saved["entwurf"] is True and saved["jahr"] == 2026
    assert set(saved["dateien"]) == names - {"manifest.json"}
    for n, h in saved["dateien"].items():
        assert hashlib.sha256(z.read(folder + n)).hexdigest() == h
    journal_csv = z.read(folder + "03 Journal 2026.csv").decode("utf-8")
    assert journal_csv.startswith("﻿Datum;Beleg;Text;Soll;Haben;Betrag CHF")
    assert "2026-01-05;26-001;Papeterie;6500;1020;45.80" in journal_csv


def test_selection_single_parts_and_lock(book):
    b = booked(book, book.root)
    _, data, manifest = dossier.build_zip(b, 2026, ["journal"], ["csv"])
    assert set(manifest["dateien"]) == {"00 Inhalt.pdf", "03 Journal 2026.csv"}
    name, part = dossier.build_part(b, 2026, "saldenliste", "csv")
    assert name == "Saldenliste 2026.csv" and b"6500" in part
    name, part = dossier.build_part(b, 2026, "belege", "dateien")
    assert name.endswith(".zip") and len(zipfile.ZipFile(io.BytesIO(part)).namelist()) == 2
    with pytest.raises(BookError):
        dossier.build_part(b, 2026, "journal", "xlsx")
    with pytest.raises(BookError):
        dossier.build_part(b, 2026, "gibtsnicht", "pdf")
    assert "ENTWURF" in PdfReader(io.BytesIO(dossier.build_part(b, 2026, "journal", "pdf")[1])).pages[0].extract_text()
    api.lock(b, "2026-12-31")
    k = dossier.Kontext(Book(b.root), 2026)
    assert not k.entwurf
    text = PdfReader(io.BytesIO(dossier.build_part(Book(b.root), 2026, "journal", "pdf")[1])).pages[0].extract_text()
    assert "ENTWURF" not in text


def test_api_writes_to_berichte(book):
    b = booked(book, book.root)
    res = api.dossier(b, 2026, ["kontenplan"], ["pdf"])
    assert res["ok"] and Path(res["datei"]).exists() and "berichte" in res["datei"]
    assert api.dossier_overview(b, 2026)["teile"][0]["teil"] == "jahresrechnung"

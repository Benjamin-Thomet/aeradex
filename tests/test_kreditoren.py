"""Kreditoren: supplier bills from a scanned QR-bill, MWST, payment run (pain.001), payment booking."""
from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, check, kreditoren
from allkvitt.book import Book, BookError

D = Decimal
NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}


def errors(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level in ("fehler", "warnung")]


@pytest.fixture
def supplier_pdf(tmp_path: Path) -> Path:
    """A real QR-bill: issued by a second book acting as the supplier (QR-IBAN → QRR)."""
    root = tmp_path / "lieferant"
    api.init_book(root, "Papeterie Muster AG", 2026, strasse="Marktgasse", nr="14", plz="3011", ort="Bern",
                  iban="CH44 3199 9123 0008 8901 2", git=False)
    api.customer_add(Book(root), name="Test", firma="Test GmbH", strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    res = api.invoice_create(Book(root), "K0001", [{"text": "Druckerpapier", "menge": 5, "preis": "21.62"}], "2026-03-02")
    return root / res["pdf"]


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    return Book(root)


def test_scan_qr_bill_and_enter_it(book, supplier_pdf):
    shutil.copy(supplier_pdf, book.root / "inbox" / "rechnung.pdf")
    data = api.qr_scan(book, "inbox/rechnung.pdf")
    assert data["iban"] == "CH4431999123000889012" and data["referenz_typ"] == "QRR"
    assert data["betrag"] == "108.10" and data["kreditor"]["name"] == "Papeterie Muster AG"
    assert data["lieferant"] is None
    sup = api.supplier_add(Book(book.root), name=data["kreditor"]["name"], strasse=data["kreditor"]["strasse"],
                           nr=data["kreditor"]["nr"], plz=data["kreditor"]["plz"], ort=data["kreditor"]["ort"],
                           iban=data["iban"], konto="6500")["lieferant"]
    assert api.qr_scan(Book(book.root), "inbox/rechnung.pdf")["lieferant"] == sup["nummer"]
    res = api.bill_add(Book(book.root), sup["nummer"], data["betrag"], datum="2026-03-03", referenz=data["referenz"],
                       referenz_typ="QRR", datei="inbox/rechnung.pdf", rechnungsnr="R-2026-0001")
    nr = res["kreditor"]["nummer"]
    assert not (book.root / "inbox" / "rechnung.pdf").exists()
    b = Book(book.root)
    assert [(r.soll, r.haben, r.betrag) for r in b.rows] == [("6500", "2000", D("108.10"))]
    assert errors(book.root) == []
    assert api.payables(b)["total_offen"] == "108.10" and api.payables(b)["differenz"] == "0.00"


def test_bill_with_vorsteuer_and_payment_run(book):
    api.settings_update(book, mwst={"methode": "effektiv"})
    api.supplier_add(Book(book.root), name="Swisscom", strasse="Alte Tiefenaustrasse", nr="6", plz="3048",
                     ort="Worblaufen", iban="CH93 0076 2011 6238 5295 7", konto="6510", mwst="V81")
    api.bill_add(Book(book.root), "L0001", "59.00", datum="2026-03-10", rechnungsnr="123456",
                 referenz="RF18539007547034")
    api.bill_add(Book(book.root), "L0001", "59.00", datum="2026-04-10", rechnungsnr="123457")
    b = Book(book.root)
    assert sum(r.betrag for r in b.rows if r.soll == "1170") == D("8.84")
    with pytest.raises(BookError):        # a QR-IBAN cannot be debited
        api.settings_update(Book(book.root), iban="CH44 3199 9123 0008 8901 2")
        api.payment_run(Book(book.root), ["E-2026-0001"], "2026-04-20")
    api.settings_update(Book(book.root), iban="CH93 0076 2011 6238 5295 7")
    res = api.payment_run(Book(book.root), ["E-2026-0001", "E-2026-0002"], "2026-04-20")
    xml = (book.root / res["zahlungslauf"]["datei"]).read_bytes()
    doc = ET.fromstring(xml)
    assert doc.find(".//p:GrpHdr/p:NbOfTxs", NS).text == "2"
    assert doc.find(".//p:GrpHdr/p:CtrlSum", NS).text == "118.00"
    assert doc.find(".//p:ReqdExctnDt/p:Dt", NS).text == "2026-04-20"
    refs = [e.text for e in doc.findall(".//p:CdtrRefInf/p:Ref", NS)]
    assert refs == ["RF18539007547034"]
    assert doc.findall(".//p:RmtInf/p:Ustrd", NS)[0].text == "Rechnung 123457"
    assert {s["status"] for s in api.bill_list(Book(book.root))} == {"angewiesen"}
    api.payment_run_book(Book(book.root), res["zahlungslauf"]["datei"], "2026-04-20")
    assert {s["status"] for s in api.bill_list(Book(book.root))} == {"bezahlt"}
    assert api.payables(Book(book.root))["total_offen"] == "0.00"
    assert errors(book.root) == []


def test_bad_references_are_refused(book):
    api.supplier_add(book, name="X", iban="CH4431999123000889012", konto="6500")
    with pytest.raises(BookError):        # QR-IBAN needs a valid QRR reference
        api.bill_add(Book(book.root), "L0001", "10", referenz="210000000003139471430009018", referenz_typ="QRR")
    with pytest.raises(BookError):
        api.bill_add(Book(book.root), "L0001", "10")


def test_void_and_tamper(book):
    api.supplier_add(book, name="Y", iban="CH93 0076 2011 6238 5295 7", konto="6500")
    api.bill_add(Book(book.root), "L0001", "100", datum="2026-02-01")
    path = next((book.root / "kreditoren" / "2026").glob("E-*.md"))
    path.write_text(path.read_text().replace("betrag: 100.00", "betrag: 90.00"))
    assert any("Fingerprint" in e for e in errors(book.root))
    path.write_text(path.read_text().replace("betrag: 90.00", "betrag: 100.00"))
    api.bill_void(Book(book.root), "E-2026-0001", "doppelt")
    assert Book(book.root).rows == [] and errors(book.root) == []


def test_pain001_validates_against_official_schema(book):
    """Set ALLKVITT_SPS_XSD to SIX's pain.001.001.09.ch.03.xsd (SPS download centre) to run."""
    import os
    xsd = os.environ.get("ALLKVITT_SPS_XSD")
    if not xsd or not Path(xsd).exists():
        pytest.skip("ALLKVITT_SPS_XSD nicht gesetzt")
    from lxml import etree
    from allkvitt import qrbill_ch as qr
    api.supplier_add(book, name="Papeterie Muster AG", strasse="Marktgasse", nr="14", plz="3011", ort="Bern",
                     iban="CH4431999123000889012", konto="6500")
    api.supplier_add(Book(book.root), name="Swisscom", strasse="Weg", nr="6", plz="3048", ort="Worblaufen",
                     iban="CH5604835012345678009", konto="6510")
    api.bill_add(Book(book.root), "L0001", "108.10", referenz=qr.make_qrr_reference("4711"), referenz_typ="QRR")
    api.bill_add(Book(book.root), "L0002", "59.00", referenz="RF18539007547034")
    api.bill_add(Book(book.root), "L0002", "12.50", rechnungsnr="A-77")
    res = api.payment_run(Book(book.root), ["E-2026-0001", "E-2026-0002", "E-2026-0003"], "2026-12-20")
    schema = etree.XMLSchema(etree.parse(xsd))
    assert schema.validate(etree.parse(str(book.root / res["zahlungslauf"]["datei"]))), schema.error_log

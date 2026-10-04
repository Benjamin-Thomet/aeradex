"""Bank import (camt.053): automatic booking, reconciliation, open items, balance check."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from batzen import api, bank, check
from batzen.book import Book, BookError

from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


def problems(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level in ("fehler", "warnung")]


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", iban=IBAN)
    text = (root / "kontenplan.yaml").read_text()
    text = text.replace('{nr: "1020", name: Bank, klasse: aktiv}', '{nr: "1020", name: Bank, klasse: aktiv, eroeffnung: 20000}')
    text = text.replace('{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv}',
                        '{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv, eroeffnung: -20000}')
    (root / "kontenplan.yaml").write_text(text)
    b = Book(root)
    api.customer_add(b, name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    api.invoice_create(Book(root), "K0001", [{"text": "Beratung", "menge": 10, "preis": "150"}], "2026-03-02")
    api.supplier_add(Book(root), name="Swisscom", iban="CH5604835012345678009", konto="6510")
    api.bill_add(Book(root), "L0001", "59.00", datum="2026-03-05", referenz="RF18539007547034")
    api.payment_run(Book(root), ["E-2026-0001"], "2026-03-20")
    api.employee_add(Book(root), "Lea", "Muster", monatslohn=5000)
    api.payroll_run(Book(root), "2026-03")
    api.payslip_close(Book(root), "2026-03", "M0001")
    return Book(root)


def march_statement(b: Book) -> tuple[bytes, Decimal]:
    inv = api.invoice_list(b)[0]
    net = Decimal(api.payslip_show(b, "2026-03", "M0001")["werte"]["nettolohn"])
    items = [
        (entry("1500.00", "CRDT", "2026-03-10", ref=inv["referenz"], party="Kunde AG"), "1500.00"),
        (entry("59.00", "DBIT", "2026-03-20", e2e="E-2026-0001", party="Swisscom"), "-59.00"),
        (entry(f"{net:.2f}", "DBIT", "2026-04-01", party="Lea Muster", ustrd="Lohn März"), f"-{net:.2f}"),
        (entry("7.50", "DBIT", "2026-03-31", ustrd="Kontoführungsgebühr", acct_ref="FEE1"), "-7.50"),
        (entry("250.00", "CRDT", "2026-03-25", party="Unbekannt", ustrd="Rückerstattung"), "250.00"),
    ]
    return statement(IBAN, "20000.00", items, "2026-03-01", "2026-04-01"), net


def test_import_matches_books_and_reconciles(book):
    data, net = march_statement(book)
    xsd = os.environ.get("BATZEN_CAMT053_XSD")
    if xsd and Path(xsd).exists():
        from lxml import etree
        schema = etree.XMLSchema(etree.parse(xsd))
        assert schema.validate(etree.fromstring(data)), schema.error_log
    (book.root / "inbox" / "maerz.xml").write_bytes(data)
    res = api.bank_import(Book(book.root), "inbox/maerz.xml")["import_"]
    assert (res["neu"], res["gebucht"], res["abgeglichen"], res["offen"]) == (5, 2, 1, 2)
    b = Book(book.root)
    assert api.invoice_list(b)[0]["status"] == "bezahlt"
    assert api.bill_list(b)[0]["status"] == "bezahlt"
    assert not (book.root / "inbox" / "maerz.xml").exists()
    assert any("Differenz" in p for p in problems(book.root)) is False     # open items explain the gap
    open_items = {t["Betrag"]: t for t in api.bank_list(b, "offen")}
    fee, refund = open_items["-7.50"], open_items["250.00"]
    api.bank_book(Book(book.root), fee["ID"], "6940", "Kontoführung März")
    with pytest.raises(BookError):
        api.bank_assign(Book(book.root), refund["ID"], "E-2026-0001")        # a credit cannot pay a supplier
    api.bank_ignore(Book(book.root), refund["ID"], "Testbuchung der Bank, separat storniert")
    rec = api.bank_reconciliation(Book(book.root))[0]
    assert rec["differenz"] == "250.00"                                      # bank has the refund, the books not
    api.post_entry(Book(book.root), "2026-03-25", "1020", "3600", "250", "Rückerstattung")
    assert api.bank_reconciliation(Book(book.root))[0]["differenz"] == "0.00"
    # importing the same statement again changes nothing
    (book.root / "inbox" / "nochmal.xml").write_bytes(data)
    again = api.bank_import(Book(book.root), "inbox/nochmal.xml")["import_"]
    assert again["neu"] == 0 and again["doppelt"] == 5
    assert problems(book.root) == []


def test_proposal_for_bank_transaction(book):
    data, _ = march_statement(book)
    (book.root / "inbox" / "a.xml").write_bytes(data)
    api.bank_import(book, "inbox/a.xml")
    fee = [t for t in api.bank_list(Book(book.root), "offen") if t["Betrag"] == "-7.50"][0]
    with pytest.raises(BookError):
        api.propose(Book(book.root), "2026-03-31", "6940", "1020", "8.00", "Gebühr", "Bank", bank=fee["ID"])
    api.propose(Book(book.root), "2026-03-31", "6940", "1020", "7.50", "Kontoführung", "Bankgebühr", bank=fee["ID"])
    api.approve(Book(book.root), ["V-001"])
    tx = [t for t in api.bank_list(Book(book.root)) if t["ID"] == fee["ID"]][0]
    assert tx["Status"] == "gebucht" and tx["Beleg"]


def test_unknown_account_is_refused(book):
    data = statement("CH5604835012345678009", "0", [(entry("1.00", "CRDT", "2026-03-02"), "1.00")],
                     "2026-03-01", "2026-03-31")
    (book.root / "inbox" / "x.xml").write_bytes(data)
    with pytest.raises(BookError, match="keinem Buchhaltungskonto"):
        api.bank_import(book, "inbox/x.xml")
    api.settings_update(Book(book.root), bankkonten={"CH56 0483 5012 3456 7800 9": "1000"})
    assert api.bank_import(Book(book.root), "inbox/x.xml")["import_"]["neu"] == 1


def test_older_camt_version_parses():
    data = statement(IBAN, "0", [(entry("5.00", "CRDT", "2026-01-02", ustrd="x"), "5.00")], "2026-01-01", "2026-01-31")
    old = data.replace(b"camt.053.001.08", b"camt.053.001.04").replace(b"<Pty><Nm>", b"<Nm>").replace(b"</Nm></Pty>", b"</Nm>")
    stmt = bank.parse(old)[0]
    assert stmt["iban"] == IBAN and stmt["buchungen"][0]["betrag"] == Decimal("5.00")

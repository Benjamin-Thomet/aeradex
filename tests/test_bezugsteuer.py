"""Bezugsteuer (Art. 45 MWSTG): services from abroad, both methods, Abrechnung and eMWST."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, mwst
from allkvitt.book import Book
from allkvitt.testing import assert_clean, make_book

DE_IBAN = "DE89370400440532013000"


def book_with(tmp_path, **cfg):
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, uid="CHE-123.456.789",
                  iban="CH93 0076 2011 6238 5295 7")
    api.settings_update(b, mwst=cfg)
    api.supplier_add(Book(b.root), name="Cloud Services GmbH", iban=DE_IBAN, konto="6570", land="DE")
    return Book(b.root)


def saldo(book, konto):
    return Decimal(api.ledger(Book(book.root), konto, 2026)["saldo"])


def validate(xml: bytes):
    xsd = os.environ.get("ALLKVITT_ECH0217_XSD")
    if xsd and Path(xsd).exists():
        from lxml import etree
        etree.XMLSchema(etree.parse(xsd)).assertValid(etree.fromstring(xml))


def test_effective_method_owes_and_deducts(tmp_path):
    book = book_with(tmp_path, methode="effektiv")
    api.bill_add(book, "L0001", "1000.00", datum="2026-02-10", mwst="B81")
    b = Book(book.root)
    assert saldo(b, "6570") == Decimal("1000.00") and saldo(b, "2000") == Decimal("-1000.00")
    assert saldo(b, "1171") == Decimal("81.00") and saldo(b, "2200") == Decimal("-81.00")
    rep = mwst.report(b, "2026-Q1")
    z = rep["ziffern"]
    assert z["382"] == Decimal("1000.00") and z["382_steuer"] == Decimal("81.00")
    assert z["399"] == Decimal("81.00") and z["405"] == Decimal("81.00") and rep["zahllast"] == 0
    assert_clean(b)
    xml = mwst.ech0217(b, "2026-Q1")
    assert b"<eCH-0217:acquisitionTax><eCH-0217:taxRate>8.10</eCH-0217:taxRate><eCH-0217:turnover>1000.00" in xml
    assert b"<eCH-0217:payableTax>0.00</eCH-0217:payableTax>" in xml
    validate(xml)
    api.mwst_book(b, "2026-Q1")
    b = Book(book.root)
    assert saldo(b, "2200") == 0 and saldo(b, "1171") == 0 and saldo(b, "2201") == 0
    assert_clean(b)


def test_material_account_deducts_on_400(tmp_path):
    book = book_with(tmp_path, methode="effektiv")
    api.bill_add(book, "L0001", "500.00", datum="2026-02-10", konto="4400", mwst="B81")
    z = mwst.report(Book(book.root), "2026-Q1")["ziffern"]
    assert z["400"] == Decimal("40.50") and not z["405"]


def test_saldo_method_owes_without_deduction(tmp_path):
    book = book_with(tmp_path, methode="saldo", saldosteuersatz="6.2", taetigkeit="12345")
    api.invoice_create  # (no sales needed)
    api.bill_add(book, "L0001", "1000.00", datum="2026-02-10", mwst="B81")
    b = Book(book.root)
    assert saldo(b, "6570") == Decimal("1081.00")             # the tax is a cost
    rep = mwst.report(b, "2026-S1")
    assert rep["ziffern"]["382"] == Decimal("1000.00") and rep["zahllast"] == Decimal("81.00")
    xml = mwst.ech0217(b, "2026-S1")
    assert b"acquisitionTax" in xml and b"<eCH-0217:payableTax>81.00</eCH-0217:payableTax>" in xml
    validate(xml)
    api.mwst_book(b, "2026-S1")
    b = Book(book.root)
    assert saldo(b, "2200") == 0 and saldo(b, "2201") == Decimal("-81.00")
    assert_clean(b)


def test_foreign_currency_bill_with_bezugsteuer(tmp_path, monkeypatch):
    from allkvitt import fx
    monkeypatch.setattr(fx, "_get", lambda url: b'<wechselkurse><datum>x</datum><devise code="eur"><waehrung>1 EUR</waehrung><kurs>0.95</kurs></devise></wechselkurse>')
    book = book_with(tmp_path, methode="effektiv")
    api.bill_add(book, "L0001", "1000.00", datum="2026-02-10", waehrung="EUR",
                 positionen=[{"konto": "6570", "betrag": "1000.00", "mwst": "B81"}])
    b = Book(book.root)
    assert saldo(b, "6570") == Decimal("950.00") and saldo(b, "2200") == Decimal("-76.95")
    assert mwst.report(b, "2026-Q1")["ziffern"]["382"] == Decimal("950.00")
    assert_clean(b)

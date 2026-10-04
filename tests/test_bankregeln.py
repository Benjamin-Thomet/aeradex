"""Bank rules: recurring movements booked on import, never twice, created from a booked movement."""
from __future__ import annotations

from decimal import Decimal

import pytest

from batzen import api, bank
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path):
    return make_book(tmp_path, iban=IBAN, eroeffnung={"1020": 10000, "2800": -10000}, strasse="Hauptstrasse", nr="1",
                     plz="3000", ort="Bern")


def stmt(tmp_path, name, items, day, opening="10000.00"):
    xml = statement(IBAN, opening, [(entry(a.lstrip("-"), "DBIT" if a.startswith("-") else "CRDT", day,
                                              party=p, ustrd=t, acct_ref=f"{name}-{i}"), a)
                                       for i, (a, p, t) in enumerate(items)], day, day, stmt_id=name)
    f = tmp_path / f"{name}.xml"
    f.write_bytes(xml)
    return str(f)


def test_rule_from_booked_movement_books_next_month(book, tmp_path):
    api.bank_import(book, stmt(tmp_path, "M1", [("-1800.00", "IMMOBILIEN MUSTER AG", "Miete März")], "2026-03-01"))
    tid = bank.transactions(Book(book.root))[0]["ID"]
    api.bank_book(Book(book.root), tid, "6000", "Miete März")
    out = api.bank_rule_from(Book(book.root), tid)
    assert out["regel"]["konto"] == "6000" and out["regel"]["richtung"] == "belastung"
    res = api.bank_import(Book(book.root), stmt(tmp_path, "M2", [("-1800.00", "IMMOBILIEN MUSTER AG", "Miete April"),
                                                                ("-12.00", "UBS", "Kontoführung")], "2026-04-01",
                                                "8200.00"))
    assert res["import_"]["regeln"] == 1 and res["import_"]["offen"] == 1
    b = Book(book.root)
    assert Decimal(api.ledger(b, "6000", 2026)["saldo"]) == Decimal("3600.00")
    april = [t for t in bank.transactions(b) if t["Datum"] == "2026-04-01" and t["Gegenpartei"].startswith("IMMO")][0]
    assert april["Status"] == "gebucht" and april["Hinweis"] == "Regel R1"
    assert_clean(b)


def test_text_rule_applies_to_open_movements(book, tmp_path):
    api.bank_import(book, stmt(tmp_path, "M1", [("-12.00", "UBS", "Kontoführung März")], "2026-03-31"))
    out = api.bank_rule_add(Book(book.root), "6940", text="Kontoführung", buchungstext="Bankspesen")
    assert len(out["gebucht"]) == 1
    assert Decimal(api.ledger(Book(book.root), "6940", 2026)["saldo"]) == Decimal("12.00")


def test_invoice_reference_wins_over_rule(book, tmp_path):
    api.customer_add(book, name="A", firma="Kunde AG", strasse="x", nr="1", plz="3000", ort="Bern")
    api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "500"}], "2026-03-01")
    api.bank_rule_add(Book(book.root), "3400", gegenpartei="KUNDE AG")           # a careless rule
    res = api.bank_import(Book(book.root), stmt(tmp_path, "P", [("500.00", "KUNDE AG", "Rechnung R-2026-0001")],
                                                "2026-03-20"))
    assert res["import_"]["gebucht"] == 1 and not res["import_"].get("regeln")   # paid the invoice, no extra revenue
    assert Decimal(api.ledger(Book(book.root), "3400", 2026)["saldo"]) == Decimal("-500.00")


def test_rule_needs_a_criterion_and_known_account(book):
    with pytest.raises(BookError, match="Gegenpartei oder einen Text"):
        api.bank_rule_add(book, "6940")
    with pytest.raises(BookError):
        api.bank_rule_add(Book(book.root), "9999", text="x")

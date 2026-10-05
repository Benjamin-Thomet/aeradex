"""Bank import for a foreign-currency account (camt.053 in EUR)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from allkvitt import api, bank, fx, invoices
from allkvitt.book import Book, BookError
from allkvitt.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"
EUR_IBAN = "CH5800791123000889012"
DAILY = {"20260302": "0.95", "20260415": "0.96", "20260416": "0.96"}


def bazg(url):
    d = url.split("d=")[1][:8]
    return (f'<wechselkurse><datum>x</datum><devise code="eur"><waehrung>1 EUR</waehrung>'
            f'<kurs>{DAILY.get(d, "0.95")}</kurs></devise></wechselkurse>').encode()


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "_get", bazg)
    b = make_book(tmp_path, iban=IBAN, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  eroeffnung={"1020": 5000, "2800": -5000})
    api.add_account(b, "1025", "Bank EUR", "aktiv", waehrung="EUR")
    api.settings_update(Book(b.root), bankkonten={EUR_IBAN: "1025"})
    api.post_entry(Book(b.root), "2026-03-01", "1025", "1020", "2000", "Umbuchung", waehrung="EUR", kurs="0.95")
    return Book(b.root)


def eur_statement(tmp_path, name, opening, items, day):
    xml = statement(EUR_IBAN, opening, [(entry(a.lstrip("-"), "DBIT" if a.startswith("-") else "CRDT", day, party=p,
                                               ustrd=t, acct_ref=f"{name}{i}"), a) for i, (a, p, t) in enumerate(items)],
                    day, day, stmt_id=name).decode().replace('Ccy="CHF"', 'Ccy="EUR"').replace("<Ccy>CHF</Ccy>", "<Ccy>EUR</Ccy>")
    f = tmp_path / f"{name}.xml"
    f.write_text(xml)
    return str(f)


def test_eur_account_import_rules_and_reconciliation(book, tmp_path):
    api.bank_rule_add(book, "6570", gegenpartei="HOSTER")
    res = api.bank_import(Book(book.root), eur_statement(tmp_path, "E1", "2000.00",
                                                         [("-50.00", "HOSTER GMBH", "Server April")], "2026-04-15"))
    assert res["import_"]["regeln"] == 1
    b = Book(book.root)
    led = api.ledger(b, "1025", 2026)
    assert led["saldo_fw"] == "1950.00"
    assert Decimal(api.ledger(b, "6570", 2026)["saldo"]) == Decimal("48.00")          # 50 EUR × 0.96
    rec = bank.reconciliation(b)
    assert rec and all(Decimal(str(r["differenz"])) == 0 for r in rec)
    assert_clean(b)


def test_eur_invoice_paid_into_eur_account(book, tmp_path):
    api.customer_add(book, name="Max", firma="Kunde GmbH", strasse="Allee", nr="2", plz="10115", ort="Berlin", land="DE")
    meta = api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 10, "preis": "100"}],
                              "2026-03-02", waehrung="EUR")["rechnung"]
    res = api.bank_import(Book(book.root), eur_statement(tmp_path, "E2", "2000.00",
                                                         [("1000.00", "KUNDE GMBH", f"Rechnung {meta['nummer']}")],
                                                         "2026-04-15"))
    assert res["import_"]["gebucht"] == 1
    b = Book(book.root)
    assert invoices.invoice_state(b, invoices.invoice(b, meta["nummer"]))["status"] == "bezahlt"
    assert api.ledger(b, "1025", 2026)["saldo_fw"] == "3000.00"
    assert Decimal(api.ledger(b, "6952", 2026)["saldo"]) == Decimal("-10.00")          # 960 received, 950 booked
    assert_clean(b)


def test_statement_currency_must_match_account(book, tmp_path):
    api.settings_update(book, bankkonten={EUR_IBAN: "1020"})
    with pytest.raises(BookError, match="Auszug ist in EUR"):
        api.bank_import(Book(book.root), eur_statement(tmp_path, "E3", "0.00", [("-1.00", "X", "y")], "2026-04-15"))


def test_manual_booking_of_eur_movement(book, tmp_path):
    api.bank_import(book, eur_statement(tmp_path, "E4", "2000.00", [("-20.00", "BANK", "Spesen")], "2026-04-16"))
    tid = bank.transactions(Book(book.root))[0]["ID"]
    api.bank_book(Book(book.root), tid, "6940", "Bankspesen EUR")
    assert Decimal(api.ledger(Book(book.root), "6940", 2026)["saldo"]) == Decimal("19.20")
    assert_clean(Book(book.root))

"""Bank reconciliation page (Xero style): rules as suggestions, search & match, receipts without an
account pointed to their review page, and the page itself."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, bank
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path: Path) -> Book:
    b = make_book(tmp_path, eroeffnung={"1020": 30000, "2800": -30000}, iban=IBAN,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    root = b.root
    (root / "inbox").mkdir(exist_ok=True)
    api.settings_update(Book(root), kreditoren={"agent_automatisch": False})
    api.customer_add(Book(root), name="Peter Privat", strasse="Seeweg", nr="1", plz="3600", ort="Thun")
    api.invoice_create(Book(root), "K0001", [{"text": "Steuererklärung", "menge": 1, "preis": "450"}], "2026-02-03")
    api.supplier_add(Book(root), name="Druckerei Beispiel AG", iban="CH7409000000300012345", konto="6600")
    api.bill_add(Book(root), "L0001", "486.45", datum="2026-02-10", rechnungsnr="D-7781")
    return Book(root)


def import_(book: Book, tmp_path: Path, items, name: str = "feb") -> dict:
    f = tmp_path / f"{name}.xml"
    f.write_bytes(statement(IBAN, "30000.00", items, "2026-02-01", "2026-02-28", stmt_id=name.upper()))
    return api.bank_import(Book(book.root), str(f))


def tx_by_amount(book: Book, amount: str) -> dict:
    return next(t for t in bank.transactions(Book(book.root)) if t["Betrag"] == amount)


def test_rule_added_later_is_a_sure_suggestion(book, tmp_path):
    import_(book, tmp_path, [(entry("12.00", "DBIT", "2026-02-27", party="UBS", ustrd="Kontoführung"), "-12.00")])
    tid = tx_by_amount(book, "-12.00")["ID"]
    api.bank_rule_add(Book(book.root), "6940", text="Kontoführung", buchungstext="Bankspesen", anwenden=False)
    opts = bank.suggestions(Book(book.root))[tid]
    assert opts[0]["art"] == "regel" and opts[0]["sicher"] and opts[0]["ziel"] == "R1"
    api.bank_accept(Book(book.root), tid, "regel", "R1")
    tx = tx_by_amount(book, "-12.00")
    assert tx["Status"] == "gebucht" and tx["Hinweis"] == "Regel R1"
    assert Decimal(api.ledger(Book(book.root), "6940", 2026)["saldo"]) == Decimal("12.00")
    assert_clean(Book(book.root))


def test_open_bill_beats_a_rule(book, tmp_path):
    import_(book, tmp_path, [(entry("486.45", "DBIT", "2026-02-24", party="Druckerei Beispiel AG",
                                    ustrd="Rechnung D-7781"), "-486.45")])
    api.bank_rule_add(Book(book.root), "6600", gegenpartei="Druckerei", anwenden=False)
    opts = bank.suggestions(Book(book.root))[tx_by_amount(book, "-486.45")["ID"]]
    assert opts[0]["art"] == "kreditor" and not any(o["art"] == "regel" for o in opts)


def test_search_finds_open_invoice_by_name_and_amount(book, tmp_path):
    import_(book, tmp_path, [(entry("200.00", "CRDT", "2026-02-21", party="P. Privat", ustrd="Anzahlung"), "200.00")])
    tid = tx_by_amount(book, "200.00")["ID"]
    assert bank.candidates(Book(book.root), tid) == []            # nothing with the same amount
    hits = bank.candidates(Book(book.root), tid, "Privat")
    assert [h["ziel"] for h in hits] == ["R-2026-0001"]
    assert [h["ziel"] for h in bank.candidates(Book(book.root), tid, "450")] == ["R-2026-0001"]
    api.bank_assign(Book(book.root), tid, "R-2026-0001")              # part payment through «zuordnen»
    assert tx_by_amount(book, "200.00")["Status"] == "gebucht"


def test_search_for_money_out_offers_bills_not_invoices(book, tmp_path):
    import_(book, tmp_path, [(entry("486.45", "DBIT", "2026-02-24", party="Unbekannt"), "-486.45")])
    hits = bank.candidates(Book(book.root), tx_by_amount(book, "-486.45")["ID"])
    assert [(h["art"], h["ziel"]) for h in hits] == [("kreditor", "E-2026-0001")]


def test_receipt_without_account_points_to_review(book, tmp_path):
    from aeradex import erfassung
    (book.root / "inbox" / "coop.txt").write_text("Coop Bern\n20.02.2026\nTotal CHF 64.80\n", encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/coop.txt", "quittung")["entwurf"]
    api.bill_draft_update(Book(book.root), d["id"], "Hand", name="Coop", betrag="64.80", datum="2026-02-20")
    meta = erfassung.draft(Book(book.root), d["id"])
    if (meta.get("konto") or {}).get("wert"):
        pytest.skip("Jev assigned an account")
    import_(book, tmp_path, [(entry("64.80", "DBIT", "2026-02-21", party="Coop Bern"), "-64.80")])
    opts = bank.suggestions(Book(book.root))[tx_by_amount(book, "-64.80")["ID"]]
    q = next(o for o in opts if o["art"] == "quittung")
    assert not q["sicher"] and q["pruefen"] == f"/eingang/quittung?entwurf={d['id']}"

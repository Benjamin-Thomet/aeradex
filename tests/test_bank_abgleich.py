"""Bank reconciliation page (Xero style): rules as suggestions, search & match, receipts without an
account pointed to their review page, and the page itself."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, bank
from aeradex.book import Book, BookError
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


def test_one_bank_account_at_a_time(book):
    """With two bank accounts the reconcile page and the movement list can show one account; taking all sure
    suggestions there leaves the other account alone."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    root = book.root
    other = "CH5604835012345678009"
    api.add_account(Book(root), "1021", "Bank Zwei", "aktiv")
    api.settings_update(Book(root), bankkonten={IBAN: "1020", other: "1021"})
    ref = api.invoice_list(Book(root))[0]["referenz"]
    one = statement(IBAN, "30000.00", [(entry("450.00", "CRDT", "2026-03-02", ref=ref, party="Peter Privat"), "450.00"),
                                       (entry("12.00", "DBIT", "2026-03-31", ustrd="Kontofuehrung Xq"), "-12.00")],
                    "2026-03-01", "2026-04-01", stmt_id="A")
    two = statement(other, "0.00", [(entry("80.00", "CRDT", "2026-03-05", party="Jemand", ustrd="Einzahlung"), "80.00")],
                    "2026-03-01", "2026-04-01", stmt_id="B")
    (root / "inbox" / "eins.xml").write_bytes(one)
    (root / "inbox" / "zwei.xml").write_bytes(two)
    api.bank_import(Book(root), "inbox/eins.xml")
    api.bank_import(Book(root), "inbox/zwei.xml")
    offen = {t["Konto"] for t in api.bank_list(Book(root), "offen")}
    assert offen == {"1020", "1021"}
    app = create_app(root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/bank?konto=1021").text
        assert "Bank Zwei" in page and "Jemand" in page and "Kontofuehrung Xq" not in page
        r = c.get("/bank", follow_redirects=False)                # never both banks on one page
        assert r.status_code == 303 and r.headers["location"] == "/bank?konto=1020"
        first = c.get("/bank").text
        assert "Kontofuehrung Xq" in first and "Jemand" not in first
        assert c.get("/bank/bewegungen", follow_redirects=False).headers["location"].startswith("/bank/bewegungen?konto=1020")
        assert "Jemand" not in c.get("/bank/bewegungen?konto=1020").text
    before = api.invoice_list(Book(root))[0]["status"]
    api.bank_accept_all(Book(root), "1021")                     # nothing sure on 1021: the invoice stays open
    assert api.invoice_list(Book(root))[0]["status"] == before
    assert_clean(Book(root))


def _two_banks(book):
    root = book.root
    other = "CH5604835012345678009"
    api.add_account(Book(root), "1021", "Bank Zwei", "aktiv")
    api.settings_update(Book(root), bankkonten={IBAN: "1020", other: "1021"})
    return root, other


def _import(root, iban, opening, items, stmt_id, name):
    (root / "inbox" / name).write_bytes(statement(iban, opening, items, "2026-03-01", "2026-04-01", stmt_id=stmt_id))
    return api.bank_import(Book(root), f"inbox/{name}")["import_"]


def test_transfer_between_own_accounts_is_suggested_and_books_both_sides(book):
    root, other = _two_banks(book)
    _import(root, IBAN, "30000.00", [(entry("500.00", "DBIT", "2026-03-10", ustrd="An Wise"), "-500.00")], "A", "a.xml")
    _import(root, other, "0.00", [(entry("500.00", "CRDT", "2026-03-11", ustrd="Einzahlung"), "500.00")], "B", "b.xml")
    out, inn = (next(t for t in api.bank_list(Book(root), "offen") if t["Konto"] == k) for k in ("1020", "1021"))
    sugg = bank.suggestions(Book(root))
    assert sugg[out["ID"]][0]["art"] == "uebertrag" and sugg[out["ID"]][0]["sicher"]
    assert sugg[inn["ID"]][0]["ziel"] == out["ID"]
    res = api.bank_accept_all(Book(root))                     # both sure; the second is done by the first
    b = Book(root)
    assert not api.bank_list(b, "offen")
    rows = [r for r in b.rows if r.text.startswith("Übertrag")]
    assert len(rows) == 1 and (rows[0].soll, rows[0].haben, rows[0].betrag) == ("1021", "1020", Decimal("500.00"))
    assert {t["Beleg"] for t in api.bank_list(b)} == {rows[0].beleg}
    assert all(r["differenz"] == "0.00" for r in api.bank_reconciliation(b))
    assert_clean(b)


def test_transfer_booked_from_one_side_reconciles_the_other_and_a_later_import(book):
    root, other = _two_banks(book)
    _import(root, IBAN, "30000.00", [(entry("400.00", "DBIT", "2026-03-12", ustrd="An Wise"), "-400.00"),
                                     (entry("500.00", "DBIT", "2026-03-20", ustrd="An Wise"), "-500.00")], "A", "a.xml")
    t400, t500 = sorted(api.bank_list(Book(root), "offen"), key=lambda t: t["Datum"])
    with pytest.raises(BookError, match="kein eigenes Bankkonto"):
        api.bank_transfer(Book(root), t400["ID"], "6500")
    api.bank_book(Book(root), t400["ID"], "1021", "Übertrag auf Bank Zwei")       # booked like an ordinary account
    api.bank_transfer(Book(root), t500["ID"], "1021")                              # partner not imported yet
    res = _import(root, other, "0.00", [(entry("400.00", "CRDT", "2026-03-12", ustrd="Einzahlung"), "400.00"),
                                        (entry("500.00", "CRDT", "2026-03-21", ustrd="Einzahlung"), "500.00")], "B", "b.xml")
    assert res["abgeglichen"] == 2 and res["offen"] == 0           # the same Beleg serves both statements
    assert_clean(Book(root))


def test_transfer_booked_after_both_imports_is_offered_on_the_other_side(book):
    root, other = _two_banks(book)
    _import(root, IBAN, "30000.00", [(entry("500.00", "DBIT", "2026-03-10", ustrd="An Wise"), "-500.00")], "A", "a.xml")
    _import(root, other, "0.00", [(entry("500.00", "CRDT", "2026-03-10", ustrd="Einzahlung"), "500.00")], "B", "b.xml")
    out, inn = (next(t for t in api.bank_list(Book(root), "offen") if t["Konto"] == k) for k in ("1020", "1021"))
    beleg = api.bank_book(Book(root), out["ID"], "1021", "an Wise")["buchung"]["beleg"]
    s = bank.suggestions(Book(root))[inn["ID"]][0]
    assert (s["art"], s["ziel"], s["sicher"]) == ("beleg", beleg, True)
    api.bank_accept(Book(root), inn["ID"], "beleg", beleg)
    assert not api.bank_list(Book(root), "offen")
    assert_clean(Book(root))

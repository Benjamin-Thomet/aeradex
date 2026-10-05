"""Bank suggestions: what an open movement probably is, taken with one click; matching of Belege
that touch the bank in several lines; receipt drafts that meet their bank movement after the import."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from batzen import api, bank, erfassung
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path: Path) -> Book:
    b = make_book(tmp_path, eroeffnung={"1020": 30000, "2800": -30000}, iban=IBAN,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    root = b.root
    (root / "inbox").mkdir(exist_ok=True)
    api.settings_update(Book(root), kreditoren={"agent_automatisch": False})
    api.post_entry(Book(root), "2026-01-31", "6000", "1020", "1800", "Miete Januar Immobilien Muster AG")
    api.customer_add(Book(root), name="Peter Privat", strasse="Seeweg", nr="1", plz="3600", ort="Thun")
    api.invoice_create(Book(root), "K0001", [{"text": "Steuererklärung", "menge": 1, "preis": "450"}], "2026-02-03")
    api.supplier_add(Book(root), name="Druckerei Beispiel AG", iban="CH7409000000300012345", konto="6600")
    api.bill_add(Book(root), "L0001", "486.45", datum="2026-02-10", rechnungsnr="D-7781")
    return Book(root)


def import_(book: Book, tmp_path: Path, items: list[tuple[str, str]], name: str = "feb") -> dict:
    f = tmp_path / f"{name}.xml"
    f.write_bytes(statement(IBAN, "28200.00", items, "2026-02-01", "2026-02-28", stmt_id=name.upper()))
    return api.bank_import(Book(book.root), str(f))


def tx_by_amount(book: Book, amount: str) -> dict:
    return next(t for t in bank.transactions(Book(book.root)) if t["Betrag"] == amount)


def test_salary_with_expenses_is_reconciled_on_import(book, tmp_path):
    """The payslip books the net wage and the expenses as two lines on the bank; the bank pays one sum."""
    api.employee_add(Book(book.root), "Lea", "Muster", monatslohn=5000)
    api.expense_add(Book(book.root), "M0001", "2026-02-05", "Zugbillett", "120", "6640")
    api.payroll_run(Book(book.root), "2026-02")
    api.payslip_close(Book(book.root), "2026-02", "M0001")
    paid = Decimal(api.payslip_show(Book(book.root), "2026-02", "M0001")["werte"]["auszahlung"])
    assert paid > 120
    import_(book, tmp_path, [(entry(f"{paid:.2f}", "DBIT", "2026-02-27", party="Lea Muster"), f"-{paid:.2f}")])
    tx = tx_by_amount(book, f"-{paid:.2f}")
    assert tx["Status"] == "abgeglichen" and tx["Beleg"] == "L-2026-02-M0001"


def test_suggestions_and_one_click(book, tmp_path):
    import_(book, tmp_path, [
        (entry("450.00", "CRDT", "2026-02-21", party="Peter Privat", ustrd="Steuererklaerung"), "450.00"),
        (entry("486.45", "DBIT", "2026-02-24", party="Druckerei Beispiel AG", ustrd="Rechnung D-7781"), "-486.45"),
        (entry("1800.00", "DBIT", "2026-02-27", party="Immobilien Muster AG", ustrd="Miete Februar"), "-1800.00"),
        (entry("5.00", "DBIT", "2026-02-28", ustrd="Kontoführung"), "-5.00"),
    ])
    s = api.bank_suggestions(Book(book.root))
    inv, bill, rent, fee = (tx_by_amount(book, a)["ID"] for a in ("450.00", "-486.45", "-1800.00", "-5.00"))
    assert (s[inv][0]["art"], s[inv][0]["ziel"], s[inv][0]["sicher"]) == \
        ("rechnung", "R-2026-0001", True)
    assert (s[bill][0]["art"], s[bill][0]["ziel"], s[bill][0]["sicher"]) == ("kreditor", "E-2026-0001", True)
    assert (s[rent][0]["art"], s[rent][0]["ziel"], s[rent][0]["sicher"]) == ("konto", "6000", False)
    assert fee not in s

    res = api.bank_accept_all(Book(book.root))
    assert len(res["abgeglichen"]) == 2                     # only the sure ones
    b = Book(book.root)
    assert tx_by_amount(b, "450.00")["Status"] == "gebucht"
    assert tx_by_amount(b, "-486.45")["Status"] == "gebucht"
    assert tx_by_amount(b, "-1800.00")["Status"] == "offen"

    api.bank_accept(Book(book.root), rent)                  # first suggestion: 6000 from history
    assert tx_by_amount(book, "-1800.00")["Status"] == "gebucht"
    assert Decimal(api.ledger(Book(book.root), "6000", 2026)["saldo"]) == Decimal("3600.00")
    with pytest.raises(BookError, match="keinen Vorschlag"):
        api.bank_accept(Book(book.root), fee)
    with pytest.raises(BookError, match="nicht \\(mehr\\)"):
        api.bank_accept(Book(book.root), fee, "konto", "6940")
    assert_clean(Book(book.root))


def test_same_amount_twice_is_not_sure(book, tmp_path):
    api.bill_add(Book(book.root), "L0001", "486.45", datum="2026-02-12", rechnungsnr="D-7790")
    import_(book, tmp_path, [(entry("486.45", "DBIT", "2026-02-24", party="Druckerei Beispiel AG"), "-486.45")])
    tid = tx_by_amount(book, "-486.45")["ID"]
    opts = api.bank_suggestions(Book(book.root))[tid]
    assert {o["ziel"] for o in opts} == {"E-2026-0001", "E-2026-0002"} and not any(o["sicher"] for o in opts)
    assert api.bank_accept_all(Book(book.root))["abgeglichen"] == []


@pytest.mark.parametrize("amount,side,signed", [("450.00", "CRDT", "450.00"),
                                               ("486.45", "DBIT", "-486.45")])
def test_amount_alone_is_not_sure(book, tmp_path, amount, side, signed):
    import_(book, tmp_path, [(entry(amount, side, "2026-02-24", party="Unbekannte Gegenpartei"), signed)])
    tid = tx_by_amount(book, signed)["ID"]
    opts = api.bank_suggestions(Book(book.root))[tid]
    assert opts and not any(o["sicher"] for o in opts)
    assert api.bank_accept_all(Book(book.root))["abgeglichen"] == []
    assert tx_by_amount(book, signed)["Status"] == "offen"


def test_bill_number_resolves_same_amount(book, tmp_path):
    api.bill_add(Book(book.root), "L0001", "486.45", datum="2026-02-12", rechnungsnr="D-7790")
    import_(book, tmp_path, [(entry("486.45", "DBIT", "2026-02-24", party="Druckerei Beispiel AG",
                                  ustrd="Zahlung D-7790"), "-486.45")])
    tid = tx_by_amount(book, "-486.45")["ID"]
    opts = api.bank_suggestions(Book(book.root))[tid]
    assert [o["ziel"] for o in opts if o["sicher"]] == ["E-2026-0002"]
    assert len(api.bank_accept_all(Book(book.root))["abgeglichen"]) == 1
    assert next(b for b in api.bill_list(Book(book.root)) if b["nummer"] == "E-2026-0001")["status"] == "offen"
    assert_clean(Book(book.root))


def test_same_customer_and_amount_needs_invoice_number(book, tmp_path):
    api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "450"}], "2026-02-04")
    import_(book, tmp_path, [(entry("450.00", "CRDT", "2026-02-24", party="Peter Privat"), "450.00")])
    tid = tx_by_amount(book, "450.00")["ID"]
    opts = api.bank_suggestions(Book(book.root))[tid]
    assert len(opts) == 2 and not any(o["sicher"] for o in opts)
    assert api.bank_accept_all(Book(book.root))["abgeglichen"] == []


def test_agent_proposal_needs_individual_approval(book, tmp_path):
    import_(book, tmp_path, [(entry("5.00", "DBIT", "2026-02-28", ustrd="Kontoführung"), "-5.00")])
    tid = tx_by_amount(book, "-5.00")["ID"]
    api.propose(Book(book.root), "2026-02-28", "6940", "1020", "5", "Kontoführung",
                "Gebühr laut Kontoauszug", bank=tid)
    opt = api.bank_suggestions(Book(book.root))[tid][0]
    assert opt["art"] == "vorschlag" and not opt["sicher"]
    assert opt["grund"] == "Gebühr laut Kontoauszug"
    assert api.bank_accept_all(Book(book.root))["abgeglichen"] == []
    api.bank_accept(Book(book.root), tid, "vorschlag", opt["ziel"])
    assert tx_by_amount(book, "-5.00")["Status"] == "gebucht"
    assert_clean(Book(book.root))


def test_bank_suggestion_forms(book, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from batzen.web.app import create_app

    import_(book, tmp_path, [
        (entry("450.00", "CRDT", "2026-02-21", party="Peter Privat"), "450.00"),
        (entry("1800.00", "DBIT", "2026-02-27", party="Immobilien Muster AG"), "-1800.00"),
    ])
    rent = tx_by_amount(book, "-1800.00")["ID"]
    app = create_app(book.root, token="tok")
    with TestClient(app) as client:
        client.get("/?t=tok", follow_redirects=False)
        for page in ("/bank", "/pruefen"):
            response = client.get(page)
            assert response.status_code == 200
            assert "Alle sicheren abgleichen" in response.text and "R-2026-0001" in response.text
        headers = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        response = client.post("/bank/alle-abgleichen", headers=headers)
        assert response.status_code == 204 and response.headers["HX-Redirect"] == "/bank"
        assert tx_by_amount(book, "450.00")["Status"] == "gebucht"
        assert tx_by_amount(book, "-1800.00")["Status"] == "offen"
        response = client.post(f"/bank/{rent}/uebernehmen", data={"art": "konto", "ziel": "6000"}, headers=headers)
        assert response.status_code == 204
        assert tx_by_amount(book, "-1800.00")["Status"] == "gebucht"
    assert_clean(Book(book.root))


def test_receipt_draft_meets_its_bank_movement_after_import(book, tmp_path):
    """Receipt read before the statement: booking it later must use the movement, not book it twice."""
    (book.root / "inbox" / "coop.txt").write_text("Coop Bern\nDatum: 20.02.2026\nKaffee\nTotal CHF 64.80\n"
                                                   "Bezahlt mit Debitkarte\n")
    d = api.bill_draft_create(Book(book.root), "inbox/coop.txt")["entwurf"]
    assert d["art"] == "quittung" and d["zahlung"]["art"] == "konto"
    api.bill_draft_update(Book(book.root), d["id"], "Hand", "5800")
    import_(book, tmp_path, [(entry("64.80", "DBIT", "2026-02-20", party="Coop", ustrd="Debitkarte"), "-64.80")])
    tid = tx_by_amount(book, "-64.80")["ID"]
    assert erfassung.draft(Book(book.root), d["id"])["zahlung"] == {**erfassung.draft(Book(book.root), d["id"])["zahlung"],
                                                                    "art": "bank", "bank": tid}
    opt = api.bank_suggestions(Book(book.root))[tid][0]
    assert (opt["art"], opt["ziel"], opt["sicher"]) == ("quittung", d["id"], True)
    api.bank_accept(Book(book.root), tid)
    b = Book(book.root)
    assert tx_by_amount(b, "-64.80")["Status"] == "gebucht"
    assert Decimal(api.ledger(b, "1020", 2026)["saldo"]) == Decimal("30000") - Decimal("1800") - Decimal("64.80")
    assert api.bill_drafts(b) == []
    assert_clean(b)

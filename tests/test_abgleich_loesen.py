"""Undoing a bank reconciliation: the movement is open again, the booking stays and can be corrected."""
from __future__ import annotations

from decimal import Decimal

import pytest

from aeradex import api, bank, erfassung
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book, problems
from test_eingang import RECEIPT, import_bank, pdf

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "1000": 500, "2800": -5500}, iban=IBAN,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    (b.root / "inbox").mkdir(exist_ok=True)
    api.settings_update(Book(b.root), kreditoren={"agent_automatisch": False})
    return Book(b.root)


def booked_receipt(book, tmp_path, amount="-45.50") -> dict:
    """A receipt booked together with its card payment: the movement is «gebucht» with the receipt's Beleg."""
    import_bank(book, tmp_path, amount, "2026-03-06")
    pdf(book.root / "inbox" / "q.pdf", RECEIPT)
    d = api.bill_draft_create(Book(book.root), "inbox/q.pdf")["entwurf"]
    api.receipt_book(Book(book.root), d["id"], "2026-03-05", "Papier", "45.50", "6500",
                     zahlung=erfassung.draft(Book(book.root), d["id"])["zahlung"])
    return bank.transactions(Book(book.root))[0]


def test_unlink_then_correct_the_amount_and_match_again(book, tmp_path):
    tx = booked_receipt(book, tmp_path)
    assert tx["Status"] == "gebucht"
    beleg = tx["Beleg"]
    one_line = [{"soll": "6500", "haben": "1020", "betrag": "54.50", "text": "Papier", "mwst": ""}]
    with pytest.raises(BookError, match="Abgleich lösen"):
        api.amend_entry(Book(book.root), beleg, "2026-03-05", "Papier", one_line)
    res = api.bank_unlink(Book(book.root), tx["ID"])
    assert f"Beleg {beleg} bleibt" in res["meldung"]
    t = bank.find(Book(book.root), tx["ID"])
    assert t["Status"] == "offen" and t["Beleg"] == "" and "war gebucht" in t["Hinweis"]
    api.amend_entry(Book(book.root), beleg, "2026-03-05", "Papier", one_line)     # now free to change
    assert Decimal(api.ledger(Book(book.root), "6500", 2026)["saldo"]) == Decimal("54.50")
    assert any(p.name.startswith(beleg) for p in (book.root / "belege" / "2026").iterdir())   # receipt stays
    warn = [p for p in problems(Book(book.root)) if "Differenz" in p]
    assert warn and "Differenz 54.50" in warn[0]       # bank and books disagree until matched again


def test_unlink_and_relink_the_same_booking(book, tmp_path):
    tx = booked_receipt(book, tmp_path)
    api.bank_unlink(Book(book.root), tx["ID"])
    api.bank_link(Book(book.root), tx["ID"], tx["Beleg"])
    assert bank.find(Book(book.root), tx["ID"])["Status"] == "abgeglichen"
    assert Decimal(api.ledger(Book(book.root), "1020", 2026)["saldo"]) == Decimal("4954.50")    # booked once
    assert_clean(Book(book.root))


def test_ignored_movement_gets_its_text_back(book, tmp_path):
    import_bank(book, tmp_path, "-12.00", "2026-03-06")
    tid = bank.transactions(Book(book.root))[0]["ID"]
    text = bank.find(Book(book.root), tid)["Text"]
    api.bank_ignore(Book(book.root), tid, "Übertrag")
    api.bank_unlink(Book(book.root), tid)
    t = bank.find(Book(book.root), tid)
    assert t["Status"] == "offen" and t["Text"] == text
    with pytest.raises(BookError, match="schon offen"):
        api.bank_unlink(Book(book.root), tid)


def test_locked_period_cannot_be_unlinked(book, tmp_path):
    tx = booked_receipt(book, tmp_path)
    api.lock(Book(book.root), "2026-03-31")
    with pytest.raises(BookError, match="gesperrt"):
        api.bank_unlink(Book(book.root), tx["ID"])


def test_ui_button_unlinks(book, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    tx = booked_receipt(book, tmp_path)
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/bank/bewegungen").text
        assert f'hx-post="/bank/{tx["ID"]}/loesen"' in page and "Abgleich lösen" in page
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true", "HX-Current-URL": "/bank/bewegungen"}
        r = c.post(f"/bank/{tx['ID']}/loesen", headers=h)
        assert r.status_code == 204, r.text
    assert bank.find(Book(book.root), tx["ID"])["Status"] == "offen"

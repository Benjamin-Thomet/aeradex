"""Regression coverage for malformed amounts and authentication input."""
from decimal import Decimal

import pytest

from batzen import api
from batzen.book import Book
from batzen.files import FormatError, parse_amount
from batzen.testing import make_book


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity", Decimal("NaN"), Decimal("Infinity")])
def test_amount_rejects_nonfinite_values(raw):
    with pytest.raises(FormatError, match="kein Betrag"):
        parse_amount(raw, "betrag")


def test_batch_rejects_nonfinite_amount_without_writing(tmp_path):
    book = make_book(tmp_path)
    good = {"datum": "2026-03-05", "text": "Papier", "soll": "6500", "haben": "1020", "betrag": "10"}
    with pytest.raises(api.RowErrors) as exc:
        api.post_entries(book, [good, {**good, "betrag": "NaN"}])
    assert set(exc.value.fehler) == {2}
    assert Book(book.root).rows == []


def test_session_rejects_non_ascii_signature(tmp_path):
    from batzen.web.auth import Sessions
    sessions = Sessions(tmp_path)
    cookie = sessions.issue("anna", 0)
    assert sessions.read(cookie)["u"] == "anna"
    assert sessions.read(cookie.split(".")[0] + ".é") is None


def test_local_auth_rejects_non_ascii_token(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from batzen.web.app import create_app
    book = make_book(tmp_path)
    with TestClient(create_app(book.root, token="tok")) as client:
        assert client.get("/?t=é").status_code == 403
        client.cookies.set("batzen_session", '"\\351"')
        assert client.get("/").status_code == 401


@pytest.mark.parametrize("split", [False, True])
def test_reverse_foreign_booking_preserves_currency_and_balances(tmp_path, split):
    from batzen.testing import assert_clean
    book = make_book(tmp_path)
    api.add_account(book, "1021", "Bank EUR", "aktiv", waehrung="EUR")
    if split:
        api.post_split(book, "2026-03-05", "Verkauf", [
            {"soll": "1021", "betrag": "100"},
            {"haben": "3200", "betrag": "60"},
            {"haben": "3400", "betrag": "40"},
        ], waehrung="EUR", kurs="0.95")
    else:
        api.post_entry(book, "2026-03-05", "1021", "3200", "100", "Verkauf",
                                waehrung="EUR", kurs="0.95")
    original = list(Book(book.root).rows)
    api.reverse_entry(book, original[0].beleg, "2026-03-06")
    rows = Book(book.root).rows
    for before, after in zip(original, rows[len(original):]):
        assert (after.soll, after.haben) == (before.haben, before.soll)
        assert (after.waehrung, after.fw, after.kurs) == (before.waehrung, before.fw, before.kurs)
    ledger = api.ledger(Book(book.root), "1021", 2026)
    assert ledger["saldo"] == "0.00" and ledger["saldo_fw"] == "0.00"
    assert_clean(Book(book.root))

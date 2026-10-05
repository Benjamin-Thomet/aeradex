"""Dividend payout with Verrechnungssteuer; the cash-box check."""
from __future__ import annotations

from decimal import Decimal

import pytest

from allkvitt import api, check
from allkvitt.book import Book, BookError
from allkvitt.testing import assert_clean, make_book


def test_dividend_with_verrechnungssteuer(tmp_path):
    b = make_book(tmp_path, jahr=2025, eroeffnung={"1020": 50000, "2800": -20000, "2970": -30000})
    api.post_entry(b, "2025-06-01", "1020", "3400", "40000", "Umsatz")
    with pytest.raises(BookError, match="noch nicht gebucht"):
        api.dividend_pay(Book(b.root), 2025, "2026-07-10")
    api.allocation_set(Book(b.root), 2025, "10000", "0")
    api.allocation_book(Book(b.root), 2025, "2026-06-30")
    out = api.dividend_pay(Book(b.root), 2025, "2026-07-10")
    d = out["dividende"]
    assert d["vst"] == "3500.00" and d["netto"] == "6500.00" and d["frist"] == "2026-08-09"
    bk = Book(b.root)
    assert Decimal(api.ledger(bk, "2261", 2026)["saldo"]) == 0
    assert Decimal(api.ledger(bk, "2206", 2026)["saldo"]) == Decimal("-3500.00")
    with pytest.raises(BookError, match="bereits"):
        api.dividend_pay(bk, 2025, "2026-07-11")
    api.post_entry(bk, "2026-08-05", "2206", "1020", "3500", "Verrechnungssteuer Formular 103")
    assert_clean(Book(b.root))


def test_negative_cash_box_is_flagged(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1000": 100, "2800": -100})
    api.post_entry(b, "2026-03-01", "6500", "1000", "150", "Einkauf bar")
    found = [str(i) for i in check.run(Book(b.root)) if "Kasse 1000" in i.message]
    assert found and "01.03.2026 negativ (-50.00)" in found[0]

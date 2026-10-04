from datetime import date
from decimal import Decimal
from pathlib import Path

from batzen import api, fx
from batzen.book import Book

SAMPLE = b'''<?xml version="1.0"?><wechselkurse xmlns="https://www.backend-rates.bazg.admin.ch/xmldaily">
<datum>02.10.2026</datum><devise code="eur"><waehrung>1 EUR</waehrung><kurs>0.94445</kurs></devise>
<devise code="jpy"><waehrung>100 JPY</waehrung><kurs>0.56</kurs></devise></wechselkurse>'''


def test_rates_parse_cache_and_weekend_fallback(tmp_path: Path):
    api.init_book(tmp_path / "b", "X GmbH", 2026, git=False)
    b = Book(tmp_path / "b")
    calls = []
    def fetch(url):
        calls.append(url)
        return SAMPLE if "20261002" in url else b"<wechselkurse/>"
    assert fx.rate(b, "EUR", date(2026, 10, 4), fetch) == Decimal("0.94445")   # Sunday -> Friday
    assert fx.rate(b, "JPY", date(2026, 10, 2), fetch) == Decimal("0.0056")
    n = len(calls)
    fx.rate(b, "EUR", date(2026, 10, 2), fetch)
    assert len(calls) == n                                                       # cached

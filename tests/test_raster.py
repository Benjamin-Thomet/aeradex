"""The journal grid: several bookings at once, all or none, errors per line."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from aeradex import api
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book


@pytest.fixture
def book(tmp_path):
    return make_book(tmp_path, eroeffnung={"1020": 5000, "2800": -5000})


def test_batch_is_all_or_nothing(book):
    good = {"datum": "2026-03-05", "text": "Papier", "soll": "6500", "haben": "1020", "betrag": "45.80"}
    bad = {"datum": "2026-03-06", "text": "Miete", "soll": "9999", "haben": "1020", "betrag": "1800"}
    with pytest.raises(api.RowErrors) as exc:
        api.post_entries(book, [good, bad, {**good, "text": ""}])
    assert set(exc.value.fehler) == {2, 3} and "9999" in exc.value.fehler[2]
    assert Book(book.root).rows == []
    out = api.post_entries(Book(book.root), [good, {**bad, "soll": "6000"}])
    assert [r["beleg"] for r in out["buchungen"]] == ["26-001", "26-002"]
    assert_clean(Book(book.root))


def test_grid_view_parses_like_excel(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    api.settings_update(book, mwst={"methode": "effektiv"})
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/journal?jahr=2026").text
        assert 'id="raster"' in page and 'list="mwstcodes"' in page
        data = {"jahr": "2026",
                "datum": ["5.3.26", "", "", ""],
                "text": ["Papier", "Miete März", "", "Kaffee"],
                "soll": ["6500 Büromaterial", "6000", "", "9999"],
                "haben": ["1020", "1020", "", "1020"],
                "betrag": ["108.10", "1'800.00", "", "12,50"],
                "mwst": ["V81", "", "", ""]}
        r = c.post("/journal/raster", headers=h, data=data)
        marks = json.loads(r.headers["HX-Trigger"])["rasterFehler"]
        assert list(marks) == ["4"] and "9999" in marks["4"]            # grid line 4, not batch line 3
        assert Book(book.root).rows == []
        data["soll"][3] = "5800"
        r = c.post("/journal/raster", headers=h, data=data)
        assert r.status_code == 204, r.text
    b = Book(book.root)
    assert {r.datum.isoformat() for r in b.rows} == {"2026-03-05"}      # empty date = as above
    assert Decimal(api.ledger(b, "6500", 2026)["saldo"]) == Decimal("100.00")      # V81 split off the gross
    assert Decimal(api.ledger(b, "5800", 2026)["saldo"]) == Decimal("12.50")
    assert_clean(b)


def test_grid_needs_a_first_date(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        r = c.post("/journal/raster", headers={"X-CSRF": app.state.ui.csrf, "HX-Request": "true"},
                   data={"datum": [""], "text": ["x"], "soll": ["6500"], "haben": ["1020"], "betrag": ["1"]})
        assert "Datum fehlt" in json.loads(r.headers["HX-Trigger"])["rasterFehler"]["1"]

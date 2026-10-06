from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, check
from allkvitt.book import Book, BookError
from allkvitt.testing import assert_clean, make_book
from allkvitt_anlagen import anlagen


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["anlagen"], eroeffnung={"1020": 10000, "2800": -10000})
    api.post_entry(b, "2026-03-15", "1520", "1020", "2400", "Laptop Lea")
    return Book(b.root)


def saldo(book, konto, year=2026):
    return Decimal(api.ledger(Book(book.root), konto, year)["saldo"])


def add_laptop(book, **kw):
    return api.write(book, "Anlage", anlagen.add, "Laptop Lea", "2026-03-15", "2400", "edv", **kw)["ergebnis"]


def test_degressive_pro_rata_and_next_year(book):
    meta = add_laptop(book)
    assert meta["konto"] == "1520" and meta["satz"] == "40"
    assert anlagen.preview(Book(book.root), 2026)[0]["betrag"] == Decimal("800.00")      # 2400 × 40 % × 10/12
    api.write(Book(book.root), "AB", anlagen.depreciate, 2026)
    assert saldo(book, "6800") == Decimal("800.00") and saldo(book, "1520") == Decimal("1600.00")
    assert_clean(Book(book.root))
    api.post_entry(Book(book.root), "2027-01-05", "6500", "1020", "10", "x")
    api.write(Book(book.root), "AB", anlagen.depreciate, 2027)
    assert saldo(book, "6800", 2027) == Decimal("640.00")                                 # 1600 × 40 %
    with pytest.raises(BookError, match="nichts abzuschreiben"):
        api.write(Book(book.root), "AB", anlagen.depreciate, 2027)


def test_linear_half_rate_and_residual_value(book):
    meta = api.write(book, "Anlage", anlagen.add, "Server", "2026-01-10", "1000", "edv", "", "linear", None, 990)["ergebnis"]
    assert meta["satz"] == "20"
    assert anlagen.depreciation(meta, 2026) == Decimal("10.00")                         # capped at the residual value


def test_tampering_with_a_booked_depreciation_is_caught(book):
    add_laptop(book)
    api.write(Book(book.root), "AB", anlagen.depreciate, 2026)
    path = next((book.root / "anlagen").glob("A0001*.yaml"))
    path.write_text(path.read_text().replace("'800.00'", "'900.00'"))
    assert any(i.level == "fehler" and "Anlage" in i.message for i in check.run(Book(book.root)))


def test_disposal_with_proceeds(book):
    add_laptop(book)
    api.write(Book(book.root), "AB", anlagen.depreciate, 2026)
    api.post_entry(Book(book.root), "2027-01-05", "6500", "1020", "10", "x")
    api.write(Book(book.root), "Abgang", anlagen.dispose, "A0001", "2027-06-30", "1000")
    b = Book(book.root)
    assert saldo(b, "1520", 2027) == 0 and saldo(b, "8500", 2027) == Decimal("600.00")
    assert anlagen.schedule(b, 2027)["anlagen"][0]["abgang"] == Decimal("1600.00")
    assert_clean(b)


def test_account_reconciliation_hint(book):
    assert anlagen.assets(book) == {}
    add_laptop(book)
    api.post_entry(Book(book.root), "2026-04-01", "1520", "1020", "300", "Monitor ohne Anlage")
    hints = [i.message for i in check.run(Book(book.root)) if i.level == "hinweis"]
    assert any("Konto 1520: Saldo 2700.00, Anlagen 2400.00" in h for h in hints)


def test_plugin_page(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from allkvitt.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert 'href="/p/anlagen/anlagen"' in c.get("/abschluss").text                   # Plugins navigation
        r = c.post("/p/anlagen/anlagen/erfassen", headers=h,
                   data={"bezeichnung": "Laptop Lea", "datum": "2026-03-15", "wert": "2400", "kategorie": "edv"})
        assert r.status_code == 204, r.text
        page = c.get("/p/anlagen/anlagen?jahr=2026").text
        assert "A0001" in page and "Abschreibungen 2026 buchen" in page
        r = c.post("/p/anlagen/anlagen/abschreiben", headers=h, data={"jahr": "2026"})
        assert r.status_code == 204, r.text
        assert c.get("/p/nichtda/x").status_code == 404
    assert saldo(book, "6800") == Decimal("800.00")

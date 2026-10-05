"""Chart templates per legal form; Einzelfirma Privat accounts closing into Eigenkapital."""
from decimal import Decimal

import pytest

from allkvitt import api, plugins, statements
from allkvitt.book import Book, BookError
from allkvitt.ledger import BalanceEngine
from allkvitt.testing import assert_clean, make_book, problems


def test_templates_are_built_in():
    assert {"kmu", "einzelfirma", "verein"} <= set(plugins.kontenplaene())


@pytest.mark.parametrize("rechtsform, konto, fehlt", [
    ("GmbH", "2970", "2850"), ("AG", "2970", "2850"),
    ("Einzelfirma", "2851", "2970"), ("Verein", "3000", "2851")])
def test_chart_follows_legal_form(tmp_path, rechtsform, konto, fehlt):
    book = make_book(tmp_path, rechtsform=rechtsform)
    assert konto in book.accounts and fehlt not in book.accounts
    assert_clean(book)


def test_explicit_template_wins(tmp_path):
    book = make_book(tmp_path, rechtsform="GmbH", kontenplan="einzelfirma")
    assert "2850" in book.accounts


def test_einzelfirma_year_end(tmp_path):
    book = make_book(tmp_path, firma="Maler Muster", rechtsform="Einzelfirma",
                     eroeffnung={"1020": 10000, "2800": -10000})
    assert book.settings.konto("gewinnvortrag") == "2800"
    api.post_entry(book, "2026-03-01", "1100", "3400", "8000", "Malerarbeiten")
    api.post_entry(Book(book.root), "2026-03-20", "1020", "1100", "8000", "Zahlung Kunde")
    api.post_entry(Book(book.root), "2026-04-01", "6000", "1020", "1000", "Miete Werkstatt")
    api.post_entry(Book(book.root), "2026-05-01", "2850", "1020", "2500", "Privatbezug")
    api.post_entry(Book(book.root), "2026-06-01", "2851", "1020", "700", "AHV Akonto Inhaber")
    api.post_entry(Book(book.root), "2026-07-01", "2852", "1020", "300", "Steuern Inhaber")
    api.post_entry(Book(book.root), "2027-01-10", "6500", "1020", "50", "Büromaterial")
    book = Book(book.root)
    assert_clean(book)

    st = statements.year_end_statement(book, 2026)
    assert st["differenz"] == 0 and st["jahresergebnis"] == 7000
    assert st["gewinnverwendung"] is None
    ek = st["eigenkapital"]
    assert ek["bestand"] == 10000 and [p["betrag"] for p in ek["privat"]] == [-2500, -700, -300]
    assert ek["neu"] == Decimal("13500")              # 10000 + 7000 Gewinn − 3500 privat

    eng = BalanceEngine(book)
    assert eng.opening("2800", 2027) == Decimal("-13500")
    for nr in ("2850", "2851", "2852"):
        assert eng.opening(nr, 2027) == 0
    assert statements.year_end_statement(book, 2027, eng)["differenz"] == 0

    with pytest.raises(BookError, match="nur bei AG und GmbH"):
        api.allocation_set(book, 2026, 1000, 0)


def test_gmbh_keeps_profit_allocation(tmp_path):
    book = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000})
    st = statements.year_end_statement(book, 2026)
    assert st["gewinnverwendung"] is not None and st["eigenkapital"] is None


def test_verein_year(tmp_path):
    book = make_book(tmp_path, firma="Turnverein Muster", rechtsform="Verein",
                     eroeffnung={"1020": 5000, "2800": -5000})
    assert book.settings.konto("ertrag") == "3000"
    api.post_entry(book, "2026-03-01", "1020", "3000", "1200", "Mitgliederbeiträge 2026")
    api.post_entry(Book(book.root), "2026-04-01", "6000", "1020", "800", "Hallenmiete")
    assert_clean(Book(book.root))
    st = statements.year_end_statement(Book(book.root), 2026)
    assert st["jahresergebnis"] == 400 and st["differenz"] == 0 and st["eigenkapital"]["neu"] == 400


def test_abschluss_must_point_to_equity(tmp_path):
    book = make_book(tmp_path, rechtsform="Einzelfirma")
    book.accounts["2850"].abschluss = "1020"
    book.save_accounts()
    assert any("abschluss 1020" in p for p in problems(book))


def test_einzelfirma_closing_page_and_pdf(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from allkvitt.web.app import create_app
    book = make_book(tmp_path, firma="Maler Muster", rechtsform="Einzelfirma",
                     eroeffnung={"1020": 10000, "2800": -10000})
    api.post_entry(book, "2026-05-01", "2851", "1020", "700", "AHV Akonto Inhaber")
    with TestClient(create_app(book.root, token="tok")) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/abschluss?jahr=2026")
        assert page.status_code == 200
        assert "Eigenkapital nach Abschluss" in page.text and "Privat AHV" in page.text
        assert "Gewinnverwendung" not in page.text
        assert c.get("/pdf/jahresrechnung?jahr=2026").status_code == 200

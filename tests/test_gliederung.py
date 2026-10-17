"""Jahresrechnung preview and its Gliederung: accounts move between OR groups of the same side."""
from __future__ import annotations

import pytest

from aeradex import api, statements
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book, problems


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 10000, "2800": -10000}, git=True)
    api.post_entry(Book(b.root), "2026-03-01", "1176", "1020", "500", "Verrechnungssteuer-Guthaben")
    api.post_entry(Book(b.root), "2026-03-02", "6500", "1020", "120", "Büromaterial")
    return Book(b.root)


def group_of(book, side, nr):
    return next(g["gruppe"] for g in statements.layout(Book(book.root), 2026, side) if any(k["konto"] == nr for k in g["konten"]))


def test_layout_lists_every_group_of_a_side(book):
    bilanz = statements.layout(book, 2026, "bilanz")
    codes = [g["gruppe"] for g in bilanz]
    assert codes[:6] == statements._UMLAUF and "eigenkapital" in codes and "ertrag" not in codes
    assert next(g for g in bilanz if g["gruppe"] == "ford_nahe_kf")["konten"] == []           # empty: a place to drop
    assert [g["gruppe"] for g in statements.layout(book, 2026, "erfolg")] == statements._PL
    assert group_of(book, "bilanz", "1176") == "ford_uebrige_kf"
    assert all(k["konto"] != "1000" for g in bilanz for k in g["konten"])                    # no balance: left out
    assert any(k["konto"] == "1000" for g in statements.layout(book, 2026, "bilanz", alle=True) for k in g["konten"])


def test_move_reset_and_the_statement_follows(book):
    res = api.account_group_set(book, "1176", "arap", 2026)
    assert res["ok"] and "1176" in (book.root / "kontenplan.yaml").read_text()
    b = Book(book.root)
    assert group_of(b, "bilanz", "1176") == "arap"
    row = next(r for r in statements.year_end_statement(b, 2026)["aktiven"] if r.get("gruppe") == "arap")
    assert row["aktuell"] == 500 and row["konten"][0]["verschoben"] is True
    assert_clean(b)
    api.account_group_set(b, "1176", "", 2026)                                              # back to its number's group
    assert group_of(Book(book.root), "bilanz", "1176") == "ford_uebrige_kf"
    assert "gruppe" not in (book.root / "kontenplan.yaml").read_text().split("nr: '1176'")[1].split("- nr")[0]


def test_wrong_side_fixed_accounts_and_locked_year_are_refused(book):
    with pytest.raises(BookError, match="Bilanzkonto"):
        api.account_group_set(book, "1176", "betrieb", 2026)
    with pytest.raises(BookError, match="Erfolgskonto"):
        api.account_update(Book(book.root), "6500", gruppe="sachanlagen")
    with pytest.raises(BookError, match="Abschlusskonto"):
        api.account_group_set(Book(book.root), book.settings.konto("jahresergebnis"), "verb_uebrige_kf", 2026)
    api.lock(Book(book.root), "2026-12-31")
    with pytest.raises(BookError, match="gesperrt"):
        api.account_group_set(Book(book.root), "1176", "arap", 2026)


def test_check_warns_about_a_group_of_the_other_side(book):
    text = (book.root / "kontenplan.yaml").read_text()
    path = book.root / "kontenplan.yaml"
    path.write_text(text.replace("- nr: '6500'\n", "- nr: '6500'\n  gruppe: sachanlagen\n", 1))
    assert any("6500" in p and "Bilanz" in p for p in problems(Book(book.root)))


def test_preview_and_gliederung_pages(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/abschluss/vorschau?jahr=2026").text
        assert "Bilanz per 31. Dezember 2026" in page and "Erfolgsrechnung 2026" in page
        assert "1. Firma, Rechtsform und Sitz" in page and "Art. 958 Abs. 3 OR" in page
        assert "Antrag über die Verwendung des Bilanzverlustes" in page          # Test GmbH, a loss so far
        assert 'href="/abschluss/vorschau?jahr=2026"' in c.get("/abschluss?jahr=2026").text
        glied = c.get("/abschluss/vorschau?jahr=2026&gliederung=1").text
        assert 'data-dropgruppe="ford_nahe_kf"' in glied and 'data-konto="1176"' in glied and 'draggable="true"' in glied
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/abschluss/gruppe", headers=h, data={"konto": "1176", "gruppe": "arap", "jahr": "2026"})
        assert r.status_code == 204, r.text
        r = c.post("/abschluss/gruppe", headers=h, data={"konto": "1176", "gruppe": "betrieb", "jahr": "2026"})
        assert r.status_code == 200 and "Bilanzkonto" in r.text
        glied = c.get("/abschluss/vorschau?jahr=2026&gliederung=1").text
        assert "verschoben" in glied and "↺ Standard" in glied
    assert group_of(Book(book.root), "bilanz", "1176") == "arap"


def test_einzelfirma_shows_the_equity_change_not_an_allocation(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    b = make_book(tmp_path, firma="Maler Muster", rechtsform="Einzelfirma", eroeffnung={"1020": 5000, "2800": -5000})
    app = create_app(b.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/abschluss/vorschau").text
        assert "Veränderung" in page and "keine Gewinnverwendung" in page and "Inhaber/in" in page
        assert "Antrag über die Verwendung" not in page
        assert "MWST-Jahresabschluss" not in c.get("/abschluss").text


def test_vat_year_end_box_only_with_vat(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "2800": -5000})
    api.settings_update(Book(b.root), mwst={"methode": "effektiv"})
    app = create_app(b.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/abschluss?jahr=2026").text
    assert "MWST-Jahresabschluss 2026" in page and "/mwst/abstimmung?jahr=2026" in page
    assert "/abschluss/unterlagen/mwst?jahr=2026&amp;format=pdf" in page or "/abschluss/unterlagen/mwst?jahr=2026&format=pdf" in page

"""Jahresabschluss: the checklist, stocks booked to their value per 31.12., private shares, Privat accounts."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from aeradex import api, jahresabschluss
from aeradex.book import Book, BookError
from aeradex.ledger import BalanceEngine
from aeradex.testing import assert_clean, make_book


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, firma="Malerei Muster", rechtsform="Einzelfirma", eroeffnung={"1020": 20000, "2800": -20000})
    api.post_entry(b, "2026-03-02", "4000", "1020", "3000", "Farbe")
    api.post_entry(b, "2026-05-10", "1100", "3400", "10000", "Rechnung Fassade")
    return Book(b.root)


def bal(book, nr, d="2026-12-31"):
    return BalanceEngine(Book(book.root)).balance_at(nr, date.fromisoformat(d))


def test_a_stock_is_booked_to_its_value_and_again_only_the_change(book):
    api.closing_entry_book(book, 2026, "material", [{"text": "Farbe Lager", "betrag": "800"},
                                                   {"text": "Abdeckmaterial", "betrag": "400"}])
    b = Book(book.root)
    assert bal(b, "1210") == D("1200") and b.accounts["1210"].name == "Materialvorrat"
    assert bal(b, "4000") == D("1800")                                   # 3000 bought − 1200 in stock
    v = api.closing_entry(b, 2026, "material")
    assert D(v["saldo"]) == 0 and D(v["wert"]) == D("1200") and not v["veraltet"]      # saldo: without this entry
    api.closing_entry_book(b, 2026, "material", [{"text": "Farbe Lager", "betrag": "500"}])       # counted again
    b = Book(book.root)
    assert bal(b, "1210") == D("500") and len([r for r in b.rows if r.quelle == "jahresabschluss:2026:material"]) == 1
    beleg = jahresabschluss.saved(b, 2026)["buchungen"]["material"]["beleg"]
    assert {r.beleg for r in b.rows if r.quelle.startswith("jahresabschluss:")} == {beleg}       # same Beleg
    assert_clean(b)
    # next year: only the change against the carried balance
    api.closing_entry_book(b, 2027, "material", wert="650")
    b = Book(book.root)
    assert bal(b, "1210", "2027-12-31") == D("650")
    [r] = [r for r in b.rows if r.quelle == "jahresabschluss:2027:material"]
    assert (r.soll, r.haben, r.betrag) == ("1210", "4000", D("150"))
    assert_clean(b)


def test_credit_side_stocks_and_changed_books(book):
    v = api.closing_entry(book, 2026, "delkredere")
    assert D(v["vorschlaege"][0]["betrag"]) == D("500")                  # 5 % of 10'000 open
    api.closing_entry_book(book, 2026, "delkredere", wert="500")
    b = Book(book.root)
    assert bal(b, "1109") == D("-500") and bal(b, "3805") == D("500")
    api.closing_entry_book(b, 2026, "arbeiten", [{"text": "Fassade Meier, 60 %", "betrag": "4200"}])
    b = Book(book.root)
    assert bal(b, "1280") == D("4200") and bal(b, "3940") == D("-4200")
    api.post_entry(b, "2026-12-20", "1280", "1020", "100", "Korrektur")   # someone books on the account
    assert api.closing_entry(Book(book.root), 2026, "arbeiten")["veraltet"]
    steps = {s["key"]: s for p in api.closing_checklist(Book(book.root), 2026)["phasen"] for s in p["schritte"]}
    assert steps["arbeiten"]["status"] == "offen" and steps["delkredere"]["status"] == "ok"
    api.closing_entry_remove(Book(book.root), 2026, "arbeiten")
    b = Book(book.root)
    assert bal(b, "1280") == D("100") and not any(r.quelle == "jahresabschluss:2026:arbeiten" for r in b.rows)
    assert_clean(b)


def test_private_shares_go_to_privat_and_the_vehicle_share_follows_estv(book):
    assert jahresabschluss.fahrzeug_anteil("40000", 12) == D("4320.00")      # 0.9 % × 40'000 × 12
    assert jahresabschluss.fahrzeug_anteil("10000", 6) == D("900.00")        # minimum CHF 150 a month
    api.closing_entry_book(book, 2026, "privatanteile", [
        {"art": "fahrzeug", "text": "Fahrzeug VW Caddy", "betrag": "4320"},
        {"art": "telefon", "text": "Natel", "betrag": "240"}])
    b = Book(book.root)
    assert bal(b, "2850") == D("4560") and bal(b, "6270") == D("-4320") and bal(b, "6512") == D("-240")
    assert_clean(b)
    with pytest.raises(BookError, match="Zeile 1: Konto"):
        api.closing_entry_book(b, 2026, "privatanteile", [{"art": "andere", "text": "x", "betrag": "1"}])


def test_a_new_privat_account_closes_into_equity(book):
    api.add_account(book, "2853", "Privat Spesen Inhaberin")
    b = Book(book.root)
    assert b.accounts["2853"].abschluss == "2800"
    api.account_update(b, "2853", abschluss="")
    steps = {s["key"]: s for p in api.closing_checklist(Book(book.root), 2026)["phasen"] for s in p["schritte"]}
    assert steps["privat_eigenkapital"]["status"] == "offen" and "2853" in steps["privat_eigenkapital"]["text"]
    api.account_update(Book(book.root), "2853", abschluss="2800")
    with pytest.raises(BookError, match="kein Eigenkapitalkonto"):
        api.account_update(Book(book.root), "2853", abschluss="1020")


def test_the_checklist_counts_ticks_and_respects_the_lock(book):
    cl = api.closing_checklist(book, 2026)
    phases = [p["key"] for p in cl["phasen"]]
    assert phases == ["laufend", "bewerten", "buchen", "kontrolle", "abschliessen"]
    before = cl["erledigt"]
    api.closing_mark(book, 2026, "saldenliste", notiz="mit Treuhand durchgesehen", von="Delia")
    cl = api.closing_checklist(Book(book.root), 2026)
    step = next(s for p in cl["phasen"] for s in p["schritte"] if s["key"] == "saldenliste")
    assert step["erledigt"] and step["markiert"]["notiz"] == "mit Treuhand durchgesehen"
    assert cl["erledigt"] == before + 1
    with pytest.raises(BookError, match="Unbekannter Schritt"):
        api.closing_mark(Book(book.root), 2026, "gibtsnicht")
    assert jahresabschluss.due_year(Book(book.root), date(2026, 11, 30)) is None
    assert jahresabschluss.due_year(Book(book.root), date(2026, 12, 1)) == 2026
    api.closing_entry_book(Book(book.root), 2026, "material", wert="100")
    api.lock(Book(book.root), "2026-12-31")
    assert jahresabschluss.due_year(Book(book.root), date(2027, 1, 5)) is None
    with pytest.raises(BookError, match="gesperrt"):
        api.closing_entry_book(Book(book.root), 2026, "material", wert="200")
    assert_clean(Book(book.root))


def test_pages(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/abschluss?jahr=2026").text
        assert "Abschluss 2026" in page and "/abschluss/buchung/material?jahr=2026" in page
        assert "Materialvorrat" in c.get("/abschluss/buchung/material?jahr=2026").text
        assert "Fahrzeug" in c.get("/abschluss/buchung/privatanteile?jahr=2026").text
        r = c.post("/abschluss/buchung/material", headers=h,
                   data={"jahr": "2026", "text": ["Farbe", ""], "betrag": ["300", ""]})
        assert r.status_code == 204, r.text
        r = c.post("/abschluss/schritt", headers=h, data={"jahr": "2026", "schritt": "jahresrechnung", "erledigt": "1"})
        assert r.status_code in (204, 200), r.text
        r = c.post("/abschluss/buchung/material/entfernen", headers=h, data={"jahr": "2026"})
        assert r.status_code == 204, r.text
    assert_clean(Book(book.root))

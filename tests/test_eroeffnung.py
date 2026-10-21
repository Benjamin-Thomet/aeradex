"""Geschäftsjahre (bookings only in open years, moving the first year) and the Eröffnungsbilanz taken over
from the previous bookkeeping (closing figures → opening balance, import of a Saldenliste)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from aeradex import api, eroeffnung, jahreswechsel
from aeradex.book import Book, BookError
from aeradex.ledger import BalanceEngine
from aeradex.testing import assert_clean, make_book


@pytest.fixture
def book(tmp_path):
    return make_book(tmp_path, firma="Malerei Muster", rechtsform="Einzelfirma", jahr=2027)


def test_bookings_only_in_open_years(book):
    with pytest.raises(BookError, match="vor dem ersten Geschäftsjahr 2027"):
        api.post_entry(book, "2017-03-02", "6500", "1020", "10", "Tippfehler")
    assert jahreswechsel.year_problem(book, date(2028, 1, 5), today=date(2027, 12, 20)).endswith(
        "zuerst das Geschäftsjahr 2028 eröffnen (Einstellungen › Geschäftsjahre).")
    assert "Datum prüfen" in jahreswechsel.year_problem(book, date(2029, 1, 5), today=date(2027, 12, 20))
    assert jahreswechsel.year_problem(book, date(2028, 1, 5), today=date(2028, 1, 2)) is None   # calendar year is open
    api.post_entry(book, "2027-01-05", "6500", "1020", "10", "Papier")
    ov = api.years_overview(Book(book.root))
    assert ov["erstes"] == 2027 and ov["jahre"][0]["buchungen"] == 1


def test_first_year_moves_only_while_nothing_lies_before(tmp_path):
    b = make_book(tmp_path, jahr=2025)
    api.first_year_set(b, 2026)
    b = Book(b.root)
    assert b.settings.erstes_jahr == 2026 and b.years()[0] == 2026
    api.post_entry(b, "2026-02-01", "6500", "1020", "10", "x")
    api.first_year_set(Book(b.root), 2025)                         # earlier is fine: nothing changes
    with pytest.raises(BookError, match="Buchungen in 2026"):
        api.first_year_set(Book(b.root), 2027)
    assert_clean(Book(b.root))


def test_closing_figures_become_the_opening_balance(book):
    # old Schlussbilanz 31.12.2026 of an Einzelfirma: Privat and the year's profit not yet in equity
    api.opening_set(book, {"1020": "35000", "1100": "8000", "2000": "3000", "2800": "30000", "2850": "-12000",
                           "3400": "90000", "4000": "20000", "6000": "48000"})
    b = Book(book.root)
    acc = b.accounts
    assert acc["2800"].eroeffnung == D("-40000")                  # 30'000 − 12'000 Privat + 22'000 Gewinn
    assert acc["2850"].eroeffnung == 0 and acc["2850"].vorjahr == D("12000")
    assert acc["3400"].vorjahr == D("-90000") and acc["3400"].eroeffnung == 0
    st = api.statement(b, 2027)
    assert st["differenz"] == "0.00"
    total_a = next(r for r in st["aktiven"] if r["stil"] == "total")
    assert D(total_a["vorjahr"]) == D("43000") and D(total_a["aktuell"]) == D("43000")
    assert BalanceEngine(b).balance("1020", 2027) == D("35000")
    assert_clean(b)
    with pytest.raises(BookError, match="geht nicht auf"):
        api.opening_set(b, {"1020": "36000"})


def test_import_detects_columns_and_signs(book):
    csv = ("Saldenliste per 31.12.2026\n"
           "Konto;Bezeichnung;Saldo\n"
           "1;Aktiven;43'000.00\n"
           "1020;Bank;35'000.00\n1100;Debitoren;8'000.00\n"
           "2000;Kreditoren;3'000.00\n2800;Eigenkapital;18'000.00\n2979;Jahresgewinn;22'000.00\n"
           "Total;;0\n").encode()
    p = api.opening_read(book, "saldenliste.csv", csv)
    assert p["konvention"] == "gelesen" and D(p["werte"]["2000"]) == D("3000")
    assert any("übersprungen" in h for h in p["hinweise"])
    assert eroeffnung.staged(Book(book.root))
    api.opening_set(Book(book.root), p["werte"], p["neu"])
    b = Book(book.root)
    assert b.accounts["2800"].eroeffnung == D("-40000")
    assert_clean(b)


def test_import_with_soll_haben_and_a_new_account(book):
    text = ("Kto\tName\tSoll\tHaben\n1020\tBank\t5000.00\t\n1025\tBank Sparkonto\t1000,00\t\n"
            "2800\tEigenkapital\t\t6000.00\n").encode()
    p = api.opening_read(book, "eingefuegt.txt", text)
    assert p["konvention"] == "vorzeichen" and p["neu"] == [{"nr": "1025", "name": "Bank Sparkonto", "klasse": "aktiv"}]
    api.opening_set(Book(book.root), p["werte"], p["neu"])
    b = Book(book.root)
    assert b.accounts["1025"].name == "Bank Sparkonto" and b.accounts["1025"].eroeffnung == D("1000")
    assert_clean(b)


def test_numbers():
    assert eroeffnung.number("1'234.50") == D("1234.50")
    assert eroeffnung.number("-1.234,50") == D("-1234.50")
    assert eroeffnung.number("(12.00)") == D("-12.00")
    assert eroeffnung.number("1 234,5 CHF") == D("1234.5")
    assert eroeffnung.number("Bank") is None


def test_locked_first_year_keeps_its_opening(book):
    api.opening_set(book, {"1020": "1000", "2800": "1000"})
    api.lock(Book(book.root), "2027-12-31")
    with pytest.raises(BookError, match="gesperrt"):
        api.opening_set(Book(book.root), {"1020": "2000", "2800": "2000"})
    with pytest.raises(BookError, match="gesperrt"):
        api.first_year_set(Book(book.root), 2026)


def test_pages(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/einstellungen").text
        assert 'id="jahre"' in page and "Eröffnungsbilanz 2027" in page and "/eroeffnung" in page
        assert "Bilanz per 31.12.2026" in c.get("/eroeffnung").text
        r = c.post("/eroeffnung/einlesen", headers=h, data={"text": "1020;Bank;5000\n1025;Sparkonto;1000\n2800;EK;6000\n"})
        assert r.status_code == 204, r.text
        page = c.get("/eroeffnung").text
        assert "Sparkonto" in page and "Konten, die im Kontenplan fehlen" in page and 'class="row changed"' in page
        r = c.post("/eroeffnung", headers=h, data={"w_1020": "5000", "w_2800": "6000", "neu_nr": "1025",
                                                   "neu_name_1025": "Sparkonto", "n_1025": "1000", "neu_ziel_1025": ""})
        assert r.status_code == 204, r.text
        assert "Konten, die im Kontenplan fehlen" not in c.get("/eroeffnung").text
        r = c.post("/eroeffnung", headers=h, data={"w_1020": "7000"})
        assert "geht nicht auf" in r.text
        r = c.post("/einstellungen/jahre", headers=h, data={"aktion": "erstes", "erstes_jahr": "2026"})
        assert r.status_code == 204, r.text
    b = Book(book.root)
    assert b.accounts["1025"].eroeffnung == D("1000") and b.settings.erstes_jahr == 2026
    assert_clean(b)

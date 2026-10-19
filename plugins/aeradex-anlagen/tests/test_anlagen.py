from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, check
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from aeradex_anlagen import anlagen


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["anlagen"], eroeffnung={"1020": 10000, "2800": -10000})
    api.post_entry(b, "2026-03-15", "1520", "1020", "2400", "Laptop Lea")
    return Book(b.root)


def saldo(book, konto, year=2026):
    return Decimal(api.ledger(Book(book.root), konto, year)["saldo"])


def add_laptop(book, **kw):
    kw.setdefault("methode", "degressiv")
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
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert 'href="/p/anlagen/anlagen"' in c.get("/abschluss").text                   # Plugins navigation
        r = c.post("/p/anlagen/anlagen/erfassen", headers=h,
                   data={"bezeichnung": "Laptop Lea", "datum": "2026-03-15", "wert": "2400", "kategorie": "edv",
                         "methode": "degressiv"})
        assert r.status_code == 204, r.text
        page = c.get("/p/anlagen/anlagen?jahr=2026").text
        assert "A0001" in page and "Abschreibungen 2026 buchen" in page
        r = c.post("/p/anlagen/anlagen/abschreiben", headers=h, data={"jahr": "2026"})
        assert r.status_code == 204, r.text
        assert c.get("/p/nichtda/x").status_code == 404
    assert saldo(book, "6800") == Decimal("800.00")


# ---------- linear default, purchases, takeover, changes ----------

D = Decimal


def W(book, fn, *args, **kw):
    return api.write(Book(book.root), "test", fn, *args, **kw)["ergebnis"]


@pytest.fixture
def firm(tmp_path):
    """An Einzelfirma with VAT, a supplier and a bill for a spray gun on 1500 (gross 4864.50 incl. 8.1 %)."""
    b = make_book(tmp_path, plugins=["anlagen"], rechtsform="Einzelfirma", eroeffnung={"1020": 30000, "2800": -30000},
                  uid="CHE-123.456.789", iban="CH93 0076 2011 6238 5295 7")
    api.settings_update(b, mwst={"methode": "effektiv"})
    api.supplier_add(Book(b.root), name="Malerbedarf AG", iban="CH7409000000300012345", konto="4000")
    api.bill_add(Book(b.root), "L0001", "4864.50", datum="2026-04-10", konto="1500", mwst="I81",
                 positionen=[{"konto": "1500", "betrag": "4864.50", "text": "Airless-Spritzgerät", "mwst": "I81"}])
    return Book(b.root)


def test_linear_is_the_default_and_a_useful_life_sets_the_rate(book):
    meta = W(book, anlagen.add, "Laptop Lea", "2026-03-15", "2400", "edv")
    assert (meta["methode"], meta["satz"], meta["nutzungsdauer"]) == ("linear", "20", "5")
    assert anlagen.preview(Book(book.root), 2026)[0]["betrag"] == D("400.00")                 # 2400 × 20 % × 10/12
    meta = W(book, anlagen.add, "Drucker", "2026-01-01", "900", "", "1520", nutzungsdauer="3")
    assert meta["satz"] == "33.33" and anlagen.depreciation(meta, 2026) == D("299.97")
    with pytest.raises(BookError, match="Nutzungsdauer gibt es nur"):
        W(book, anlagen.add, "X", "2026-01-01", "900", "", "1520", "degressiv", nutzungsdauer="3")
    assert anlagen.useful_life("geruest") == D("4.4") and anlagen.KATEGORIEN["geruest"][1] == "1540"


def test_linear_ends_at_zero(book):
    meta = W(book, anlagen.add, "Monitor", "2026-01-01", "300", "", "1520", nutzungsdauer="2")
    for year in (2026, 2027, 2028):
        api.post_entry(Book(book.root), f"{year}-01-05", "6500", "1020", "1", "x")
        if anlagen.preview(Book(book.root), year):
            W(book, anlagen.depreciate, year)
    meta = anlagen.asset(Book(book.root), "A0001")
    assert anlagen.booked(meta) == {2026: D("150.00"), 2027: D("150.00")} and anlagen.book_value(meta) == 0


def test_a_booked_purchase_becomes_an_asset_with_its_net_amount(firm):
    [k] = anlagen.kandidaten(firm)
    assert (k["beleg"], k["konto"], k["rest"], k["kategorie"]) == ("E-2026-0001", "1500", D("4500.00"), "maschinen")
    assert k["bezeichnung"] == "Airless-Spritzgerät"
    hints = [i.message for i in check.run(firm) if i.level == "hinweis"]
    assert any("Beleg E-2026-0001 auf 1500 (4500.00) ist noch keine Anlage" in h for h in hints)
    from aeradex_anlagen import aeradex_nach_beleg
    assert aeradex_nach_beleg(firm, "E-2026-0001") == "/p/anlagen/anlagen?beleg=E-2026-0001#kandidaten"
    with pytest.raises(BookError, match="höher"):
        W(firm, anlagen.add_from_booking, "E-2026-0001", "1500", wert="5000")
    part = W(firm, anlagen.add_from_booking, "E-2026-0001", "1500", "Spritzgerät", wert="3000")
    assert (part["datum"], part["wert"], part["beleg"], part["kategorie"]) == ("2026-04-10", "3000.00", "E-2026-0001",
                                                                                "maschinen")
    assert anlagen.kandidaten(Book(firm.root))[0]["rest"] == D("1500.00")
    W(firm, anlagen.add_from_booking, "E-2026-0001", "1500", "Schlauchset", "werkzeuge")
    b = Book(firm.root)
    assert anlagen.kandidaten(b) == [] and aeradex_nach_beleg(b, "E-2026-0001") is None
    assert not [i for i in check.run(b) if i.where == "anlagen"]                          # balance = assets
    assert_clean(b)
    with pytest.raises(BookError, match="keine Anschaffung"):
        W(firm, anlagen.add_from_booking, "E-2026-0001", "1500")


def test_a_storno_and_keine_anlage_remove_a_candidate(book):
    api.post_entry(Book(book.root), "2026-05-01", "1540", "1020", "200", "Umbuchung")
    cands = {k["konto"]: k for k in anlagen.kandidaten(Book(book.root))}
    assert set(cands) == {"1520", "1540"}                                                # the laptop and this one
    W(book, anlagen.ignore, cands["1540"]["beleg"], "1540")
    assert [k["konto"] for k in anlagen.kandidaten(Book(book.root))] == ["1520"]
    api.reverse_entry(Book(book.root), cands["1520"]["beleg"])
    assert anlagen.kandidaten(Book(book.root)) == []


def test_takeover_with_contribution_and_from_the_opening_balance(book, tmp_path):
    with pytest.raises(BookError, match="Gegenkonto"):                                    # a GmbH: say where from
        W(book, anlagen.add_existing, "Lieferwagen", "2023-01-01", "40000", "16000", None, "fahrzeuge", einlage=True)
    book = make_book(tmp_path / "e", plugins=["anlagen"], rechtsform="Einzelfirma")
    van = W(book, anlagen.add_existing, "Lieferwagen", "2023-01-01", "40000", "16000", None, "fahrzeuge",
            einlage=True)
    assert van["uebernahme"] == {"per": "2026-01-01", "buchwert": "16000.00", "einlage": True, "gegenkonto": "2800"}
    b = Book(book.root)
    assert saldo(b, "1530") == D("16000.00") and anlagen.kandidaten(b) == []
    assert anlagen.preview(b, 2026)[0]["betrag"] == D("8000.00")                         # 40000 × 20 %
    line = anlagen.schedule(b, 2026)["anlagen"][0]
    assert (line["anfang"], line["zugang"], line["einlage"]) == (0, D("16000.00"), True)
    with pytest.raises(BookError, match="Eröffnungsbilanz"):
        W(book, anlagen.add_existing, "Gerüst", "2020-01-01", "8000", "2000", "2026-03-01", "geruest")
    W(book, anlagen.add_existing, "Gerüst", "2020-01-01", "8000", "2000", None, "geruest")
    b = Book(book.root)
    g = anlagen.schedule(b, 2026)["anlagen"][1]
    assert (g["anfang"], g["zugang"]) == (D("2000.00"), 0)
    assert anlagen.depreciation(anlagen.asset(b, "A0002"), 2026) == D("1800.00")         # 8000 × 22.5 %
    with pytest.raises(BookError, match="Kaufdatum"):
        W(book, anlagen.add_existing, "Neu", "2026-05-01", "800", "700", None, "edv")
    W(book, anlagen.remove, "A0001")                                                      # the contribution goes too
    b = Book(book.root)
    assert saldo(b, "1530") == 0 and "A0001" not in anlagen.assets(b)
    assert_clean(b)


def test_change_remove_and_adjusted_depreciation(book):
    add_laptop(book, methode="linear")
    W(book, anlagen.add, "Tablet", "2026-06-01", "600", "edv")
    W(book, anlagen.update, "A0002", wert="700", nutzungsdauer="2", notiz="SN 123")
    meta = anlagen.asset(Book(book.root), "A0002")
    assert (meta["wert"], meta["satz"], meta["notiz"]) == ("700.00", "50", "SN 123")
    W(book, anlagen.remove, "A0002")
    with pytest.raises(BookError, match="zwischen 0 und"):
        W(book, anlagen.depreciate, 2026, {"A0001": "2500"})
    todo = W(book, anlagen.depreciate, 2026, {"A0001": "1000"})
    assert D(todo[0]["berechnet"]) == D("400.00") and saldo(book, "6800") == D("1000.00")
    with pytest.raises(BookError, match="nur änderbar"):
        W(book, anlagen.update, "A0001", wert="2000")
    with pytest.raises(BookError, match="Abgang buchen"):
        W(book, anlagen.remove, "A0001")
    W(book, anlagen.update, "A0001", bezeichnung="Laptop Lea (alt)", nutzungsdauer="4")
    assert anlagen.asset(Book(book.root), "A0001")["satz"] == "25"
    assert_clean(Book(book.root))


def test_dossier_part_and_report(book):
    add_laptop(book, methode="linear")
    W(book, anlagen.depreciate, 2026)
    from aeradex import dossier, reports
    rep = reports.run(Book(book.root), "anlagenspiegel", jahr="2026")
    assert rep["zeilen"][-1]["werte"]["ende"] == D("2000.00")
    name, data = dossier.build_part(Book(book.root), 2026, "anlagen", "csv")
    assert "Anlagenspiegel 2026" in name and b"Laptop Lea" in data
    name, data = dossier.build_part(Book(book.root), 2026, "anlagen", "pdf")
    assert data.startswith(b"%PDF")


def test_page_candidate_takeover_and_redirect_after_a_bill(firm):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(firm.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/p/anlagen/anlagen").text
        assert 'id="kandidaten"' in page and "Airless-Spritzgerät" in page
        assert "1 Anschaffung als Anlage erfassen" in c.get("/").text                     # to-do on the Übersicht
        r = c.post("/kreditoren/neu", headers=h, data={"lieferant": "L0001", "betrag": "1081.00", "datum": "2026-05-02",
                                                         "konto": "1540", "mwst": "I81"})
        assert r.status_code == 204, r.text
        assert r.headers["HX-Redirect"].startswith("/p/anlagen/anlagen?beleg=E-2026-0002")
        r = c.post("/p/anlagen/anlagen/aus_beleg", headers=h, data={"beleg": "E-2026-0002", "konto": "1540",
                                                                      "bezeichnung": "Leitern", "kategorie": "geruest"})
        assert r.status_code == 204, r.text
        r = c.post("/p/anlagen/anlagen/uebernehmen", headers=h,
                   data={"bezeichnung": "Lieferwagen", "datum": "2022-03-01", "wert": "38000", "buchwert": "15000",
                         "kategorie": "fahrzeuge", "einlage": "1"})
        assert r.status_code == 204, r.text
        page = c.get("/p/anlagen/anlagen?tab=bestand").text
        assert "Leitern" in page and "Einlage" in page and "Bestand übernehmen" in page
        r = c.post("/p/anlagen/anlagen/abschreiben", headers=h, data={"jahr": "2026", "betrag_A0002": "100"})
        assert r.status_code == 204, r.text
    b = Book(firm.root)
    assert anlagen.booked(anlagen.asset(b, "A0002"))[2026] == D("100.00")
    assert_clean(b)


def test_todo_reminds_of_the_depreciation_from_december(book):
    from datetime import date as _date
    from aeradex_anlagen import todos
    [k] = anlagen.kandidaten(Book(book.root))
    W(book, anlagen.add_from_booking, k["beleg"], "1520", "Laptop Lea", "edv")
    assert [t.titel for t in todos(Book(book.root), _date(2026, 11, 30))] == []
    [t] = todos(Book(book.root), _date(2026, 12, 1))
    assert (t.titel, t.level, t.href) == ("Abschreibungen 2026 buchen", "info", "/p/anlagen/anlagen?jahr=2026#spiegel")
    assert "400.00" in t.detail
    assert todos(Book(book.root), _date(2027, 1, 15))[0].level == "warn"
    W(book, anlagen.depreciate, 2026)
    assert todos(Book(book.root), _date(2027, 1, 15)) == []


def test_book_values_rounded_to_100(book):
    [k] = anlagen.kandidaten(Book(book.root))
    W(book, anlagen.add_from_booking, k["beleg"], "1520", "Laptop Lea", "edv", wert="2387.35")   # 20 %, 10 months
    W(book, anlagen.add, "Maus", "2026-01-01", "130", "", "1520", nutzungsdauer="5")             # 26 a year
    assert anlagen.preview(Book(book.root), 2026)[0]["betrag"] == D("397.89")                 # to the cent
    W(book, anlagen.save_settings, 100)
    pv = {p["nummer"]: p["betrag"] for p in anlagen.preview(Book(book.root), 2026)}
    assert pv["A0001"] == D("387.35")                                                       # 2387.35 → 2000
    assert pv["A0002"] == D("30.00")                                                        # 104 → down to 100
    W(book, anlagen.depreciate, 2026)
    b = Book(book.root)
    assert anlagen.schedule(b, 2026)["total"]["ende"] == D("2100.00")
    api.post_entry(b, "2027-01-05", "6500", "1020", "1", "x")
    assert anlagen.preview(Book(book.root), 2027)[0]["betrag"] == D("500.00")               # 477.47 → 1500
    assert anlagen.round_to_step(D("100"), D("26"), D("0"), D("100")) == D("100")            # small: down to 0
    with pytest.raises(BookError, match="Rundung"):
        W(book, anlagen.save_settings, 50)

import re
from decimal import Decimal

import pytest

from allkvitt import api, check, invoices
from allkvitt.book import Book, BookError
from allkvitt.testing import assert_clean, make_book
from allkvitt_leistungen import abrechnung, daten, kontrolle, offerten


def _dec(value):
    """api.write returns JSON-ready data; numbers come back as Decimal for the asserts."""
    if isinstance(value, dict):
        return {k: _dec(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dec(v) for v in value]
    if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value):
        return Decimal(value)
    return value


def W(book, fn, *args, **kw):
    return _dec(api.write(Book(book.root), "test", fn, *args, **kw).get("ergebnis"))


FIRMA = dict(strasse="Hauptstrasse", nr="1", plz="3600", ort="Thun", iban="CH93 0076 2011 6238 5295 7")


def saldo(book, konto, year=2026):
    return Decimal(api.ledger(Book(book.root), konto, year)["saldo"])


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["leistungen"], eroeffnung={"1020": 10000, "2800": -10000}, **FIRMA)
    api.customer_add(b, name="Anna Beispiel", firma="Beispiel AG", strasse="Marktgasse", nr="5", plz="3011", ort="Bern")
    api.employee_add(Book(b.root), "Lea", "Muster", monatslohn=6000, pensum=100, vollzeit_stunden_woche=42,
                     eintritt="2026-01-01")
    api.employee_add(Book(b.root), "Tom", "Stunde", lohnart="stunde", stundenlohn=32, eintritt="2026-01-01")
    W(b, daten.set_person, "M0001", 120, 70)
    W(b, daten.set_person, "M0002", 80)
    W(b, daten.add_product, "Dispersionsfarbe weiss", "45", "l")
    return Book(b.root)


def test_rate_hierarchy(book):
    assert W(book, daten.add_time, "2026-10-01", "M0001", "2", "Beratung", "K0001")["preis"] == Decimal("120.00")
    api.customer_update(Book(book.root), "K0001", stundensatz="150")
    assert W(book, daten.add_time, "2026-10-01", "M0001", "1", "Beratung", "K0001")["preis"] == Decimal("150.00")
    W(book, daten.add_project, "K0001", "Fassade", satz="135")
    assert W(book, daten.add_time, "2026-10-02", "M0001", "1", "Fassade", "", "P0001")["preis"] == Decimal("135.00")
    assert W(book, daten.add_time, "2026-10-02", "M0001", "1", "x", "K0001", "", True, "", "99")["preis"] == Decimal("99.00")


def test_person_without_payroll_and_missing_rate(book):
    p = W(book, daten.set_person, "", "140", None, "Inhaber Ben", 42)
    assert p["nummer"] == "X01"
    assert daten.person(Book(book.root), "X01")["name"] == "Inhaber Ben"
    W(book, daten.set_person, "", None, None, "Lernende")
    with pytest.raises(BookError, match="Kein Stundensatz"):
        W(book, daten.add_time, "2026-10-01", "X02", "1", "Hilfe", "K0001")


def test_not_billable_needs_no_customer_and_stays_out_of_billing(book):
    e = W(book, daten.add_time, "2026-10-01", "M0001", "8.4", "", "", "", False, "Ferien")
    assert e["preis"] is None and e["kategorie"] == "Ferien"
    with pytest.raises(BookError, match="Kunden"):
        W(book, daten.add_time, "2026-10-01", "M0001", "1", "x")
    with pytest.raises(BookError, match="Kategorie"):
        W(book, daten.add_time, "2026-10-01", "M0001", "1", "x", "", "", False, "Party")
    assert abrechnung.select(Book(book.root)) == []
    assert_clean(Book(book.root))


def test_customer_price(book):
    W(book, daten.update_product, "P001", kunde="K0001", kundenpreis="40")
    assert W(book, daten.add_material, "2026-10-01", "K0001", "P001", "3")["preis"] == Decimal("40.00")
    api.customer_add(Book(book.root), name="Peter Privat", strasse="Weg", nr="2", plz="3000", ort="Bern")
    assert W(book, daten.add_material, "2026-10-01", "K0002", "P001", "1")["preis"] == Decimal("45.00")


def test_billing_creates_debitor_and_marks_entries(book):
    W(book, daten.add_time, "2026-10-01", "M0001", "2.5", "Beratung", "K0001")
    W(book, daten.add_time, "2026-10-02", "M0001", "1.5", "Offerte besprochen", "K0001")
    W(book, daten.add_time, "2026-10-02", "M0002", "4", "Abdecken", "K0001")
    W(book, daten.add_material, "2026-10-02", "K0001", "P001", "10")
    ids = [e["id"] for e in abrechnung.select(Book(book.root), "K0001")]
    pv = abrechnung.preview(Book(book.root), ids)
    assert [(str(p["menge"]), p["preis"]) for p in pv["positionen"]] == [
        ("4.0", Decimal("120.00")), ("4", Decimal("80.00")), ("10", Decimal("45.00"))]
    assert pv["total"] == Decimal("1250.00")            # 480 + 320 + 450
    res = W(book, abrechnung.bill, ids, datum="2026-10-31")
    b = Book(book.root)
    assert res["rechnung"] == "R-2026-0001" and res["total"] == Decimal("1250.00")
    assert saldo(b, "1100") == Decimal("1250.00")
    assert saldo(b, "3400") == Decimal("-800.00") and saldo(b, "3200") == Decimal("-450.00")
    assert (b.root / "rechnungen/2026/R-2026-0001-rapport.pdf").exists()
    assert {e["status"] for e in daten.with_state(b)} == {"abgerechnet"}
    assert abrechnung.select(b) == []
    with pytest.raises(BookError, match="abgerechnet"):
        W(b, daten.update_entry, ids[0], menge="3")
    with pytest.raises(BookError, match="Nicht offen"):
        W(b, abrechnung.bill, ids)
    assert_clean(b)


def test_detail_positions_and_one_customer_per_invoice(book):
    api.customer_add(Book(book.root), name="Peter Privat", strasse="Weg", nr="2", plz="3000", ort="Bern")
    a = W(book, daten.add_time, "2026-10-01", "M0001", "1", "A", "K0001")["id"]
    b = W(book, daten.add_time, "2026-10-01", "M0001", "1", "B", "K0002")["id"]
    with pytest.raises(BookError, match="einen Kunden"):
        abrechnung.preview(Book(book.root), [a, b])
    pv = abrechnung.preview(Book(book.root), [a], detail=True)
    assert pv["positionen"][0]["text"] == "01.10.2026 Lea Muster: A"


def test_void_reopens_entries(book):
    eid = W(book, daten.add_time, "2026-10-01", "M0001", "2", "Beratung", "K0001")["id"]
    W(book, abrechnung.bill, [eid])
    api.invoice_void(Book(book.root), "R-2026-0001", "falsch")
    b = Book(book.root)
    assert [e["id"] for e in abrechnung.select(b)] == [eid]
    assert any(i.level == "hinweis" and "wieder offen" in i.message for i in check.run(b))
    W(b, daten.update_entry, eid, menge="3")
    assert W(b, abrechnung.bill, [eid])["rechnung"] == "R-2026-0002"
    assert daten.entry(Book(book.root), eid)["rechnung"] == "R-2026-0002"
    assert_clean(Book(book.root))


def test_hand_edited_billed_entry_is_caught(book):
    eid = W(book, daten.add_time, "2026-10-01", "M0001", "2", "Beratung", "K0001")["id"]
    W(book, abrechnung.bill, [eid])
    path = next((book.root / "leistungen/erfassung").glob("*.md"))
    path.write_text(path.read_text().replace("|     2 |", "|     5 |"))
    assert any(i.level == "warnung" and "R-2026-0001" in i.message for i in check.run(Book(book.root)))


def test_quote_flat_rate_partial_and_final_invoice(book):
    q = W(book, offerten.create, "K0001",
          [{"produkt": "P001", "menge": "20"}, {"stunden": "16", "text": "Malerarbeiten", "wer": "M0001"}],
          "2026-09-01", "Wohnzimmer streichen")
    assert q["nummer"] == "O-2026-0001" and q["netto"] == Decimal("2820.00")        # 900 + 1920
    assert (book.root / "offerten/2026/O-2026-0001.pdf").exists()
    W(book, offerten.set_status, "O-2026-0001", "versendet")
    q = W(book, offerten.set_status, "O-2026-0001", "angenommen")
    proj = daten.project(Book(book.root), q["projekt"])
    assert proj["abrechnung"] == "pauschal" and proj["budget_stunden"] == Decimal("16")
    assert proj["satz"] == Decimal("120.00")
    teil = W(book, offerten.invoice, "O-2026-0001", anteil="30", datum="2026-09-15")
    assert teil["art"] == "teil" and teil["netto"] == Decimal("846.00")
    # time recorded on a flat-rate project is controlled, not billed
    W(book, daten.add_time, "2026-09-20", "M0001", "18", "Malen", "", q["projekt"])
    assert abrechnung.select(Book(book.root)) == []
    final = W(book, offerten.invoice, "O-2026-0001", schluss=True, datum="2026-09-30")
    assert final["art"] == "schluss" and final["netto"] == Decimal("1974.00") and final["offen_nach"] == 0
    b = Book(book.root)
    texts = [p["text"] for p in invoices.invoice(b, final["rechnung"])["positionen"]]
    assert "abzüglich Teilrechnung R-2026-0001 (Anteil Material)" in texts
    assert "abzüglich Teilrechnung R-2026-0001 (Anteil Arbeit)" in texts
    assert saldo(b, "3400") + saldo(b, "3200") == Decimal("-2820.00")
    with pytest.raises(BookError, match="vollständig"):
        W(b, offerten.invoice, "O-2026-0001")
    st = kontrolle.project(b, q["projekt"])
    assert st["abgerechnet"] == Decimal("2820.00") and st["stunden"] == Decimal("18")
    assert st["deckungsbeitrag"] == Decimal("2820.00") - 18 * Decimal("70")
    found = check.run(b)
    assert any("Budget" in i.message for i in found)
    assert not [i for i in found if i.level in ("fehler", "warnung")]


def test_quote_time_and_material_is_budget(book):
    W(book, offerten.create, "K0001", [{"stunden": "10", "text": "Reparatur", "preis": "110"}], "2026-10-01",
      "Reparatur", abrechnung="aufwand")
    q = W(book, offerten.set_status, "O-2026-0001", "angenommen")
    with pytest.raises(BookError, match="Aufwand"):
        W(book, offerten.invoice, "O-2026-0001")
    e = W(book, daten.add_time, "2026-10-05", "M0001", "3", "Reparatur", "", q["projekt"])
    assert e["preis"] == Decimal("110.00")                  # project rate from the quote
    assert W(book, abrechnung.bill, [e["id"]])["total"] == Decimal("330.00")
    with pytest.raises(BookError, match="nicht mehr änderbar"):
        W(book, offerten.update, "O-2026-0001", titel="neu")


def test_hours_control(book):
    W(book, daten.add_time, "2026-10-01", "M0001", "8.4", "Beratung", "K0001")
    W(book, daten.add_time, "2026-10-02", "M0001", "8.4", "", "", "", False, "Intern")
    W(book, daten.set_holidays, ["2026-10-05"])
    row = next(r for r in kontrolle.month(Book(book.root), 2026, 10) if r["nummer"] == "M0001")
    assert row["soll"] == Decimal("176.40")                # 21 working days × 8.4 h
    assert row["ist"] == Decimal("16.8") and row["abrechenbar"] == Decimal("8.4") and row["quote"] == 50
    assert row["saldo"] == Decimal("-159.60")
    jan_sep = sum(kontrolle.target(daten.person(Book(book.root), "M0001"), 2026, m, set()) for m in range(1, 10))
    assert row["saldo_jahr"] == -jan_sep - Decimal("159.60")
    W(book, daten.set_holidays, None, "2026-10-01")             # control starts in October
    row = next(r for r in kontrolle.month(Book(book.root), 2026, 10) if r["nummer"] == "M0001")
    assert row["saldo_jahr"] == row["saldo"] == Decimal("-159.60")
    assert daten.settings(Book(book.root))["feiertage"] == ["2026-10-05"]
    tom = next(r for r in kontrolle.month(Book(book.root), 2026, 10) if r["nummer"] == "M0002")
    assert tom["soll"] is None and tom["stundenlohn"]


def test_hours_to_payroll(book):
    W(book, daten.add_time, "2026-10-01", "M0002", "6", "Abdecken", "K0001")
    W(book, daten.add_time, "2026-10-02", "M0002", "4", "Aufräumen", "", "", False, "Intern")
    res = W(book, kontrolle.transfer_to_payroll, 2026, 10)
    assert res[0]["stunden"] == Decimal("10")
    slip = api.payslip_show(Book(book.root), "2026-10", "M0002")
    assert Decimal(str(slip["werte"]["stunden"])) == Decimal("10")


def test_inline_customer_and_entry_edit(book):
    c = W(book, daten.add_customer, name="Neu Kunde", ort="Thun", stundensatz="95")
    assert c["nummer"] == "K0002"
    e = W(book, daten.add_time, "2026-10-03", "M0001", "1", "x", "K0002")
    assert e["preis"] == Decimal("95.00")
    moved = W(book, daten.update_entry, e["id"], datum="2026-11-01", text="y")
    assert moved["text"] == "y" and (book.root / "leistungen/erfassung/2026-11.md").exists()
    assert not (book.root / "leistungen/erfassung/2026-10.md").exists()
    W(book, daten.delete_entry, e["id"])
    assert daten.entries(Book(book.root)) == []


def test_mwst_book(tmp_path):
    b = make_book(tmp_path, plugins=["leistungen"], eroeffnung={"1020": 1000, "2800": -1000}, **FIRMA)
    api.settings_update(b, mwst={"methode": "effektiv"})
    api.customer_add(Book(b.root), name="Kunde", strasse="Weg", nr="2", plz="3000", ort="Bern")
    W(b, daten.set_person, "", "100", None, "Inhaber")
    W(b, daten.add_product, "Farbe", "50", "l", "", "U81")
    W(b, daten.add_time, "2026-10-01", "X01", "2", "Arbeit", "K0001")
    W(b, daten.add_material, "2026-10-01", "K0001", "P001", "2")
    ids = [e["id"] for e in abrechnung.select(Book(b.root))]
    assert W(b, abrechnung.bill, ids)["total"] == Decimal("324.30")     # 300 + 8.1 %
    assert_clean(Book(b.root))


def test_pages(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from allkvitt.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}

        def post(url, data):
            r = c.post(url, headers=h, data=data)
            assert r.status_code == 204, r.text
            return r

        assert 'href="/p/leistungen/leistungen"' in c.get("/debitoren").text            # tabs of Debitoren
        post("/p/leistungen/leistungen/kunde", {"name": "Neu", "ort": "Thun", "stundensatz": "100"})
        post("/p/leistungen/leistungen/person", {"name": "Inhaber", "satz": "130", "soll_woche": "40"})
        post("/p/leistungen/leistungen/zeit", {"datum": "2026-10-01", "wer": "M0001", "stunden": "2",
                                               "abrechenbar": "ja", "kunde": "K0002", "text": "Beratung"})
        post("/p/leistungen/leistungen/zeit", {"datum": "2026-10-01", "wer": "M0001", "stunden": "6",
                                               "abrechenbar": "nein", "kategorie": "Ferien"})
        post("/p/leistungen/leistungen/material", {"datum": "2026-10-01", "produkt": "P001", "menge": "2",
                                                   "kunde": "K0002"})
        for tab in ("erfassen", "abrechnen", "projekte", "kontrolle", "stammdaten"):
            assert c.get(f"/p/leistungen/leistungen?tab={tab}&monat=2026-10").status_code == 200, tab
        page = c.get("/p/leistungen/leistungen?tab=abrechnen&kunde=K0002").text
        assert "Rechnung ausstellen" in page and "290.00" in page          # 2 × 100 + 2 × 45
        assert "Lea Muster" in c.get("/p/leistungen/leistungen?tab=kontrolle&monat=2026-10&wer=M0001").text
        ids = [e["id"] for e in abrechnung.select(Book(book.root), "K0002")]
        r = post("/p/leistungen/leistungen/rechnung", {"ids": ids, "detail": "0", "rapport": "1", "datum": "2026-10-31"})
        assert r.headers["HX-Redirect"] == "/debitoren/rechnung/R-2026-0001"
        r = post("/p/leistungen/offerten/neu", {"kunde": "K0001", "titel": "Küche", "abrechnung": "pauschal",
                                                "pos_art": ["stunden", "produkt", "frei"], "pos_produkt": ["", "P001", ""],
                                                "pos_text": ["Malen", "", "Abdeckmaterial"], "pos_menge": ["5", "4", "1"],
                                                "pos_preis": ["110", "", "35"], "pos_einheit": ["", "", "Pauschal"]})
        assert r.headers["HX-Redirect"] == "/p/leistungen/offerten?nr=O-2026-0001"
        assert "Status setzen" in c.get("/p/leistungen/offerten?nr=O-2026-0001").text
        post("/p/leistungen/offerten/status", {"nummer": "O-2026-0001", "status": "angenommen"})
        page = c.get("/p/leistungen/offerten?nr=O-2026-0001").text
        assert "ganze Offerte" in page and "765.00" in page                 # 550 + 180 + 35
        post("/p/leistungen/offerten/rechnung", {"nummer": "O-2026-0001", "art": "teil", "anteil": "50"})
        post("/p/leistungen/offerten/rechnung", {"nummer": "O-2026-0001", "art": "schluss"})
        assert c.get("/p/leistungen/leistungen?tab=projekte&status=alle").status_code == 200
    assert_clean(Book(book.root))
    assert saldo(book, "1100") == Decimal("290.00") + Decimal("765.00")


def test_reports(book):
    from allkvitt import reports
    W(book, daten.add_time, "2026-10-01", "M0001", "6", "Beratung", "K0001")
    W(book, daten.add_time, "2026-10-02", "M0001", "2", "", "", "", False, "Intern")
    W(book, daten.add_material, "2026-10-02", "K0001", "P001", "4")
    W(book, daten.add_project, "K0001", "Fassade", budget_stunden="10")
    assert {"leistungen_personen", "leistungen_produkte", "leistungen_projekte"} <= set(reports.registry(Book(book.root)))
    rep = reports.run(Book(book.root), "leistungen_personen", jahr=2026, periode="10")
    lea = rep["zeilen"][0]["werte"]
    assert lea["ist"] == Decimal("8") and lea["abr"] == Decimal("6") and lea["quote"] == Decimal("75.0")
    assert lea["wert"] == Decimal("720.00") and lea["kosten"] == Decimal("560.00")
    prod = reports.run(Book(book.root), "leistungen_produkte", jahr=2026)
    assert prod["zeilen"][-1]["werte"]["offen"] == Decimal("180.00")
    assert reports.run(Book(book.root), "leistungen_projekte")["zeilen"][0]["werte"]["budget_h"] == Decimal("10")

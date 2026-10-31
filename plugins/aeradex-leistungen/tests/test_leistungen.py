import re
from decimal import Decimal

import pytest

from aeradex import api, check, invoices
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from aeradex_leistungen import abrechnung, daten, kontrolle, offerten


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
    from aeradex_leistungen import abwesenheit           # the canton's public holidays lower the target too
    hols = abwesenheit.holidays(Book(book.root), 2026)
    jan_sep = sum(kontrolle.target(daten.person(Book(book.root), "M0001"), 2026, m, hols) for m in range(1, 10))
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
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}

        def post(url, data):
            r = c.post(url, headers=h, data=data)
            assert r.status_code == 204, r.text
            return r

        assert 'href="/p/leistungen/leistungen"' in c.get("/debitoren").text            # own main menu entry
        settings = c.get("/einstellungen").text                                         # Einstellungen › Funktionen
        assert "Leistungen &amp; Offerten" in settings and "Stunden und Material erfassen" in settings
        assert 'href="/p/leistungen/offerten">Leistungen</a>' not in settings and ">Leistungen</a>" in settings
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
    from aeradex import reports
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


def test_update_quote_takes_new_customer_address(tmp_path, monkeypatch):
    from aeradex import tools as core_tools
    from aeradex.testing import make_book as _make
    import aeradex_leistungen
    b = _make(tmp_path, plugins=["leistungen"])
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    nr = core_tools.add_customer(name="Anna Meier", firma="Torfix AG", ort="Bretzwil")["kunde"]["nummer"]
    quote = aeradex_leistungen.draft_quote(nr, [{"text": "Tor", "menge": "1", "preis": "900"}], titel="Tor")
    q_nr = quote["ergebnis"]["nummer"]
    assert quote["ergebnis"]["an"]["zusatz"] == "Anna Meier"
    core_tools.update_customer(nr, name="Pascal Mühlberg")
    res = aeradex_leistungen.update_quote(q_nr, adresse_neu=True, titel="Gartentor")
    assert res["ok"] and res["ergebnis"]["an"]["zusatz"] == "Pascal Mühlberg" and res["ergebnis"]["titel"] == "Gartentor"
    assert any(str(d).endswith(".pdf") for d in res["dateien"])


def test_objects_required_and_invoice_with_the_days(book):
    regie = W(book, daten.add_product, "Regiearbeiten", "95", "h")["nummer"]          # a Leistungsart
    farbe = next(iter(daten.materials(Book(book.root))))
    W(book, daten.set_options, abrechnen_je="projekt", projekt_wort="Objekt", projekt_pflicht="1", rechnung_tage="1")
    cfg = daten.settings(Book(book.root))
    assert (cfg["projekt_wort"], cfg["projekt_pflicht"], cfg["rechnung_tage"]) == ("Objekt", True, True)
    old = W(book, daten.add_time, "2026-10-12", "M0001", "1", "Vorher", "K0001")   # before the customer had objects
    W(book, daten.add_project, "K0001", "MFH Bernstrasse 12, Thun")
    with pytest.raises(BookError, match="Beispiel AG hat Objekte: zuerst das Objekt wählen"):
        W(book, daten.add_time, "2026-10-13", "M0001", "4", "Abdecken", "K0001", leistung=regie)
    with pytest.raises(BookError, match="Objekt wählen"):
        W(book, daten.add_material, "2026-10-13", "K0001", farbe, "2")
    W(book, daten.update_entry, old["id"], text="Vorher, nachgetragen")          # old entries stay editable
    # two people on the same day: only one describes it; the same text twice is shown once
    W(book, daten.add_time, "2026-10-13", "M0001", "4", "Abdecken, Wände grundiert", "", "P0001", leistung=regie)
    W(book, daten.add_time, "2026-10-13", "M0002", "4", "", "", "P0001", leistung=regie)
    W(book, daten.add_time, "2026-10-14", "M0001", "8", "Decken 2× gestrichen", "", "P0001", leistung=regie)
    W(book, daten.add_time, "2026-10-14", "M0002", "8", "Decken 2× gestrichen", "", "P0001", leistung=regie)
    W(book, daten.add_time, "2026-10-15", "M0002", "2", "", "", "P0001", leistung=regie)   # nobody described it
    W(book, daten.add_material, "2026-10-14", "", farbe, "3", "P0001")
    b = Book(book.root)
    items = abrechnung.select(b, "K0001", "P0001")
    pre = abrechnung.preview(b, [e["id"] for e in items])
    assert [d["datum"].isoformat() for d in pre["ohne_beschreibung"]] == ["2026-10-15"]
    hours = pre["positionen"][0]
    assert hours["text"] == ("Regiearbeiten, MFH Bernstrasse 12, Thun (13.10.2026–15.10.2026)\n"
                             "13.10. Abdecken, Wände grundiert\n14.10. Decken 2× gestrichen")
    assert hours["menge"] == Decimal("26") and hours["preis"] == Decimal("95.00")
    assert pre["positionen"][1]["text"] == "Dispersionsfarbe weiss" and pre["positionen"][1]["menge"] == Decimal("3")
    res = W(book, abrechnung.bill, [e["id"] for e in items])
    import io
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO((book.root / res["pdf"]).read_bytes())).pages[0].extract_text()
    assert "13.10. Abdecken, Wände grundiert" in text and "14.10. Decken 2× gestrichen" in text
    assert_clean(Book(book.root))


def test_billing_takes_off_the_akonto_and_gives_a_discount(book):
    ak = api.invoice_create(Book(book.root), "K0001", [{"text": "1. Akonto", "preis": "300"}], "2026-09-15",
                            art="akonto")["rechnung"]["nummer"]
    W(book, daten.add_time, "2026-10-01", "M0001", "10", "Fassade", "K0001")
    ids = [e["id"] for e in abrechnung.select(Book(book.root), "K0001")]
    pv = abrechnung.preview(Book(book.root), ids, rabatt="10")
    assert [a["nummer"] for a in pv["offen_akonto"]] == [ak]
    assert pv["total"] == Decimal("780.00")                       # 1200 − 10 % − 300
    res = W(book, abrechnung.bill, ids, datum="2026-10-31", rabatt="10", akonto=[ak])
    assert res["total"] == Decimal("780.00") and "warnung" not in res
    b = Book(book.root)
    assert saldo(b, "2030") == 0 and saldo(b, "3800") == Decimal("120.00")
    assert invoices.open_akonto(b) == []
    assert_clean(b)


def test_month_end_billing_leaves_the_akonto_open_and_says_so(book):
    api.invoice_create(Book(book.root), "K0001", [{"text": "Akonto", "preis": "300"}], "2026-09-15", art="akonto")
    W(book, daten.add_time, "2026-10-01", "M0001", "2", "Fassade", "K0001")
    res = W(book, abrechnung.bill_all, "2026-10-31")
    assert len(res["warnungen"]) == 1 and len(invoices.open_akonto(Book(book.root))) == 1


def test_agent_sets_up_a_new_employee(tmp_path, monkeypatch):
    """After add_employee the agent sets rate and holidays, changes the Pensum and keeps the master data."""
    from aeradex import tools as core_tools
    import aeradex_leistungen as plugin
    b = make_book(tmp_path, plugins=["leistungen"])
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    nr = core_tools.add_employee("Eva", "Beispiel", monatslohn="5000", eintritt="2026-01-01")["mitarbeiter"]["nummer"]
    assert plugin.set_person_rates(nr, satz="95", ferien_tage="25")["ok"]
    p = daten.person(Book(b.root), nr)
    assert Decimal(str(p["satz"])) == 95 and Decimal(str(p["ferien_tage"])) == 25
    assert plugin.change_pensum(nr, "2026-07-01", "80")["ok"]
    assert plugin.add_product("Dispersionsfarbe", "12.50", "l")["ok"]
    k = api.customer_add(Book(b.root), name="Hans Huber")["kunde"]["nummer"]
    assert plugin.add_project(k, "Treppenhaus")["ok"]
    assert_clean(Book(b.root))


def test_letter_layout_for_offer_and_invoice_from_it(book):
    import io
    from pypdf import PdfReader
    api.settings_update(Book(book.root), rechnung={"layout": "brief", "unterzeichner": "Eva Muster"})
    q = W(book, offerten.create, "K0001", [{"produkt": "P001", "menge": "4"}], "2026-09-01", "Küche streichen")
    text = PdfReader(io.BytesIO((book.root / "offerten/2026/O-2026-0001.pdf").read_bytes())).pages[0].extract_text()
    assert "Offerte-Nr." in text and "Küche streichen" in text and "Eva Muster" in text and "Pos." in text
    W(book, offerten.set_status, q["nummer"], "versendet")
    W(book, offerten.set_status, q["nummer"], "angenommen")
    res = W(book, offerten.invoice, q["nummer"], datum="2026-09-20")
    inv = invoices.invoice(Book(book.root), res["rechnung"])
    assert inv["offerte"] == {"nummer": "O-2026-0001", "datum": "2026-09-01"}
    assert inv["objekt"] == "Küche streichen"                       # the project the accepted offer became
    assert_clean(Book(book.root))


def test_invoice_draft_collects_work_waits_for_prices_and_issues_like_billing(book):
    from aeradex_leistungen import entwuerfe
    regie = W(book, daten.add_product, "Regiearbeiten", "0", "h")["nummer"]      # 0: the person's rate applies
    vlies = W(book, daten.add_product, "Abdeckvlies", "0", "Rolle")["nummer"]   # price not set yet
    W(book, daten.set_person, "M0001", 120, 70)
    a = W(book, daten.add_time, "2026-10-13", "M0001", "4", "Abdecken", "K0001", leistung=regie)
    b = W(book, daten.add_time, "2026-10-13", "M0002", "4", "Abdecken", "K0001", leistung=regie)
    c = W(book, daten.add_material, "2026-10-13", "K0001", vlies, "2")
    d = W(book, entwuerfe.add, "K0001", [a["id"], b["id"]], rapporte=["R-2026-001"])
    assert d["nummer"] == "RE-0001"
    d = W(book, entwuerfe.add, "K0001", [c["id"]])                  # same customer → same draft
    assert d["nummer"] == "RE-0001" and len(d["eintraege"]) == 3
    v = entwuerfe.view(Book(book.root), "RE-0001")
    assert not v["ausstellbar"] and "1 Eintrag ohne Preis" in v["fehler"][0]
    assert [str(e["preis"]) for e in v["eintraege_offen"] if e["art"] == "Zeit"] == ["120.00", "80.00"]
    with pytest.raises(BookError, match="ohne Preis"):
        W(book, entwuerfe.issue, "RE-0001")
    # month-end billing leaves the draft's work alone
    with pytest.raises(BookError, match="Nichts offen"):
        W(book, abrechnung.bill_all)
    W(book, daten.update_product, regie, preis="95")
    for e in (a, b):
        W(book, daten.update_entry, e["id"], preis="95")
    W(book, daten.update_entry, c["id"], preis="12.50")
    W(book, entwuerfe.update, "RE-0001", rabatt="5")
    v = entwuerfe.view(Book(book.root), "RE-0001")
    assert v["ausstellbar"] and v["vorschau"]["positionen"][0]["menge"] == Decimal("8")
    expected = v["vorschau"]["total"]
    res = W(book, entwuerfe.issue, "RE-0001", "2026-10-31")
    assert res["total"] == expected and res["entwurf"] == "RE-0001"
    assert entwuerfe.draft(Book(book.root), "RE-0001")["status"] == "ausgestellt"
    assert entwuerfe.overview(Book(book.root)) == []
    assert_clean(Book(book.root))


def test_draft_page(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    a = W(book, daten.add_time, "2026-10-13", "M0001", "4", "Abdecken", "K0001")
    app = create_app(book.root, token="tok")
    with TestClient(app) as cl:
        cl.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = cl.post("/p/leistungen/entwuerfe/neu", data={"kunde": "K0001", "ids": a["id"]}, headers=h)
        assert r.status_code in (200, 204), r.text
        page = cl.get("/p/leistungen/entwuerfe?nr=RE-0001").text
        assert "RE-0001" in page and "Rechnung ausstellen" in page and "Abdecken" in page
        r = cl.post("/p/leistungen/entwuerfe/ausstellen", data={"nummer": "RE-0001", "datum": "2026-10-31"}, headers=h)
        assert r.status_code in (200, 204), r.text
        assert "/debitoren/rechnung/" in r.headers.get("HX-Redirect", "")


def test_regie_hours_of_several_people_describe_the_work_once(book):
    regie = W(book, daten.add_product, "Regiearbeiten", "0", "h")["nummer"]
    W(book, daten.set_options, rechnung_tage="1")
    W(book, daten.add_project, "K0001", "MFH Marktgasse 5", strasse="Marktgasse", hausnr="5", plz="3011", ort="Bern")
    text = "Abgedeckt; Wände gestrichen"
    for d in ("2026-10-13", "2026-10-14"):
        W(book, daten.add_time, d, "M0001", "8", text, "", "P0001", leistung=regie)
        W(book, daten.add_time, d, "M0002", "6", text, "", "P0001", leistung=regie)
    b = Book(book.root)
    pos = abrechnung.positions(b, abrechnung.select(b, "K0001"))
    assert pos[0]["text"].count(text) == 1 and "13.10.–14.10. " + text in pos[0]["text"]
    assert text not in pos[1]["text"] and ("Lea Muster" in pos[0]["text"] + pos[1]["text"])
    assert abrechnung.object_line(b, abrechnung.select(b, "K0001")) == "MFH Marktgasse 5, 3011 Bern"

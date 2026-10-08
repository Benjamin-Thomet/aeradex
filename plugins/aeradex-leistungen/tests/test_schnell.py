"""Fast entry (quick line, timer, week grid, favourites), Leistungsarten, absences and holiday account,
billing in one click."""
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from aeradex import api, invoices
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean
from aeradex_leistungen import abrechnung, abwesenheit, daten, kontrolle, schnell, stoppuhr, woche

from test_leistungen import W, book  # noqa: F401  (fixture: Thun/BE, K0001 Beispiel AG, M0001 Lea, M0002 Tom)

D = Decimal
TODAY = date(2026, 10, 8)        # a Thursday


@pytest.fixture
def setup(book):
    W(book, daten.add_product, "Malerarbeiten", "95", "h")            # P002 Leistungsart
    W(book, daten.add_project, "K0001", "Fassade Muster", "aufwand", "40")
    return Book(book.root)


# ---------- the quick line ----------

@pytest.mark.parametrize("line, expect", [
    ("3.5h Fassade spachteln", {"stunden": D("3.5"), "projekt": "P0001", "kunde": "K0001", "text": "spachteln"}),
    ("2:30 P0001 Malerarbeiten Decke", {"stunden": D("2.50"), "leistung": "P002", "text": "Decke"}),
    ("8-12 gestern K0001 Beratung", {"stunden": D("4.00"), "datum": date(2026, 10, 7), "kunde": "K0001"}),
    ("90min mo Beispiel Telefon", {"stunden": D("1.50"), "datum": date(2026, 10, 5)}),
    ("12 l Dispersionsfarbe Beispiel", {"art": "Produkt", "menge": D("12"), "produkt": "P001", "kunde": "K0001"}),
    ("1.5h intern Buchhaltung", {"abrechenbar": False, "kategorie": "Intern", "text": "Buchhaltung"}),
    ("start P0001 Spachteln", {"start": True, "stunden": None, "projekt": "P0001"}),
    ("2h Fassade", {"text": "Fassade Muster"}),
])
def test_quick_line_is_understood(setup, line, expect):
    p = schnell.parse(setup, line, "M0001", TODAY)
    for k, v in expect.items():
        assert p[k] == v, (line, k, p[k])


def test_quick_line_rate_amount_budget_and_what_is_missing(setup):
    p = schnell.parse(setup, "4h P0001 Malerarbeiten Grundierung", "M0001", TODAY)
    assert p["preis"] == D("95.00") and p["betrag"] == D("380.00") and p["budget"] == 10 and p["ok"]
    p = schnell.parse(setup, "3h Gartenarbeit", "M0001", TODAY)
    assert not p["ok"] and any("Kunde" in f for f in p["fehlt"])
    with pytest.raises(BookError, match="Es fehlt"):
        schnell.save(setup, "3h Gartenarbeit", "M0001", TODAY)


def test_ambiguous_names_are_offered_not_guessed(setup):
    api.customer_add(setup, name="Beispiel Zwei", firma="Beispiel GmbH", ort="Bern")
    p = schnell.parse(Book(setup.root), "2h Beispiel Telefon", "M0001", TODAY)
    assert set(dict(p["kandidaten"]["kunde"])) == {"K0001", "K0002"} and not p["ok"]
    with pytest.raises(BookError, match="Nicht eindeutig"):
        schnell.save(Book(setup.root), "2h Beispiel Telefon", "M0001", TODAY)
    assert schnell.parse(Book(setup.root), "2h K0002 Telefon", "M0001", TODAY)["kunde"] == "K0002"


def test_quick_save_records_with_leistungsart_rate(setup):
    e = W(setup, schnell.save, "2:30 P0001 Malerarbeiten Decke", "M0001", TODAY)
    assert e["produkt"] == "P002" and e["preis"] == D("95.00") and e["menge"] == D("2.50")
    assert_clean(Book(setup.root))


# ---------- rates ----------

def test_rate_order_with_leistungsart(setup):
    from aeradex_leistungen.saetze import hourly_rate
    b = Book(setup.root)
    assert hourly_rate(b, "M0001", "K0001", "", "P002") == D("95.00")          # Leistungsart beats the person
    W(b, daten.update_product, "P002", kunde="K0001", kundenpreis="88")
    assert hourly_rate(Book(b.root), "M0001", "K0001", "", "P002") == D("88.00")  # its customer price first
    W(b, daten.update_project, "P0001", satz="110")
    assert hourly_rate(Book(b.root), "M0001", "K0001", "P0001", "P002") == D("110.00")  # the project's rate wins
    assert hourly_rate(Book(b.root), "M0001", "K0001") == D("120.00")              # no Leistungsart: the person


# ---------- timer ----------

def test_timer_start_switch_stop_with_rounding(setup):
    b = Book(setup.root)
    W(b, daten.set_options, rundung=15)
    t0 = datetime(2026, 10, 8, 8, 0)
    stoppuhr.start(Book(b.root), "M0001", "K0001", "P0001", "P002", "Grundierung", now=t0)
    assert stoppuhr.path(b).exists() and "M0001" in stoppuhr.running(Book(b.root))
    res = stoppuhr.start(Book(b.root), "M0001", "K0001", "P0001", "P002", "Deckanstrich",
                         now=t0 + timedelta(minutes=52))
    assert res["gestoppt"]["stunden"] == D("1.00")                                           # 52 min → 60 min
    stoppuhr.start(Book(b.root), "M0002", "K0001", "", "", "Abdecken", now=t0)               # two people at once
    out = stoppuhr.stop(Book(b.root), "M0001", now=t0 + timedelta(minutes=52 + 7))
    assert out["stunden"] == D("0.25")
    texts = sorted(e["text"] for e in daten.entries(Book(b.root)))
    assert texts == ["Deckanstrich", "Grundierung"] and "M0002" in stoppuhr.running(Book(b.root))
    stoppuhr.discard(Book(b.root), "M0002")
    assert stoppuhr.running(Book(b.root)) == {}
    with pytest.raises(BookError, match="Keine laufende"):
        stoppuhr.stop(Book(b.root), "M0001")
    assert_clean(Book(b.root))


def test_timer_under_a_minute_records_nothing(setup):
    t0 = datetime(2026, 10, 8, 8, 0)
    stoppuhr.start(setup, "M0001", "K0001", now=t0)
    assert stoppuhr.stop(Book(setup.root), "M0001", now=t0 + timedelta(seconds=40))["verworfen"]
    assert daten.entries(Book(setup.root)) == []


# ---------- week grid ----------

def test_week_grid_cells_create_change_delete_and_lock(setup):
    b = Book(setup.root)
    mon = date(2026, 10, 5)
    row = dict(kunde="K0001", projekt="P0001", leistung="P002", abrechenbar=True)
    W(b, woche.set_cell, "M0001", mon, "4", **row)
    g = woche.grid(Book(b.root), "M0001", mon)
    (r,) = g["zeilen"]
    assert r["zellen"][0]["stunden"] == D("4") and r["titel"] == "Fassade Muster" and "Malerarbeiten" in r["unter"]
    W(b, woche.set_cell, "M0001", mon, "6.5", **row)
    assert woche.grid(Book(b.root), "M0001", mon)["zeilen"][0]["zellen"][0]["stunden"] == D("6.5")
    W(b, woche.set_cell, "M0001", mon, "", **row)
    assert woche.grid(Book(b.root), "M0001", mon)["zeilen"] == []
    W(b, woche.set_cell, "M0001", mon, "2", **row)
    W(b, daten.add_time, mon, "M0001", "1", "zweiter", "K0001", "P0001", True, "", None, "P002")
    assert woche.grid(Book(b.root), "M0001", mon)["zeilen"][0]["zellen"][0]["gesperrt"]       # several entries
    with pytest.raises(BookError, match="Mehrere"):
        woche.set_cell(Book(b.root), "M0001", mon, "5", **row)


def test_week_totals_target_and_holidays(setup):
    b = Book(setup.root)
    g = woche.grid(b, "M0001", date(2026, 12, 21))                 # Christmas week, canton BE
    assert g["soll"][4] == 0 and g["soll"][5] == 0                 # 25.12. Friday holiday, Saturday
    assert g["soll"][0] == D("8.40") and g["total_soll"] == D("33.60")
    W(b, daten.add_time, date(2026, 12, 21), "M0001", "9", "Arbeit", "K0001")
    assert woche.grid(Book(b.root), "M0001", date(2026, 12, 21))["saldo"] == D("9") - D("33.60")


def test_recent_and_favourites(setup):
    b = Book(setup.root)
    W(b, schnell.save, "2h P0001 Malerarbeiten Decke", "M0001", TODAY)
    W(Book(b.root), schnell.save, "1h intern Buchhaltung", "M0001", TODAY)
    rec = woche.recent(Book(b.root), "M0001")
    assert sorted(r["titel"] for r in rec) == ["Fassade Muster", "Intern"]
    fav = rec[0]["id"]
    W(b, woche.toggle_favourite, "M0001", fav)
    assert [f["id"] for f in woche.favourites(Book(b.root), "M0001")] == [fav]
    W(b, woche.toggle_favourite, "M0001", fav)
    assert woche.favourites(Book(b.root), "M0001") == []


def test_move_entries_and_rerate(setup):
    b = Book(setup.root)
    e = W(b, daten.add_time, TODAY, "M0001", "2", "Beratung", "K0001")
    assert e["preis"] == D("120.00")
    moved = W(b, daten.update_entry, e["id"], projekt="P0001", kunde="", leistung="P002", neu_bewerten=True)
    assert moved["projekt"] == "P0001" and moved["produkt"] == "P002" and moved["preis"] == D("95.00")


# ---------- absences ----------

def test_absences_count_as_actual_time_holidays_skipped(setup):
    b = Book(setup.root)
    W(b, abwesenheit.add, "M0001", "Ferien", "2026-12-21", "2026-12-31")
    acc = abwesenheit.holiday_account(Book(b.root), "M0001", 2026, today=date(2026, 12, 1))
    assert acc["geplant"] == D("8") and acc["bezogen"] == 0          # 25.12. (holiday BE) and weekends do not count
    row = next(r for r in kontrolle.month(Book(b.root), 2026, 12) if r["nummer"] == "M0001")
    assert row["kategorien"]["Ferien"] == D("8.40") * 8
    W(b, abwesenheit.add, "M0001", "Krank", "2026-12-17", None, "0.5")
    assert abwesenheit.month_totals(Book(b.root), daten.person(Book(b.root), "M0001"), 2026, 12)["arten"]["Krank"]["tage"] == D("0.5")
    with pytest.raises(BookError, match="Überschneidet"):
        abwesenheit.add(Book(b.root), "M0001", "Krank", "2026-12-22")


def test_compensation_and_unpaid_leave(setup):
    b = Book(setup.root)
    W(b, abwesenheit.add, "M0001", "Kompensation", "2026-11-02")
    W(Book(b.root), abwesenheit.add, "M0001", "Unbezahlt", "2026-11-03")
    row = next(r for r in kontrolle.month(Book(b.root), 2026, 11) if r["nummer"] == "M0001")
    full = kontrolle.target(daten.person(Book(b.root), "M0001"), 2026, 11, abwesenheit.holidays(Book(b.root), 2026))
    assert row["soll"] == full - D("8.40") and row["ist"] == 0     # unpaid lowers the target, compensation adds nothing


def test_holiday_entitlement_pro_rata(setup):
    b = Book(setup.root)
    api.employee_add(b, "Neu", "Eintritt", monatslohn=5000, eintritt="2026-07-01")
    acc = abwesenheit.holiday_account(Book(b.root), "M0003", 2026)
    assert acc["anspruch"] == D("10")                               # 184/365 × 20 → 10.08 → 10
    W(b, daten.set_person, "M0003", ferien_tage=25, ferien_jahr=2026, ferien_vortrag=3)
    acc = abwesenheit.holiday_account(Book(b.root), "M0003", 2026)
    assert acc["anspruch"] == D("12.5") and acc["vortrag"] == D("3")


def test_time_record_pdf(setup):
    b = Book(setup.root)
    W(b, daten.add_time, date(2026, 10, 5), "M0001", "8", "Arbeit", "K0001")
    W(Book(b.root), abwesenheit.add, "M0001", "Krank", "2026-10-06")
    r = abwesenheit.time_record(Book(b.root), "M0001", 2026, 10)
    assert r["tage"][4]["gearbeitet"] == D("8") and r["tage"][5]["abwesenheit"] == "Krank"
    assert abwesenheit.time_record_pdf(Book(b.root), "M0001", 2026, 10).startswith(b"%PDF")


# ---------- billing in one click ----------

def test_cards_groups_and_bill_all(setup):
    b = Book(setup.root)
    api.customer_add(b, name="Peter", firma="Peter AG", strasse="Weg", nr="1", plz="3011", ort="Bern")
    W(b, daten.add_time, date(2026, 10, 1), "M0001", "4", "Grundierung", "K0001", "P0001", True, "", None, "P002")
    W(b, daten.add_time, date(2026, 10, 2), "M0001", "2", "Beratung", "K0001")
    W(b, daten.add_time, date(2026, 10, 2), "M0001", "1", "Telefon", "K0002")
    cards = abrechnung.summary(Book(b.root))
    assert {(c["kunde"], c["projekt"]) for c in cards} == {("K0001", "P0001"), ("K0001", ""), ("K0002", "")}
    assert sorted(g["kunde"] for g in abrechnung.groups(Book(b.root), je="kunde")) == ["K0001", "K0002"]
    assert len(abrechnung.groups(Book(b.root), je="projekt")) == 3
    res = W(b, abrechnung.bill_all, None, "kunde", "2026-10-31")
    assert res["anzahl"] == 2 and res["total"] == D("380") + D("240") + D("120")
    pos = invoices.invoice(Book(b.root), res["rechnungen"][0]["rechnung"])["positionen"]
    assert any(p["text"].startswith("Malerarbeiten, Fassade Muster") for p in pos)        # grouped per Leistungsart
    assert abrechnung.summary(Book(b.root)) == [] and abrechnung.mismatches(Book(b.root)) == []
    assert_clean(Book(b.root))


# ---------- web ----------

def test_web_tabs_fragments_and_actions(setup):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(setup.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        for tab in ("woche", "abrechnen", "abwesenheiten", "auswertung", "stammdaten", "erfassen", "kontrolle"):
            assert c.get(f"/p/leistungen/leistungen?tab={tab}&wer=M0001").status_code == 200, tab
        r = c.get("/p/leistungen/leistungen/vorschau", params={"text": "3.5h Fassade spachteln", "wer": "M0001"})
        assert "Fassade Muster" in r.text and "↵ erfassen" in r.text
        r = c.post("/p/leistungen/leistungen/schnell", headers=h, data={"text": "3.5h Fassade spachteln", "wer": "M0001"})
        assert r.status_code == 200 and 'id="woche"' in r.text and "aeradexToast" in r.headers["HX-Trigger"]
        r = c.post("/p/leistungen/leistungen/schnell", headers=h, data={"text": "3h ohne Kunde", "wer": "M0001"})
        assert 'class="alert error"' in r.text                                      # the page shows it as a toast
        r = c.post("/p/leistungen/leistungen/start", headers=h, data={"text": "P0001 Decke", "wer": "M0001"})
        assert "data-timer-start" in r.text
        r = c.post("/p/leistungen/leistungen/abwesenheit", headers=h,
                   data={"wer": "M0001", "art": "Ferien", "von": "2026-12-21", "bis": "2026-12-23", "jahr": "2026"})
        assert 'id="kalender"' in r.text and r.text.count(" ferien") >= 3
        r = c.get("/p/leistungen/leistungen/rechnung_vorschau", params={"kunde": "K0001", "projekt": "P0001"})
        assert "Rechnung ausstellen" in r.text
        r = c.post("/p/leistungen/leistungen/nachweis", headers=h, data={"wer": "M0001", "monat": "2026-12"})
        assert r.headers["HX-Redirect"].startswith("/datei/berichte/Arbeitszeitnachweis")


# ---------- speed ----------

def test_ten_thousand_entries_stay_fast(setup, tmp_path):
    b = Book(setup.root)
    rows = []
    for i in range(10000):
        d = date(2026, 1 + i % 12, 1 + i % 28)
        rows.append({"id": f"L-2026-{i + 1:05d}", "datum": d, "art": "Zeit", "wer": "M0001", "kunde": "K0001",
                     "projekt": "P0001", "produkt": "P002", "menge": D("1.5"), "preis": D("95.00"), "text": "Arbeit",
                     "abrechenbar": True, "kategorie": "", "rechnung": ""})
    by_month = {}
    for e in rows:
        by_month.setdefault(daten.month_path(b, e["datum"]), []).append(e)
    for path, items in by_month.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        daten._write_month(path, items)
    daten.entries(b)                                   # parse once
    t = time.perf_counter()
    assert len(daten.entries(b)) == 10000
    woche.grid(b, "M0001", date(2026, 10, 5))
    assert time.perf_counter() - t < 1.5

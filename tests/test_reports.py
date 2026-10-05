from datetime import date
from decimal import Decimal

import pytest

from allkvitt import api, budget, reports, statements
from allkvitt.book import Book, BookError
from allkvitt.testing import make_book

D = Decimal


def post(book, *rows):
    for datum, soll, haben, betrag, text in rows:
        api.post_entry(Book(book.root), datum, soll, haben, betrag, text)


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000})
    for m in range(1, 13):
        post(b, (f"2026-{m:02d}-05", "1100", "3400", 3000 + 100 * m, "Rechnung"),
             (f"2026-{m:02d}-20", "1020", "1100", 3000 + 100 * m, "Zahlung"),
             (f"2026-{m:02d}-25", "6000", "1020", "1200", "Miete"),
             (f"2026-{m:02d}-25", "5000", "1020", "1500", "Lohn"))
    post(b, ("2026-03-10", "1520", "1020", "5000", "Laptop"),
         ("2026-04-01", "1020", "2450", "10000", "Darlehen"),
         ("2026-11-15", "6500", "2000", "300", "Papier offen"),
         ("2026-12-15", "1100", "3400", "800", "Rechnung offen"),
         ("2026-12-31", "6800", "1520", "1000", "Abschreibung"))
    return Book(b.root)


def by_label(rep, key="c0"):
    return {r["label"]: r["werte"].get(key) for r in rep["zeilen"] if r["stil"] != "kopf"}


def test_year_equals_jahresrechnung(book):
    st = statements.year_end_statement(book, 2026)
    er = reports.run(book, "erfolgsrechnung", jahr=2026)
    want = {r["label"]: r["aktuell"] for r in st["erfolg"] if r["aktuell"]}
    got = {k: v for k, v in by_label(er).items() if v}
    assert got == want
    bi = by_label(reports.run(book, "bilanz", jahr=2026))
    assert bi["Total Aktiven"] == st["total_aktiven"] == bi["Total Passiven"]
    assert bi["Ergebnis laufendes Jahr"] == st["jahresergebnis"]


def test_month_columns_add_up_and_balance_every_month(book):
    er = reports.run(book, "erfolgsrechnung", jahr=2026, spalten="monat")
    keys = [c["key"] for c in er["spalten"] if c["key"].startswith("c")]
    assert len(keys) == 12 and er["spalten"][12]["key"] == "total"
    for r in er["zeilen"]:
        assert sum((r["werte"][k] for k in keys), D(0)) == r["werte"]["total"], r["label"]
    assert by_label(er, "c0")["Betriebsertrag aus Lieferungen und Leistungen"] == D("3100")
    assert er["diagramm"]["labels"][0] == "Jan 2026"
    bi = reports.run(book, "bilanz", jahr=2026, spalten="monat")
    assert bi["hinweise"] == []
    assert bi["spalten"][0]["label"] == "31.01.2026"


def test_quarter_with_previous_period(book):
    er = reports.run(book, "erfolgsrechnung", jahr=2026, periode="q2", vergleich="vorperiode")
    labels = [c["label"] for c in er["spalten"]]
    assert labels == ["Q2 2026", "Vorperiode Q1 2026", "Abweichung", "Abw. %"]
    ertrag = next(r for r in er["zeilen"] if r["label"].startswith("Betriebsertrag"))["werte"]
    assert ertrag["c0"] == D("3400") + D("3500") + D("3600") and ertrag["ref"] == D("3100") + D("3200") + D("3300")
    assert ertrag["diff"] == D("900") and ertrag["pct"] == D("9.4")


def test_cash_flow_reconciles(book):
    gf = reports.run(book, "geldfluss", jahr=2026)
    v = by_label(gf)
    assert gf["abstimmung"] == 0 and gf["hinweise"] == []
    assert v["Geldfluss aus Investitionstätigkeit"] == D("-5000")
    assert v["Geldfluss aus Finanzierungstätigkeit"] == D("10000")
    assert v["Abschreibungen und Wertberichtigungen"] == D("1000")
    assert v["Flüssige Mittel: Ende"] - v["Flüssige Mittel: Anfang"] == v["Veränderung flüssige Mittel"]
    q = reports.run(book, "geldfluss", jahr=2026, periode="q4")
    assert q["abstimmung"] == 0


def test_across_the_year_end(book):
    post(book, ("2027-01-05", "1100", "3400", "5000", "Rechnung"), ("2027-02-01", "1020", "1100", "5800", "Zahlung"),
         ("2027-02-10", "2450", "1020", "2000", "Rückzahlung"))
    gf = reports.run(book, "geldfluss", jahr=2026, von="2026-07-01", bis="2027-06-30")
    assert gf["abstimmung"] == 0
    er = reports.run(book, "erfolgsrechnung", jahr=2026, von="2026-12-01", bis="2027-01-31")
    assert by_label(er)["Betriebsertrag aus Lieferungen und Leistungen"] == D("4200") + D("800") + D("5000")
    ref = reports.run(book, "erfolgsrechnung", jahr=2027, periode="01", vergleich="vorjahr")
    assert next(r for r in ref["zeilen"] if r["label"].startswith("Betriebsertrag"))["werte"]["ref"] == D("3100")


def test_budget_from_prior_year_and_comparison(book):
    api.write(Book(book.root), "Budget", budget.from_prior, 2027, 10)
    months = budget.load(Book(book.root), 2027)["3400"]
    assert sum(months, D(0)) == D("49100")                     # 44600 × 1.1 → rounded to 100
    assert months[0] < months[10]                              # seasonality kept
    api.write(Book(book.root), "Budget", budget.set_account, 2027, "6000", 15600)
    assert budget.load(Book(book.root), 2027)["6000"] == [D("1300.00")] * 12
    post(book, ("2027-01-25", "6000", "1020", "1400", "Miete"))
    er = reports.run(Book(book.root), "erfolgsrechnung", jahr=2027, periode="01", vergleich="budget")
    row = next(r for r in er["zeilen"] if r["label"] == "Übriger betrieblicher Aufwand")
    assert row["werte"]["c0"] == D("-1400") and row["werte"]["ref"] == D("-1300.00") and row["werte"]["diff"] == D("-100")
    assert budget.window(Book(book.root), date(2027, 1, 1), date(2027, 1, 15))["6000"] == D("629.03")
    with pytest.raises(BookError, match="Erfolgskonto"):
        api.write(Book(book.root), "Budget", budget.set_account, 2027, "1020", 100)


def test_budget_check(book):
    p = budget.path(book, 2027)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('konten:\n  "9999": {jahr: 10}\n  "6000": {jahr: 100, monate: [1,1,1,1,1,1,1,1,1,1,1,1]}\n')
    found = budget.check(Book(book.root))
    assert any("9999" in m for _, _, m in found) and any("Summe" in m for _, _, m in found)


def test_kennzahlen(book):
    k = {r["label"]: r for r in reports.run(book, "kennzahlen", jahr=2026, stichtag="2026-12-31")["zeilen"]}
    st = statements.year_end_statement(book, 2026)
    ek = D("20000") + st["jahresergebnis"]
    assert k["Eigenkapitalquote"]["werte"]["c0"] == (ek / st["total_aktiven"] * 100).quantize(D("0.1"))
    assert k["Betriebsertrag"]["werte"]["c0"] == D("44600")
    assert k["Liquiditätsgrad 2"]["ampel"] in ("ok", "warn", "red")


def test_aging_and_revenue(book):
    rep = reports.run(book, "kreditoren", stichtag="2026-12-31")
    assert rep["zeilen"][-1]["werte"]["total"] == 0                # booked without a Kreditor document
    um = reports.run(book, "umsatz", jahr=2026, nach="monat")
    assert um["zeilen"][0]["werte"]["c0"] == D("3100") and um["zeilen"][-1]["werte"]["c0"] == D("44600")
    with pytest.raises(BookError, match="unbekannt"):
        reports.run(book, "gibtsnicht")

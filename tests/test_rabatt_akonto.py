"""Invoice discount in percent (on Erlösminderungen) and Akontorechnungen credited on the Schlussrechnung."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, check, mwst
from aeradex.book import Book, BookError
from aeradex.ledger import BalanceEngine

D = Decimal


def _book(tmp_path: Path, methode: str = "keine", **mw) -> Path:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7", uid="CHE-123.456.789 MWST")
    if methode != "keine":
        api.settings_update(Book(root), mwst={"methode": methode, **mw})
    api.customer_add(Book(root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    api.customer_add(Book(root), name="Beat", strasse="Weg", nr="2", plz="3012", ort="Bern")
    return root


def errors(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level != "hinweis"]


def hints(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level == "hinweis"]


def bal(root: Path, nr: str) -> Decimal:
    return BalanceEngine(Book(root)).balance(nr, 2026)


def rows(root: Path, nummer: str):
    return sorted((r.soll, r.haben, r.betrag, r.mwst) for r in Book(root).rows if r.quelle == f"rechnung:{nummer}")


ARBEIT = [{"text": "Malerarbeiten", "menge": 10, "preis": "100"}, {"text": "Material", "menge": 1, "preis": "500", "konto": "3200"}]


def test_discount_without_vat_goes_to_erloesminderungen(tmp_path):
    root = _book(tmp_path)
    inv = api.invoice_create(Book(root), "K0001", ARBEIT, "2026-03-01", rabatt="10")["rechnung"]
    assert inv["total"] == "1350.00" and inv["rabatt"] == "10"
    assert [p["text"] for p in inv["positionen"] if p.get("rabatt")] == ["Rabatt 10 %", "Rabatt 10 %"]
    assert rows(root, inv["nummer"]) == sorted([("1100", "", D("1350.00"), ""), ("", "3400", D("1000.00"), ""),
                                                ("", "3200", D("500.00"), ""), ("3800", "", D("150.00"), "")])
    assert bal(root, "3800") == D("150.00") and errors(root) == []


def test_discount_with_vat_two_rates(tmp_path):
    root = _book(tmp_path, "effektiv")
    pos = [{"text": "Arbeit", "menge": 1, "preis": "1000"}, {"text": "Buch", "menge": 1, "preis": "200", "mwst": "U26"}]
    inv = api.invoice_create(Book(root), "K0001", pos, "2026-02-01", rabatt="12.5")["rechnung"]
    by = {b["code"]: b for b in inv["mwst"]}
    assert (by["U81"]["netto"], by["U81"]["steuer"]) == ("875.00", "70.88")
    assert (by["U26"]["netto"], by["U26"]["steuer"]) == ("175.00", "4.55")
    assert inv["total"] == "1125.43"
    assert ("3800", "", D("125.00"), "U81") in rows(root, inv["nummer"])
    z = mwst.report(Book(root), "2026-Q1")["ziffern"]
    assert z["303"] == D("875.00") and z["303_steuer"] == D("70.88")
    assert errors(root) == []


def test_discount_with_saldo_method(tmp_path):
    root = _book(tmp_path, "saldo", saldosteuersatz="6.2", periode="semester")
    inv = api.invoice_create(Book(root), "K0001", ARBEIT[:1], "2026-02-01", rabatt="5")["rechnung"]
    assert inv["total"] == str(D("950") + D("76.95"))
    assert errors(root) == []


def test_akonto_without_vat_is_cleared_by_the_schlussrechnung(tmp_path):
    root = _book(tmp_path)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "1. Akonto Fassade", "preis": "1000"}], "2026-03-01",
                            art="akonto")["rechnung"]
    assert ak["art"] == "akonto" and bal(root, "2030") == D("-1000.00")
    assert any(ak["nummer"] in h for h in hints(root))
    assert [a["nummer"] for a in api.invoice_open_akonto(Book(root), "K0001")] == [ak["nummer"]]
    api.invoice_pay(Book(root), ak["nummer"], "1000", "2026-03-10")
    assert api.invoice_check(Book(root), "K0001", ARBEIT, "2026-05-01", akonto="alle")["total"] == "500.00"
    res = api.invoice_create(Book(root), "K0001", ARBEIT, "2026-05-01", akonto="alle")
    fin = res["rechnung"]
    assert fin["art"] == "schluss" and fin["total"] == "500.00"
    assert res["angerechnet"] == [ak["nummer"]] and "warnung" not in res
    assert ("2030", "", D("1000.00"), "") in rows(root, fin["nummer"])
    assert bal(root, "2030") == 0 and bal(root, "3400") == D("-1000.00")
    assert api.invoice_open_akonto(Book(root), "K0001") == [] and not any("Akontorechnung" in h for h in hints(root))
    assert errors(root) == []
    with pytest.raises(BookError, match="angerechnet"):
        api.invoice_void(Book(root), ak["nummer"])
    with pytest.raises(BookError, match="bereits"):
        api.invoice_check(Book(root), "K0001", ARBEIT, "2026-05-02", akonto=[ak["nummer"]])
    api.invoice_void(Book(root), fin["nummer"], "falsch")
    assert [a["nummer"] for a in api.invoice_open_akonto(Book(root))] == [ak["nummer"]]
    assert errors(root) == []


def test_an_unpaid_akonto_is_credited_too_and_both_add_up(tmp_path):
    root = _book(tmp_path)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "Akonto", "preis": "1000"}], "2026-03-01", art="akonto")["rechnung"]
    fin = api.invoice_create(Book(root), "K0001", ARBEIT, "2026-04-01", akonto=[ak["nummer"]])["rechnung"]
    states = {s["nummer"]: s for s in api.invoice_list(Book(root))}
    assert D(states[ak["nummer"]]["offen"]) + D(states[fin["nummer"]]["offen"]) == D("1500.00")
    assert bal(root, "1100") == D("1500.00")


def test_akonto_of_another_customer_or_too_high_is_refused(tmp_path):
    root = _book(tmp_path)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "Akonto", "preis": "5000"}], "2026-03-01", art="akonto")["rechnung"]
    with pytest.raises(BookError, match="anderen|gehört"):
        api.invoice_create(Book(root), "K0002", ARBEIT, "2026-04-01", akonto=[ak["nummer"]])
    with pytest.raises(BookError, match="höher"):
        api.invoice_create(Book(root), "K0001", ARBEIT[:1], "2026-04-01", akonto=[ak["nummer"]])
    with pytest.raises(BookError, match="Akontorechnung hat keinen Rabatt"):
        api.invoice_create(Book(root), "K0001", ARBEIT, "2026-04-01", art="akonto", rabatt="5")


def test_an_ordinary_invoice_warns_about_open_akonto(tmp_path):
    root = _book(tmp_path)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "Akonto", "preis": "500"}], "2026-03-01", art="akonto")["rechnung"]
    res = api.invoice_create(Book(root), "K0001", ARBEIT[:1], "2026-04-01")
    assert ak["nummer"] in res["warnung"]
    assert "warnung" not in api.invoice_create(Book(root), "K0002", ARBEIT[:1], "2026-04-01")


@pytest.mark.parametrize("art", ["vereinbart", "vereinnahmt"])
def test_akonto_with_vat_is_taxed_once(tmp_path, art):
    root = _book(tmp_path, "effektiv", abrechnungsart=art)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "Akonto", "preis": "2000"}], "2026-02-01", art="akonto")["rechnung"]
    assert ak["total"] == "2162.00" and ("", "2030", D("2000.00"), "U81") in rows(root, ak["nummer"])
    api.invoice_pay(Book(root), ak["nummer"], "2162", "2026-02-10")
    fin = api.invoice_create(Book(root), "K0001", [{"text": "Arbeit", "menge": 1, "preis": "5000"}], "2026-03-01",
                             akonto="alle", rabatt="10")["rechnung"]
    # 5000 − 10 % = 4500, − Akonto 2000 = 2500 net, tax 202.50
    assert fin["netto"] == "2500.00" and fin["total"] == "2702.50"
    api.invoice_pay(Book(root), fin["nummer"], "2702.50", "2026-03-20")
    z = mwst.report(Book(root), "2026-Q1")["ziffern"]
    assert z["303"] == D("4500.00") and z["303_steuer"] == D("364.50")
    assert bal(root, "2030") == 0 and bal(root, "2200") == D("-364.50")
    assert errors(root) == []
    abst = mwst.abstimmung(Book(root), 2026)
    assert abst["ertrag"]["anzahlungen"] == 0 and abst["ertrag"]["umsatz_andere_konten"] == 0


def test_tampering_with_an_akonto_line_is_an_error(tmp_path):
    from aeradex.files import read_frontmatter, write_frontmatter
    root = _book(tmp_path)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "Akonto", "preis": "500"}], "2026-03-01", art="akonto")["rechnung"]
    fin = api.invoice_create(Book(root), "K0001", ARBEIT, "2026-04-01", akonto="alle")["rechnung"]
    path = root / "rechnungen" / "2026" / f"{fin['nummer']}.md"
    meta, body = read_frontmatter(path)
    meta["positionen"][-1]["akonto"] = "R-2026-0099"
    write_frontmatter(path, meta, body)
    assert any("keine Akontorechnung" in e for e in errors(root)) and ak


def test_pdf_titles(tmp_path):
    from aeradex import pdf
    from aeradex.invoices import customer, invoice
    root = _book(tmp_path)
    ak = api.invoice_create(Book(root), "K0001", [{"text": "Akonto", "preis": "500"}], "2026-03-01", art="akonto")["rechnung"]
    fin = api.invoice_create(Book(root), "K0001", ARBEIT, "2026-04-01", akonto="alle", rabatt="10")["rechnung"]
    from pypdf import PdfReader
    import io
    text = lambda nr: "".join(p.extract_text() for p in PdfReader(io.BytesIO(
        pdf.invoice_pdf(Book(root), invoice(Book(root), nr), customer(Book(root), "K0001")))).pages)
    assert "Akontorechnung" in text(ak["nummer"])
    t = text(fin["nummer"])
    assert "Schlussrechnung" in t and "Rabatt 10 %" in t and f"abzüglich Akontorechnung {ak['nummer']}" in t

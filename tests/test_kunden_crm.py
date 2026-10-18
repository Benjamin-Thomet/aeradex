"""Customers as a CRM: dossier (invoices, revenue, payment behaviour, timeline), contact log and follow-ups."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, check, kunden, reports, tools
from aeradex.book import Book, BookError

D = Decimal


def _book(tmp_path: Path) -> Path:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    api.customer_add(Book(root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern",
                     kategorie="Verwaltung", herkunft="Empfehlung Hans Muster")
    api.customer_add(Book(root), name="Beat", strasse="Weg", nr="2", plz="3012", ort="Bern")
    return root


def inv(root, kunde, preis, datum, **kw):
    return api.invoice_create(Book(root), kunde, [{"text": "Arbeit", "menge": 1, "preis": preis}], datum,
                              zahlungsfrist=30, **kw)["rechnung"]


def test_dossier_revenue_matches_the_report_and_counts_akonto_once(tmp_path):
    root = _book(tmp_path)
    ak = inv(root, "K0001", "1000", "2026-02-01", art="akonto")
    fin = inv(root, "K0001", "3000", "2026-03-01", akonto="alle", rabatt="10")
    gone = inv(root, "K0001", "500", "2026-03-05")
    api.invoice_void(Book(root), gone["nummer"], "falsch")
    other = inv(root, "K0002", "800", "2026-03-10")
    api.invoice_credit(Book(root), other["nummer"], "100", "2026-03-20", grund="Skonto")
    d = kunden.dossier(Book(root), "K0001", heute=date(2026, 6, 1))
    k = d["kennzahlen"]
    assert k["umsatz_jahr"] == D("2700.00") and k["umsatz_total"] == D("2700.00") and k["umsatz_vorjahr"] == 0
    rep = reports.umsatz(Book(root), date(2026, 1, 1), date(2026, 12, 31))
    by_label = {r["label"]: r["werte"]["c0"] for r in rep["zeilen"]}
    assert by_label["Kunde AG"] == k["umsatz_jahr"]
    assert kunden.dossier(Book(root), "K0002")["kennzahlen"]["umsatz_jahr"] == D("700.00")
    arten = {r["nummer"]: (r["art_label"], r["angerechnet_in"], r["status"]) for r in d["rechnungen"]}
    assert arten[ak["nummer"]] == ("Akontorechnung", fin["nummer"], "offen")
    assert arten[fin["nummer"]][0] == "Schlussrechnung" and arten[gone["nummer"]][2] == "storniert"
    assert k["anzahl"] == 2 and k["offen"] == D(ak["total"]) + D(fin["total"])
    assert k["ueberfaellig"] == k["offen"]                                   # due in March, today is June
    assert [e["titel"] for e in d["zeitachse"]][0] == f"Rechnung {gone['nummer']}"


def test_payment_behaviour(tmp_path):
    root = _book(tmp_path)
    a = inv(root, "K0001", "100", "2026-01-01")                # due 31.01.
    assert kunden.dossier(Book(root), "K0001")["zahlungsmoral"]["label"] == "zu wenig Daten"
    api.invoice_pay(Book(root), a["nummer"], "100", "2026-01-30")
    b = inv(root, "K0001", "200", "2026-02-01")                # due 03.03.
    api.invoice_pay(Book(root), b["nummer"], "200", "2026-03-05")
    zm = kunden.dossier(Book(root), "K0001")["zahlungsmoral"]
    assert (zm["bezahlte"], zm["tage_schnitt"], zm["puenktlich_prozent"], zm["ampel"]) == (2, D("0.5"), D("50"), "ok")
    c = inv(root, "K0001", "300", "2026-03-01")                # due 31.03., paid 80 days late
    api.invoice_pay(Book(root), c["nummer"], "300", "2026-06-19")
    zm = kunden.dossier(Book(root), "K0001")["zahlungsmoral"]
    assert zm["tage_schnitt"] == D("27.0") and zm["ampel"] == "warn"


def test_log_follow_up_and_check(tmp_path):
    root = _book(tmp_path)
    res = api.customer_log_add(Book(root), "K0001", "Offerte Fassade besprochen", "anruf", "2026-05-01",
                               nachfassen="2026-05-10", wer="Delia")
    assert res["ok"] and "Anruf notiert" in res["meldung"]
    api.customer_log_add(Book(root), "K0001", "Nur Info", "notiz", "2026-05-02")
    assert [e["kunde"] for e in kunden.wiedervorlagen(Book(root), date(2026, 5, 10))] == ["K0001"]
    assert kunden.wiedervorlagen(Book(root), date(2026, 5, 9)) == []
    d = kunden.dossier(Book(root), "K0001")
    assert [e["titel"] for e in d["zeitachse"]] == ["Notiz", "Anruf"] and d["zeitachse"][1]["wer"] == "Delia"
    api.customer_log_done(Book(root), "K0001", 0)
    assert kunden.wiedervorlagen(Book(root)) == []
    with pytest.raises(BookError, match="Art"):
        api.customer_log_add(Book(root), "K0001", "x", "fax")
    with pytest.raises(BookError, match="Text"):
        api.customer_log_add(Book(root), "K0001", "  ")
    path = next((root / "kunden").glob("K0001-*.md"))
    path.write_text(path.read_text().replace("nachfassen: '2026-05-10'", "nachfassen: bald"))
    assert any("nachfassen ist kein Datum" in str(i) for i in check.run(Book(root)))


def test_search_and_the_agent_tools(tmp_path, monkeypatch):
    from aeradex.web.views import customer_search_text
    root = _book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(root))
    res = tools.log_customer("K0002", "Will im Herbst streichen", "besuch", nachfassen="2026-09-01")
    assert res["ok"], res
    c = Book(root)
    from aeradex import invoices
    text = customer_search_text("K0001", invoices.customer(c, "K0001"))
    assert "Verwaltung" in text and "Hans Muster" in text
    assert "Herbst" in customer_search_text("K0002", invoices.customer(c, "K0002"))
    details = tools.customer_details("K0002")
    assert details["log"][0]["art"] == "besuch" and details["wiedervorlagen"] and "kennzahlen" in details
    assert tools.follow_ups("2026-09-01")[0]["kunde"] == "K0002"
    upd = tools.update_customer("K0002", kategorie="Privat", herkunft="Website")
    assert upd["ok"] and invoices.customer(Book(root), "K0002")["kategorie"] == "Privat"


def test_detail_page_list_and_log_post(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    root = _book(tmp_path)
    inv(root, "K0001", "1000", "2026-02-01")
    app = create_app(root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/debitoren/kunden").text
        assert "Gasse 1" in page and "3011 Bern" in page and 'data-href="/debitoren/kunden/K0001"' in page
        assert "Verwaltung" in page
        only = c.get("/debitoren/kunden?kategorie=Verwaltung").text
        assert "Kunde AG" in only and "Beat" not in only.split('id="kunden"')[1].split("</table>")[0]
        detail = c.get("/debitoren/kunden/K0001").text
        assert "Kunde AG" in detail and "R-2026-0001" in detail and "Umsatz 2026" in detail
        assert "via Empfehlung Hans Muster" in detail and "/debitoren/neu?kunde=K0001&art=akonto" in detail
        assert c.get("/debitoren/kunden/K0099").status_code == 404
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/debitoren/kunden/K0001/log", headers=h,
                   data={"art": "anruf", "text": "Termin vereinbart", "datum": "2026-05-01", "nachfassen": "2026-05-03"})
        assert r.status_code == 204, r.text
        assert "Termin vereinbart" in c.get("/debitoren/kunden/K0001").text
        assert "Nachfassen: Kunde AG" in c.get("/").text
        r = c.post("/debitoren/kunden/K0001/log/0", headers=h, data={})
        assert r.status_code == 204
        r = c.post("/debitoren/kunden/K0001/infos", headers=h, data={"notizen": "Schlüssel beim Hauswart"})
        assert r.status_code == 204
        assert "Schlüssel beim Hauswart" in c.get("/debitoren/kunden/K0001").text
        r = c.post("/debitoren/kunden/neu", headers=h, data={"name": "Cora", "ort": "Thun", "kategorie": "Privat"})
        assert r.status_code == 204 and r.headers["HX-Redirect"] == "/debitoren/kunden/K0003"
        assert c.get("/debitoren/kunden?edit=K0001", follow_redirects=False).status_code == 303
    assert kunden.wiedervorlagen(Book(root)) == []

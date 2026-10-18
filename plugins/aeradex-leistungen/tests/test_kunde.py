"""Objects (projects) with address and access, shown on the customer's page; the invoice names the object."""
import io

import pytest

from aeradex import api, invoices, kunden
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book
from aeradex_leistungen import abrechnung, daten, offerten

FIRMA = dict(strasse="Hauptstrasse", nr="1", plz="3600", ort="Thun", iban="CH93 0076 2011 6238 5295 7")


def W(book, fn, *args, **kw):
    return api.write(Book(book.root), "test", fn, *args, **kw).get("ergebnis")


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["leistungen"], eroeffnung={"1020": 10000, "2800": -10000}, **FIRMA)
    api.customer_add(b, name="Anna Beispiel", firma="Verwaltung AG", strasse="Marktgasse", nr="5", plz="3011", ort="Bern")
    api.employee_add(Book(b.root), "Lea", "Muster", monatslohn=6000, pensum=100, vollzeit_stunden_woche=42,
                     eintritt="2026-01-01")
    W(b, daten.set_person, "M0001", 120, 70)
    return Book(b.root)


def test_objects_with_address_on_the_customer_page_and_the_invoice(book):
    W(book, daten.add_project, "K0001", "", strasse="Bernstrasse", hausnr="12", plz="3600", ort="Thun",
      zugang="Schlüssel beim Hauswart")
    W(book, daten.add_project, "K0001", "MFH Seeweg", strasse="Seeweg", hausnr="4", plz="3700", ort="Spiez")
    W(book, daten.add_project, "K0001", "Altes Projekt")                       # without an address: as before
    p1 = daten.project(Book(book.root), "P0001")
    assert p1["name"] == "Bernstrasse 12, 3600 Thun" and p1["zugang"] == "Schlüssel beim Hauswart"
    assert "adresse" not in daten.project(Book(book.root), "P0003")
    W(book, daten.update_project, "P0002", None, zugang="Code 4711", notizen="Fassade Nordseite")
    assert daten.project(Book(book.root), "P0002")["zugang"] == "Code 4711"
    W(book, daten.add_time, "2026-10-02", "M0001", "3", "Fassade", "", "P0002")
    W(book, offerten.create, "K0001", [{"text": "Anstrich", "menge": 1, "preis": "5000"}], "2026-09-01", "Treppenhaus")

    d = kunden.dossier(Book(book.root), "K0001")
    [(plugin, teil)] = d["teile"]
    assert plugin == "leistungen"
    assert [o["nummer"] for o in teil.context["objekte"]] == ["P0001", "P0002", "P0003"]
    assert teil.context["objekte"][1]["adresse"] == "Seeweg 4, 3700 Spiez"
    assert teil.kennzahlen[0]["label"] == "Offene Leistungen" and teil.kennzahlen[0]["wert"] == 360
    assert any(e["art"] == "offerte" for e in d["zeitachse"])

    ids = [e["id"] for e in abrechnung.select(Book(book.root), kunde="K0001")]
    res = W(book, abrechnung.bill, ids)
    meta = invoices.invoice(Book(book.root), res["rechnung"])
    assert meta["objekt"] == "MFH Seeweg, Seeweg 4, 3700 Spiez"
    assert invoices.invoice_fingerprint_ok(meta)
    from pypdf import PdfReader
    pdf = (book.root / res["pdf"]).read_bytes()
    assert "Seeweg 4, 3700 Spiez" in "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert_clean(Book(book.root))


def test_the_customer_page_shows_the_plugin_panels(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    W(book, daten.add_project, "K0001", "", strasse="Bernstrasse", hausnr="12", plz="3600", ort="Thun")
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/debitoren/kunden/K0001").text
        assert "Bernstrasse 12, 3600 Thun" in page and "Neue Offerte" in page and 'id="objekte"' in page
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true", "HX-Current-URL": "http://x/debitoren/kunden/K0001"}
        r = c.post("/p/leistungen/leistungen/projekt_neu", headers=h,
                   data={"kunde": "K0001", "strasse": "Seeweg", "hausnr": "4", "plz": "3700", "ort": "Spiez"})
        assert r.status_code == 204, r.text
        assert r.headers["HX-Redirect"].endswith("/debitoren/kunden/K0001")
        r = c.post("/p/leistungen/leistungen/projekt_aendern", headers=h,
                   data={"nummer": "P0002", "zugang": "Code 1234", "name": "Seeweg 4, 3700 Spiez"})
        assert r.status_code == 204, r.text
        assert "Code 1234" in c.get("/debitoren/kunden/K0001").text

"""«Erfassen»: hours and products per customer, filtered list, billed per customer as a Debitor."""
from decimal import Decimal

import pytest

from aeradex import api
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book

from aeradex_leistungen import daten

IBAN = "CH9300762011623852957"


@pytest.fixture
def ui(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    b = make_book(tmp_path, iban=IBAN, git=True, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  eroeffnung={"1020": 20000, "2800": -20000}, plugins=["leistungen"])
    api.customer_add(Book(b.root), name="Pascal Mühlberg", firma="Torfix AG", strasse="Hagmattstrasse", nr="10",
                     plz="4207", ort="Bretzwil")
    api.customer_add(Book(b.root), name="Anna Keller", firma="Kunde AG", strasse="Marktgasse", nr="12",
                     plz="3011", ort="Bern")
    api.write(Book(b.root), "Person", daten.set_person, "", 120, None, "Benjamin", 42)
    app = create_app(b.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        c.headers.update({"X-CSRF": app.state.ui.csrf})
        yield c, Book(b.root)


def post(c, url, data, ok=True):
    r = c.post(url, headers={"HX-Request": "true"}, data=data)
    if ok:
        assert r.status_code == 204, r.text
    return r


A = "/p/leistungen/leistungen/"


def test_erfassen_filtern_abrechnen(ui):
    c, book = ui
    page = c.get("/p/leistungen/leistungen").text
    assert 'class="on">Erfassen' in page and "Neuer Eintrag" in page                 # the new start tab

    # products and services are captured once, then only picked
    post(c, A + "produkt_neu", {"text": "Malerarbeiten", "einheit": "h", "preis": "95"})
    post(c, A + "produkt_neu", {"text": "Dispersionsfarbe weiss", "einheit": "l", "preis": "45"})
    prods = {p["text"]: nr for nr, p in daten.products(Book(book.root)).items()}
    page = c.get("/p/leistungen/leistungen?tab=erfassen").text
    assert "Malerarbeiten · 95.00/h" in page and "Dispersionsfarbe weiss · 45.00/l" in page

    wer = next(iter(daten.people(Book(book.root))))
    post(c, A + "neu", {"datum": "2026-10-07", "kunde": "K0001", "was": prods["Malerarbeiten"], "menge": "3.5",
                         "text": "Fassade spachteln", "wer": wer})
    post(c, A + "neu", {"datum": "2026-10-08", "kunde": "K0001", "was": prods["Dispersionsfarbe weiss"], "menge": "12",
                         "wer": wer})
    post(c, A + "neu", {"datum": "2026-10-08", "kunde": "K0002", "was": "ZEIT", "menge": "2", "text": "Beratung",
                         "wer": wer})
    assert post(c, A + "neu", {"datum": "2026-10-08", "kunde": "", "was": "ZEIT", "menge": "1", "text": "x"},
                ok=False).status_code != 204                                           # a customer is required

    page = c.get("/p/leistungen/leistungen?tab=erfassen").text
    assert "<h2>Torfix AG" in page and "<h2>Kunde AG" in page and "Fassade spachteln" in page
    assert "12 l" in page and "3.5 h" in page

    # filters: customer, date, kind
    only_torfix = c.get("/p/leistungen/leistungen?tab=erfassen&kunde=K0001").text
    assert "Fassade spachteln" in only_torfix and "Beratung" not in only_torfix
    only_products = c.get("/p/leistungen/leistungen?tab=erfassen&art=Produkt").text
    assert "Dispersionsfarbe" in only_products and "Fassade spachteln" not in only_products
    on_day = c.get("/p/leistungen/leistungen?tab=erfassen&von=2026-10-08&bis=2026-10-08").text
    assert "Fassade spachteln" not in on_day and "Beratung" in on_day

    # bill Torfix: one invoice (Debitor), entries become «abgerechnet»
    ids = [e["id"] for e in daten.entries(Book(book.root)) if e["kunde"] == "K0001"]
    r = post(c, A + "rechnung", {"ids": ids, "rapport": "1"})
    assert r.headers["HX-Redirect"].startswith("/debitoren/rechnung/R-")
    inv = api.invoice_list(Book(book.root))
    assert len(inv) == 1 and inv[0]["kunde"] == "K0001"
    assert Decimal(str(inv[0]["total"])) >= Decimal("872.50")                         # 3.5 × 95 + 12 × 45, plus MWST if any

    open_now = c.get("/p/leistungen/leistungen?tab=erfassen").text
    assert "<h2>Torfix AG" not in open_now and "<h2>Kunde AG" in open_now              # default filter: not billed
    billed = c.get("/p/leistungen/leistungen?tab=erfassen&status=abgerechnet").text
    assert inv[0]["nummer"] in billed and "Fassade spachteln" in billed
    alle = c.get("/p/leistungen/leistungen?tab=erfassen&status=").text
    assert "Beratung" in alle and "Fassade spachteln" in alle

    # delete an open entry
    rest = [e["id"] for e in daten.entries(Book(book.root)) if e["kunde"] == "K0002"]
    post(c, A + "loeschen", {"id": rest[0]})
    assert not [e for e in daten.entries(Book(book.root)) if e["kunde"] == "K0002"]
    assert_clean(Book(book.root))


def test_old_tabs_still_work(ui):
    c, _ = ui
    for tab in ("woche", "abrechnen", "abwesenheiten", "auswertung", "stammdaten"):
        assert c.get(f"/p/leistungen/leistungen?tab={tab}").status_code == 200, tab

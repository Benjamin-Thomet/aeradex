from decimal import Decimal

import pytest

from allkvitt import api, budget
from allkvitt.book import Book
from allkvitt.testing import make_book

pytest.importorskip("starlette")


@pytest.fixture
def client(tmp_path):
    from starlette.testclient import TestClient
    from allkvitt.web.app import create_app
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000})
    for m in (1, 2, 3):
        api.post_entry(Book(b.root), f"2026-{m:02d}-05", "1020", "3400", 5000 + m * 100, "Umsatz")
        api.post_entry(Book(b.root), f"2026-{m:02d}-25", "6000", "1020", "1200", "Miete")
    app = create_app(b.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        c.h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        c.root = b.root
        yield c


def test_pages_and_exports(client):
    c = client
    assert 'href="/berichte"' in c.get("/journal").text                            # tab under Buchhaltung
    for q in ("typ=erfolgsrechnung&jahr=2026&spalten=monat&vergleich=vorperiode&detail=1",
              "typ=bilanz&jahr=2026&periode=q1", "typ=geldfluss&jahr=2026", "typ=kennzahlen&jahr=2026",
              "typ=debitoren&jahr=2026", "typ=kreditoren&jahr=2026", "typ=umsatz&jahr=2026&nach=monat"):
        r = c.get(f"/berichte?{q}")
        assert r.status_code == 200 and "alert error" not in r.text, q
    page = c.get("/berichte?typ=erfolgsrechnung&jahr=2026&spalten=monat&detail=1").text
    assert "<svg class=\"chart\"" in page and "/konten/3400?jahr=2026&amp;von=2026-01-01&amp;bis=2026-01-31" in page
    led = c.get("/konten/3400?jahr=2026&von=2026-02-01&bis=2026-02-28").text.replace("&#39;", "'")
    assert "Anfangssaldo 01.02.2026" in led and "5'200.00" in led and "5'300.00" not in led
    r = c.get("/berichte/export.pdf?typ=erfolgsrechnung&jahr=2026")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert c.get("/berichte/export.csv?typ=bilanz&jahr=2026").status_code == 200
    assert c.get("/berichte/export.pdf?typ=gibtsnicht").status_code == 400


def test_actions(client):
    c = client
    r = c.post("/berichte/vorlage", headers=c.h, data={"name": "Q1", "typ": "erfolgsrechnung", "periode": "q1",
                                                      "spalten": "monat", "jahr": "2026"})
    assert r.status_code == 204, r.text
    assert "Vorlage «Q1»" in c.get("/berichte?vorlage=Q1&jahr=2026").text
    r = c.post("/berichte/kommentar", headers=c.h, data={"text": "Gut.", "typ": "erfolgsrechnung", "jahr": "2026"})
    assert r.status_code == 204, r.text
    assert "Gut." in c.get("/berichte?typ=erfolgsrechnung&jahr=2026").text
    r = c.post("/berichte/monat", headers=c.h, data={"monat": "2026-03"})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/datei/berichte/2026-03/")
    assert c.get(r.headers["HX-Redirect"]).content.startswith(b"%PDF")
    r = c.post("/berichte/vorlage-loeschen", headers=c.h, data={"name": "Q1"})
    assert r.status_code == 204


def test_budget_grid(client):
    c = client
    assert c.get("/berichte/budget?jahr=2026").status_code == 200
    data = {"jahr": "2026", **{f"b_3400_{m}": "5000" for m in range(1, 13)}, **{f"b_6000_{m}": "" for m in range(1, 13)}}
    data["b_6000_1"] = "1300"
    r = c.post("/berichte/budget/speichern", headers=c.h, data=data)
    assert r.status_code == 204, r.text
    months = budget.load(Book(c.root), 2026)
    assert months["3400"] == [Decimal("5000.00")] * 12 and months["6000"][0] == Decimal("1300.00")
    page = c.get("/berichte?typ=erfolgsrechnung&jahr=2026&periode=q1&vergleich=budget").text
    assert "15'000.00" in page.replace("&#39;", "'")
    r = c.post("/berichte/budget/vorjahr", headers=c.h, data={"jahr": "2027", "prozent": "5"})
    assert r.status_code == 204, r.text

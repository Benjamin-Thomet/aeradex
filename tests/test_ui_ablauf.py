"""The simplified UI: six sections, one upload, drafts → Freigeben (& nächster) per list, the bank
reconcile cards, and the Übersicht as the one place that lists what waits."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from allkvitt import api, bank  # noqa: E402
from allkvitt.book import Book  # noqa: E402
from allkvitt.testing import assert_clean, make_book  # noqa: E402
from allkvitt.web.app import create_app  # noqa: E402
from camt_sample import entry, statement  # noqa: E402
from test_eingang import RECEIPT, pdf  # noqa: E402

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "1000": 500, "2800": -5500}, iban=IBAN,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    (b.root / "inbox").mkdir(exist_ok=True)
    api.settings_update(Book(b.root), kreditoren={"agent_automatisch": False})
    return Book(b.root)


@pytest.fixture
def client(book):
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        c.h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        yield c


def sections(html: str) -> list[str]:
    nav = re.search(r'<nav class="sections".*?</nav>', html, re.S).group(0)
    return re.findall(r'data-key="\w">([^<]+)<', nav)


def camt(tmp_path: Path, items, name="feb") -> bytes:
    return statement(IBAN, "5000.00", items, "2026-03-01", "2026-03-31", stmt_id=name.upper())


def test_six_sections_and_old_review_page_redirects(client):
    page = client.get("/").text
    assert sections(page) == ["Übersicht", "Einkauf", "Verkauf", "Bank", "Lohn", "Buchhaltung"]
    r = client.get("/pruefen", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Vorschläge" in client.get("/vorschlaege").text


def test_upload_lands_in_drafts_and_approve_and_next(client, book, tmp_path):
    files = [("dateien", (f"q{i}.pdf", pdf(tmp_path / f"q{i}.pdf", RECEIPT).read_bytes(), "application/pdf"))
             for i in (1, 2)]
    r = client.post("/eingang/einlesen", headers=client.h, data={}, files=files)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/kreditoren?status=entwurf"
    page = client.get("/kreditoren").text                      # drafts exist → the Entwürfe tab is the default
    assert "ENT-0001" in page and "ENT-0002" in page
    assert re.search(r'class="on" aria-current=page>Entwürfe<span class="badge">2</span>', page)
    review = client.get("/eingang/quittung?entwurf=ENT-0001").text
    assert "Entwurf 1 von 2" in review and "Freigeben &amp; nächster" in review
    form = {"zahlung": "konto", "zahlkonto": "1000", "datum": "2026-03-05", "betrag": "45.50", "text": "Papier",
            "konto": "6500"}
    r = client.post("/eingang/quittung", headers=client.h, data={**form, "entwurf": "ENT-0001", "weiter": "1"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/eingang/quittung?entwurf=ENT-0002"
    r = client.post("/eingang/quittung", headers=client.h, data={**form, "entwurf": "ENT-0002", "weiter": "1"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/kreditoren?status=entwurf"
    assert api.bill_drafts(Book(book.root)) == []
    assert_clean(Book(book.root))


def test_statement_through_the_global_upload_goes_to_the_bank(client, book, tmp_path):
    xml = camt(tmp_path, [(entry("12.00", "DBIT", "2026-03-31", party="UBS", ustrd="Kontoführung"), "-12.00")])
    r = client.post("/eingang/einlesen", headers=client.h, data={}, files=[("dateien", ("camt.xml", xml, "text/xml"))])
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/bank", r.text
    assert len(bank.transactions(Book(book.root))) == 1
    assert "Bankbewegung abgleichen" in client.get("/").text   # the Übersicht line that leads to the bank


def test_reconcile_card_ok_search_and_book_with_rule(client, book, tmp_path):
    api.customer_add(Book(book.root), name="Peter Privat", strasse="Seeweg", nr="1", plz="3600", ort="Thun")
    api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "450"}], "2026-03-03")
    f = tmp_path / "s.xml"
    f.write_bytes(camt(tmp_path, [
        (entry("450.00", "CRDT", "2026-03-21", party="Peter Privat", ustrd="Beratung"), "450.00"),
        (entry("200.00", "CRDT", "2026-03-22", party="Unbekannt AG", ustrd="Akonto"), "200.00"),
        (entry("29.90", "DBIT", "2026-03-25", party="Swisscom", ustrd="Abo"), "-29.90")]))
    api.bank_import(Book(book.root), str(f))
    txs = {t["Betrag"]: t["ID"] for t in bank.transactions(Book(book.root))}
    page = client.get("/bank").text
    assert page.count('class="bankcard"') == 3 and f'id="karte-{txs["450.00"]}"' in page and ">OK</button>" in page
    # one click on OK: the card answers in place
    r = client.post(f"/bank/{txs['450.00']}/uebernehmen", headers=client.h,
                    data={"art": "rechnung", "ziel": "R-2026-0001", "karte": "1"})
    assert r.status_code == 200 and "bankcard done" in r.text and r.headers["HX-Retarget"] == f"#karte-{txs['450.00']}"
    # search: nothing open with 200.00; by name nothing either (invoice is paid) → message
    assert "Nichts mit genau diesem Betrag" in client.get(f"/bank/{txs['200.00']}/suchen").text
    # book a new one and make it a rule
    r = client.post(f"/bank/{txs['-29.90']}/buchen", headers=client.h,
                    data={"konto": "6510  Telefon", "text": "Swisscom Abo", "regel": "1", "karte": "1"})
    assert r.status_code == 200 and "bankcard done" in r.text, r.text
    assert bank.rules(Book(book.root))[0]["gegenpartei"] == "Swisscom"
    assert_clean(Book(book.root))


def test_card_error_stays_in_the_card(client, book, tmp_path):
    f = tmp_path / "s.xml"
    f.write_bytes(camt(tmp_path, [(entry("10.00", "DBIT", "2026-03-25", party="X"), "-10.00")]))
    api.bank_import(Book(book.root), str(f))
    tid = bank.transactions(Book(book.root))[0]["ID"]
    r = client.post(f"/bank/{tid}/ignorieren", headers=client.h, data={"grund": "", "karte": "1"})
    assert r.status_code == 200 and "alert error" in r.text and "HX-Retarget" not in r.headers

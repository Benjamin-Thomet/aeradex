"""Open items per Stichtag for Debitoren and Kreditoren: lists, PDF, reconciliation, the report."""
from datetime import date
from decimal import Decimal

import pytest

from allkvitt import api, kreditoren, reports
from allkvitt.book import Book
from allkvitt.testing import make_book

D = Decimal


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "2800": -5000}, strasse="Weg", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    api.supplier_add(Book(b.root), name="Papeterie AG", iban="CH93 0076 2011 6238 5295 7", konto="6500")
    api.bill_add(Book(b.root), "L0001", "300.00", datum="2026-01-10", rechnungsnr="A-1")
    api.bill_add(Book(b.root), "L0001", "120.00", datum="2026-03-01", rechnungsnr="A-2")
    nr = sorted(kreditoren.bills(Book(b.root)))[0]
    api.bill_pay(Book(b.root), nr, datum="2026-03-20")                       # first bill paid after 28.02.
    return Book(b.root)


def test_open_payables_per_stichtag(book):
    ap = kreditoren.open_payables(book, date(2026, 2, 28))
    assert [p["rechnungsnr"] for p in ap["posten"]] == ["A-1"]               # entered before, paid after the Stichtag
    assert ap["total_offen"] == D("300.00") and ap["saldo_kreditoren"] == ap["total_offen"] and ap["differenz"] == 0
    assert ap["posten"][0]["alter_tage"] == 49 and ap["kategorien"]["31–60"] == D("300.00")
    now = kreditoren.open_payables(book, date(2026, 3, 31))
    assert [p["rechnungsnr"] for p in now["posten"]] == ["A-2"] and now["differenz"] == 0
    rep = reports.run(book, "kreditoren", stichtag="2026-02-28")              # the report: same logic, same total
    assert rep["zeilen"][-1]["werte"]["total"] == D("300.00") and rep["hinweise"] == []


def test_pages_and_pdfs(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from allkvitt.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/kreditoren/offene-posten?stichtag=2026-02-28").text
        assert "A-1" in page and "A-2" not in page and "✓" in page
        assert 'href="/pdf/kreditoren?stichtag=2026-02-28"' in page
        assert "/berichte?typ=kreditoren&stichtag=2026-02-28" in page
        assert 'href="/kreditoren/offene-posten"' in c.get("/kreditoren").text      # tab in the section
        deb = c.get("/debitoren/offene-posten?stichtag=2026-02-28").text
        assert 'href="/pdf/debitoren?stichtag=2026-02-28"' in deb and "/berichte?typ=debitoren" in deb
        r = c.get("/pdf/kreditoren?stichtag=2026-02-28")
        assert r.status_code == 200 and r.content.startswith(b"%PDF") and "2026-02-28" in r.headers["content-disposition"]
        r = c.get("/pdf/debitoren?stichtag=2026-02-28")
        assert r.status_code == 200 and "2026-02-28" in r.headers["content-disposition"]
        assert c.get("/pdf/kreditoren?stichtag=quatsch").status_code == 400
        assert c.get("/kreditoren/offene-posten?stichtag=quatsch").status_code == 400


def test_pdf_text_uses_stichtag(book):
    from pypdf import PdfReader
    import io
    from allkvitt import pdf
    data = pdf.payables_pdf(book, kreditoren.open_payables(book, date(2026, 2, 28)))
    text = PdfReader(io.BytesIO(data)).pages[0].extract_text()
    assert "per 28.02.2026" in text and "A-1" in text and "A-2" not in text


def test_default_is_today(book):
    assert kreditoren.open_payables(book)["stichtag"] == date.today()

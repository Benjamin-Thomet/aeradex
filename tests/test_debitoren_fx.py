"""Customer invoices in EUR: QR-bill in EUR, BAZG rate, payment differences, credit notes, Stichtag."""
from __future__ import annotations

from decimal import Decimal

import pytest

from aeradex import api, fx, invoices
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"
DAILY = {"20260302": "0.95", "20260415": "0.96", "20260630": "0.92", "20260710": "0.94"}


def bazg(url):
    d = url.split("d=")[1][:8]
    if d not in DAILY:
        return b"<wechselkurse/>"
    return (f'<wechselkurse><datum>x</datum><devise code="eur"><waehrung>1 EUR</waehrung>'
            f'<kurs>{DAILY[d]}</kurs></devise></wechselkurse>').encode()


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "_get", bazg)
    b = make_book(tmp_path, iban=IBAN, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  eroeffnung={"1020": 5000, "2800": -5000})
    api.customer_add(b, name="Max", firma="Kunde GmbH", strasse="Allee", nr="2", plz="10115", ort="Berlin", land="DE")
    return Book(b.root)


def saldo(book, konto):
    return Decimal(api.ledger(Book(book.root), konto, 2026)["saldo"])


def invoice(book, **kw):
    return api.invoice_create(book, "K0001", [{"text": "Beratung", "menge": 10, "preis": "100"}], "2026-03-02",
                              waehrung="EUR", **kw)["rechnung"]


def test_eur_invoice_with_qr_bill(book):
    meta = invoice(book)
    assert meta["waehrung"] == "EUR" and meta["kurs"] == "0.95"
    assert saldo(book, "1100") == Decimal("950.00") and saldo(book, "3400") == Decimal("-950.00")
    pdf = (book.root / "rechnungen" / "2026" / f"{meta['nummer']}.pdf").read_bytes()
    assert pdf.startswith(b"%PDF")
    st = invoices.invoice_state(Book(book.root), invoices.invoice(Book(book.root), meta["nummer"]))
    assert st["offen"] == Decimal("1000.00") and st["offen_chf"] == Decimal("950.00")
    assert api.receivables(Book(book.root))["differenz"] in ("0.00", 0, Decimal(0))
    assert_clean(Book(book.root))
    with pytest.raises(BookError, match="CHF oder EUR"):
        api.invoice_create(Book(book.root), "K0001", [{"text": "x", "menge": 1, "preis": "1"}], waehrung="USD")


def test_payment_in_chf_books_exchange_gain(book):
    nr = invoice(book)["nummer"]
    api.invoice_pay(Book(book.root), nr, "962.00", "2026-04-15")        # the bank credited CHF 962
    b = Book(book.root)
    assert saldo(b, "1100") == 0 and saldo(b, "6952") == Decimal("-12.00")
    assert invoices.invoice_state(b, invoices.invoice(b, nr))["status"] == "bezahlt"
    assert_clean(b)


def test_bank_import_matches_eur_invoice_paid_in_chf(book, tmp_path):
    meta = invoice(book)
    xml = statement(IBAN, "5000.00", [(entry("955.00", "CRDT", "2026-04-15", party="KUNDE GMBH",
                                             ustrd=f"Rechnung {meta['nummer']}"), "955.00")], "2026-04-15", "2026-04-15")
    f = tmp_path / "s.xml"
    f.write_bytes(xml)
    assert api.bank_import(Book(book.root), str(f))["import_"]["gebucht"] == 1
    b = Book(book.root)
    assert saldo(b, "6952") == Decimal("-5.00") and saldo(b, "1100") == 0
    assert_clean(b)


def test_credit_note_at_invoice_rate_then_partial_payment(book):
    nr = invoice(book)["nummer"]
    api.invoice_credit(Book(book.root), nr, "100", "2026-03-10", grund="Rabatt")
    b = Book(book.root)
    st = invoices.invoice_state(b, invoices.invoice(b, nr))
    assert st["offen"] == Decimal("900.00") and st["offen_chf"] == Decimal("855.00")
    api.invoice_pay(b, nr, None, "2026-04-15", fw="400")                 # 400 × 0.96 = 384 for 380 booked
    b = Book(book.root)
    assert saldo(b, "6952") == Decimal("-4.00")
    assert invoices.invoice_state(b, invoices.invoice(b, nr))["offen"] == Decimal("500.00")
    assert_clean(b)


def test_stichtag_revaluation_of_open_invoice(book):
    nr = invoice(book)["nummer"]
    prev = api.fx_preview(Book(book.root), "2026-06-30")
    assert prev["debitoren"][0]["chf_neu"] == "920.00" and prev["debitoren"][0]["differenz"] == "-30.00"
    api.fx_revalue(Book(book.root), "2026-06-30")
    b = Book(book.root)
    assert saldo(b, "1100") == Decimal("920.00") and saldo(b, "6942") == Decimal("30.00")
    api.invoice_pay(b, nr, None, "2026-07-10")                           # 0.94 → 940 for 920 carried
    b = Book(book.root)
    assert saldo(b, "1100") == 0 and saldo(b, "6952") == Decimal("-20.00")
    assert_clean(b)


def test_external_eur_invoice(book):
    meta = api.invoice_external(Book(book.root), "K0001", "500.00", datum="2026-03-02", waehrung="EUR",
                                rechnungsnr="X-9")["rechnung"]
    assert meta["kurs"] == "0.95" and saldo(Book(book.root), "1100") == Decimal("475.00")
    assert_clean(Book(book.root))


def test_ui_eur_invoice_and_payment(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/debitoren/neu", headers=h, data={"kunde": "K0001", "datum": "2026-03-02", "waehrung": "EUR",
                                                       "p_text": "Beratung", "p_menge": "10", "p_preis": "100", "p_einheit": "h", "p_konto": ""})
        assert r.status_code == 204, r.text
        nr = r.headers["HX-Redirect"].rsplit("/", 1)[1]
        page = c.get(f"/debitoren/rechnung/{nr}").text
        assert "Offen EUR" in page and "Buchwert offen CHF" in page
        assert "EUR" in c.get("/debitoren").text
        r = c.post(f"/debitoren/rechnung/{nr}/zahlung", headers=h,
                   data={"datum": "2026-04-15", "konto": "1020", "fw": "1000.00", "betrag": "958.00"})
        assert r.status_code == 204, r.text
    assert saldo(book, "6952") == Decimal("-8.00")
    assert_clean(Book(book.root))

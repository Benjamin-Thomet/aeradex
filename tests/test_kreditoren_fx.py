"""Supplier bills in foreign currency (BAZG rates) and split over several accounts."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, check, fx, kreditoren as kred
from allkvitt.book import Book, BookError
from allkvitt.testing import assert_clean, make_book

DE_IBAN = "DE89370400440532013000"
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
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, iban="CH93 0076 2011 6238 5295 7")
    api.supplier_add(b, name="Druckhaus Berlin GmbH", iban=DE_IBAN, konto="6600", land="DE", ort="Berlin", plz="10115",
                     strasse="Unter den Linden", nr="1")
    return Book(b.root)


def saldo(book, konto, year=2026):
    return Decimal(api.ledger(Book(book.root), konto, year)["saldo"])


def test_eur_bill_booked_at_bazg_rate(book):
    out = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")
    meta = out["kreditor"]
    assert meta["waehrung"] == "EUR" and meta["kurs"] == "0.95"
    assert saldo(book, "6600") == Decimal("950.00") and saldo(book, "2000") == Decimal("-950.00")
    rows = [r for r in Book(book.root).rows if r.quelle == f"kreditor:{meta['nummer']}"]
    assert all(r.waehrung == "EUR" and r.fw == Decimal("1000.00") for r in rows)
    st = kred.state(Book(book.root), kred.bill(Book(book.root), meta["nummer"]))
    assert st["offen"] == Decimal("1000.00") and st["offen_chf"] == Decimal("950.00") and st["waehrung"] == "EUR"
    assert api.payables(Book(book.root))["differenz"] == "0.00"
    assert_clean(Book(book.root))


def test_payment_from_chf_account_books_exchange_difference(book):
    nr = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")["kreditor"]["nummer"]
    api.bill_pay(Book(book.root), nr, "2026-04-15", "962.00")          # what the bank actually debited
    b = Book(book.root)
    assert saldo(b, "2000") == 0 and saldo(b, "6942") == Decimal("12.00")
    assert kred.state(b, kred.bill(b, nr))["status"] == "bezahlt"
    assert_clean(b)


def test_payment_at_bazg_rate_gain(book):
    nr = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR", kurs="0.97")["kreditor"]["nummer"]
    api.bill_pay(Book(book.root), nr, "2026-04-15")                    # BAZG 0.96 → 960
    b = Book(book.root)
    assert saldo(b, "1020") == Decimal("19040.00") and saldo(b, "6952") == Decimal("-10.00")
    assert_clean(b)


def test_payment_from_eur_account(book):
    api.add_account(book, "1025", "Bank EUR", "aktiv", waehrung="EUR")
    api.post_entry(Book(book.root), "2026-03-01", "1025", "1020", "2000", "Umbuchung EUR", waehrung="EUR", kurs="0.95")
    nr = api.bill_add(Book(book.root), "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")["kreditor"]["nummer"]
    api.bill_pay(Book(book.root), nr, "2026-04-15", konto="1025")
    b = Book(book.root)
    led = api.ledger(b, "1025", 2026)
    assert led["saldo_fw"] == "1000.00" and led["saldo"] == "940.00"     # 1900 − 960
    assert saldo(b, "6942") == Decimal("10.00")
    assert_clean(b)


def test_partial_payment_clears_proportionally(book):
    nr = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")["kreditor"]["nummer"]
    api.bill_pay(Book(book.root), nr, "2026-04-15", fw="400")
    b = Book(book.root)
    st = kred.state(b, kred.bill(b, nr))
    assert st["offen"] == Decimal("600.00") and st["offen_chf"] == Decimal("570.00") and st["status"] == "offen"
    assert saldo(b, "6942") == Decimal("4.00")                          # 384 paid for 380 booked
    with pytest.raises(BookError, match="übersteigt"):
        api.bill_pay(b, nr, "2026-04-15", fw="700")
    assert_clean(b)


def test_stichtag_revaluation_of_open_bill_then_payment(book):
    nr = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")["kreditor"]["nummer"]
    prev = api.fx_preview(Book(book.root), "2026-06-30")
    assert prev["kreditoren"][0]["chf_neu"] == "920.00" and prev["kreditoren"][0]["differenz"] == "-30.00"
    api.fx_revalue(Book(book.root), "2026-06-30")
    b = Book(book.root)
    assert saldo(b, "2000") == Decimal("-920.00") and saldo(b, "6952") == Decimal("-30.00")
    assert kred.state(b, kred.bill(b, nr))["offen_chf"] == Decimal("920.00")
    assert api.payables(b)["differenz"] == "0.00"
    api.bill_pay(b, nr, "2026-07-10")                                  # 0.94 → 940 for a debt carried at 920
    b = Book(book.root)
    assert saldo(b, "2000") == 0 and saldo(b, "6942") == Decimal("20.00")
    assert_clean(b)


def test_revaluation_refused_after_later_payment(book):
    nr = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")["kreditor"]["nummer"]
    api.bill_pay(Book(book.root), nr, "2026-07-10", fw="100")
    with pytest.raises(BookError, match="vor diesen Zahlungen"):
        api.fx_revalue(Book(book.root), "2026-06-30")


def test_split_over_accounts_with_vat(book):
    api.settings_update(book, mwst={"methode": "effektiv"})
    api.supplier_add(Book(book.root), name="Bürohaus AG", iban="CH5604835012345678009")
    lines = [{"konto": "6500", "betrag": "108.10", "mwst": "I81", "text": "Papier"},
             {"konto": "6570", "betrag": "540.50", "mwst": "I81", "text": "Software"},
             {"konto": "1520", "betrag": "1081.00", "mwst": "I81", "text": "Laptop"}]
    out = api.bill_add(Book(book.root), "L0002", "1729.60", datum="2026-03-05", positionen=lines)
    nr = out["kreditor"]["nummer"]
    b = Book(book.root)
    assert saldo(b, "6500") == Decimal("100.00") and saldo(b, "6570") == Decimal("500.00")
    assert saldo(b, "1520") == Decimal("1000.00") and saldo(b, "1171") == Decimal("129.60")
    assert saldo(b, "2000") == Decimal("-1729.60")
    assert kred.bill_fingerprint_ok(kred.bill(b, nr))
    assert_clean(b)
    with pytest.raises(BookError, match="Positionen ergeben"):
        api.bill_add(b, "L0002", "100.00", positionen=[{"konto": "6500", "betrag": "90"}])


def test_split_eur_bill(book):
    lines = [{"konto": "6600", "betrag": "700.00"}, {"konto": "6570", "betrag": "300.00", "text": "Hosting"}]
    nr = api.bill_add(book, "L0001", "1000.00", datum="2026-03-02", waehrung="EUR", positionen=lines)["kreditor"]["nummer"]
    b = Book(book.root)
    assert saldo(b, "6600") == Decimal("665.00") and saldo(b, "6570") == Decimal("285.00")
    api.bill_pay(b, nr, "2026-04-15")
    assert_clean(Book(book.root))


def test_old_bills_keep_their_fingerprint():
    meta = {"nummer": "E-2026-0001", "lieferant": "L0001", "name": "X", "rechnungsnr": "", "datum": "2026-03-02",
            "faellig": "2026-04-01", "betrag": "86.40", "waehrung": "CHF", "iban": "CH4431999123000889012",
            "referenz_typ": "NON", "referenz": "", "mitteilung": "", "konto": "6500", "mwst": "",
            "kreditorenkonto": "2000"}
    import hashlib, json
    frozen = {k: str(meta.get(k) if meta.get(k) is not None else "") for k in kred.FROZEN}
    frozen["betrag"] = "86.40"
    old = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    assert kred.fingerprint(meta) == old


def test_pain001_one_block_per_currency(book, tmp_path):
    api.add_account(book, "1025", "Bank EUR", "aktiv", waehrung="EUR")
    api.settings_update(Book(book.root), bankkonten={"CH5800791123000889012": "1025"})
    api.supplier_add(Book(book.root), name="Swisscom", iban="CH5604835012345678009", konto="6510")
    a = api.bill_add(Book(book.root), "L0001", "1000.00", datum="2026-03-02", waehrung="EUR")["kreditor"]["nummer"]
    c = api.bill_add(Book(book.root), "L0002", "59.00", datum="2026-03-02")["kreditor"]["nummer"]
    res = api.payment_run(Book(book.root), [a, c], "2026-04-15")
    xml = (book.root / res["zahlungslauf"]["datei"]).read_text()
    assert xml.count("<PmtInf>") == 2
    assert 'Ccy="EUR">1000.00' in xml and 'Ccy="CHF">59.00' in xml
    assert "<IBAN>CH5800791123000889012</IBAN>" in xml                  # EUR paid from the EUR account
    api.payment_run_book(Book(book.root), next((book.root / "zahlungen").glob("*.xml")).name, "2026-04-15")
    b = Book(book.root)
    assert api.ledger(b, "1025", 2026)["saldo_fw"] == "-1000.00"
    assert_clean(b)
    import os
    xsd = os.environ.get("ALLKVITT_SPS_XSD")
    if xsd and Path(xsd).exists():
        from lxml import etree
        etree.XMLSchema(etree.parse(xsd)).assertValid(etree.fromstring(xml.encode()))


def test_ui_foreign_split_bill_and_payment(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from allkvitt.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert "Auf mehrere Konten aufteilen" in c.get("/kreditoren/neu").text
        r = c.post("/kreditoren/neu", headers=h, data={
            "lieferant": "L0001", "betrag": "1000.00", "waehrung": "EUR", "datum": "2026-03-02",
            "iban": DE_IBAN, "p_konto": ["6600", "6570"], "p_betrag": ["700.00", "300.00"],
            "p_text": ["Flyer", "Hosting"]})
        assert r.status_code == 204, r.text
        nr = r.headers["HX-Redirect"].rsplit("/", 1)[1]
        page = c.get(f"/kreditoren/rechnung/{nr}").text
        assert "Betrag EUR" in page and "0.95" in page and "Hosting" in page and "Buchwert offen CHF" in page
        assert "EUR</span> 1&#39;000.00" in c.get("/kreditoren").text or "EUR" in c.get("/kreditoren").text
        r = c.post(f"/kreditoren/rechnung/{nr}/zahlung", headers=h,
                   data={"datum": "2026-04-15", "konto": "1020", "fw": "1000.00", "betrag": "958.40"})
        assert r.status_code == 204, r.text
        assert "Offene Kreditoren in Fremdwährung" not in c.get("/abschluss?jahr=2026&stichtag=2026-06-30").text
    b = Book(book.root)
    assert saldo(b, "6942") == Decimal("8.40") and saldo(b, "2000") == 0
    assert_clean(b)

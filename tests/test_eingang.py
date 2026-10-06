"""Belegeingang beyond supplier bills: receipts (with bank matching) and invoices issued outside allkvitt."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, bank, erfassung, invoices
from allkvitt.book import Book, BookError
from allkvitt.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


def pdf(path: Path, lines: list[str]) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    y = 790
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()
    return path


RECEIPT = ["Papeterie Muster AG", "Marktgasse 14", "3011 Bern", "CHE-111.222.333 MWST", "",
           "Quittung Nr. 4711", "Datum: 05.03.2026", "Druckerpapier A4          45.50",
           "MWST 8.1 %                  3.41", "Total CHF                  45.50", "Bezahlt mit Maestro"]
OWN_INVOICE = ["Test GmbH", "Hauptstrasse 1", "3000 Bern", "", "Kunde Muster AG", "Gasse 3", "3011 Bern", "",
               "Rechnung Nr. 2026-77", "Datum: 02.03.2026", "Zahlbar innert 30 Tagen", "Beratung März   1500.00",
               "Total CHF 1500.00", f"IBAN {IBAN}"]


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "1000": 500, "2800": -5500}, iban=IBAN,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    (b.root / "inbox").mkdir(exist_ok=True)
    return Book(b.root)


def import_bank(book: Book, tmp_path: Path, amount: str, day: str, party: str = "PAPETERIE MUSTER"):
    signed = amount if not amount.startswith("-") else amount
    side = "DBIT" if amount.startswith("-") else "CRDT"
    xml = statement(IBAN, "5000.00", [(entry(amount.lstrip("-"), side, day, party=party), signed)], day, day,
                    stmt_id=f"S-{day}-{amount}")
    f = tmp_path / f"camt-{day}-{amount}.xml"
    f.write_bytes(xml)
    return api.bank_import(book, str(f))


def test_kinds_are_recognised(book):
    receipt = erfassung.parse_text("\n".join(RECEIPT))
    assert erfassung.classify(book, {k: {"wert": v} for k, v in receipt.items()}, "\n".join(RECEIPT))[0] == "quittung"
    own = erfassung.parse_text("\n".join(OWN_INVOICE), "Test GmbH")
    assert erfassung.classify(book, {k: {"wert": v} for k, v in own.items()}, "")[0] == "debitor"
    assert own["name"] == "Kunde Muster AG"                                   # the customer, not us
    assert receipt["zahlungsart"] == "karte"


def test_receipt_paid_by_card_is_booked_with_its_bank_movement(book, tmp_path):
    import_bank(book, tmp_path, "-45.50", "2026-03-06")
    pdf(book.root / "inbox" / "quittung.pdf", RECEIPT)
    d = api.bill_draft_create(Book(book.root), "inbox/quittung.pdf")["entwurf"]
    assert d["art"] == "quittung" and d["zahlung"]["art"] == "bank"
    api.bill_draft_update(Book(book.root), d["id"], "Hand", "6500")
    api.receipt_book(Book(book.root), d["id"], "2026-03-05", "Druckerpapier", "45.50", "6500",
                     zahlung=erfassung.draft(Book(book.root), d["id"])["zahlung"])
    b = Book(book.root)
    tx = bank.transactions(b)[0]
    assert tx["Status"] == "gebucht"
    assert Decimal(api.ledger(b, "6500", 2026)["saldo"]) == Decimal("45.50")
    assert Decimal(api.ledger(b, "1020", 2026)["saldo"]) == Decimal("4954.50")      # once, not twice
    assert any(p.name.startswith(tx["Beleg"]) for p in (b.root / "belege" / "2026").iterdir())
    assert not (b.root / "inbox" / "quittung.pdf").exists() and api.bill_drafts(b) == []
    assert_clean(b)


def test_receipt_for_existing_booking_is_only_filed(book):
    api.post_entry(book, "2026-03-05", "6500", "1020", "45.50", "Papier (ohne Beleg)")
    pdf(book.root / "inbox" / "q.pdf", RECEIPT)
    d = api.bill_draft_create(Book(book.root), "inbox/q.pdf")["entwurf"]
    assert d["zahlung"] == {"art": "buchung", "beleg": "26-001", "konto": "1020", "quelle": "Journalabgleich",
                            "text": "Beleg 26-001 vom 05.03.2026: Papier (ohne Beleg)"}
    n = len(Book(book.root).rows)
    api.receipt_book(Book(book.root), d["id"], "2026-03-05", "x", "45.50", "6500", zahlung=d["zahlung"])
    b = Book(book.root)
    assert len(b.rows) == n                                                     # nothing booked again
    assert any(p.name.startswith("26-001") for p in (b.root / "belege" / "2026").iterdir())
    assert_clean(b)


def test_cash_receipt_with_two_vat_rates(book):
    api.settings_update(book, mwst={"methode": "effektiv"})
    lines = ["Migros Genossenschaft", "Quittung", "Datum 07.03.2026", "Kaffee, Milch 2.6 %   12.80",
             "Putzmittel 8.1 %        8.65", "Total CHF 21.45", "Bar bezahlt", "Rückgeld 8.55"]
    pdf(book.root / "inbox" / "migros.pdf", lines)
    d = api.bill_draft_create(Book(book.root), "inbox/migros.pdf")["entwurf"]
    assert d["art"] == "quittung" and d["zahlung"]["konto"] == "1000"
    api.receipt_book(Book(book.root), d["id"], "2026-03-07", "Einkauf Migros", "21.45",
                     positionen=[{"konto": "5800", "betrag": "12.80", "mwst": "V26", "text": "Kaffee"},
                                 {"konto": "6000", "betrag": "8.65", "mwst": "I81", "text": "Putzmittel"}],
                     zahlung=d["zahlung"])
    b = Book(book.root)
    assert Decimal(api.ledger(b, "1000", 2026)["saldo"]) == Decimal("478.55")
    assert_clean(b)


def test_receipt_amount_must_match_the_bank(book, tmp_path):
    import_bank(book, tmp_path, "-45.50", "2026-03-06")
    pdf(book.root / "inbox" / "q.pdf", RECEIPT)
    d = api.bill_draft_create(Book(book.root), "inbox/q.pdf")["entwurf"]
    with pytest.raises(BookError, match="Bankbewegung"):
        api.receipt_book(Book(book.root), d["id"], "2026-03-05", "x", "45.00", "6500", zahlung=d["zahlung"])


def test_external_invoice_becomes_open_item_and_is_paid_by_bank(book, tmp_path):
    pdf(book.root / "inbox" / "rechnung-77.pdf", OWN_INVOICE)
    d = api.bill_draft_create(Book(book.root), "inbox/rechnung-77.pdf")["entwurf"]
    assert d["art"] == "debitor" and d["status"] == "bereit" and d["konto"]["wert"] == "3400"
    cust = api.customer_add(Book(book.root), name="Muster", firma="Kunde Muster AG", strasse="Gasse", nr="3",
                            plz="3011", ort="Bern")["kunde"]["nummer"]
    v = erfassung.form_values(d)
    out = api.invoice_external(Book(book.root), cust, v["betrag"], entwurf=d["id"], datum=v["datum"],
                               faellig=v["faellig"], rechnungsnr=v["rechnungsnr"])
    meta = out["rechnung"]
    assert meta["extern"] == {"rechnungsnr": "2026-77"} and meta["datei"].startswith("belege/")
    b = Book(book.root)
    assert invoices.invoice_fingerprint_ok(invoices.invoice(b, meta["nummer"]))
    assert Decimal(api.ledger(b, "1100", 2026)["saldo"]) == Decimal("1500.00")
    # the customer pays quoting their invoice number: matched automatically
    xml = statement(IBAN, "5000.00", [(entry("1500.00", "CRDT", "2026-03-20", party="KUNDE MUSTER AG",
                                             ustrd="Rechnung 2026-77"), "1500.00")], "2026-03-20", "2026-03-20", "S-P")
    f = tmp_path / "pay.xml"
    f.write_bytes(xml)
    assert api.bank_import(Book(book.root), str(f))["import_"]["gebucht"] == 1
    b = Book(book.root)
    assert invoices.invoice_state(b, invoices.invoice(b, meta["nummer"]))["status"] == "bezahlt"
    assert_clean(b)


def test_external_invoice_with_vat(book):
    api.settings_update(book, mwst={"methode": "effektiv"})
    cust = api.customer_add(Book(book.root), name="X", firma="X AG", strasse="a", nr="1", plz="3000",
                            ort="Bern")["kunde"]["nummer"]
    meta = api.invoice_external(Book(book.root), cust, "1081.00", datum="2026-03-02", mwst="U81",
                                rechnungsnr="A-1")["rechnung"]
    b = Book(book.root)
    assert Decimal(api.ledger(b, "3400", 2026)["saldo"]) == Decimal("-1000.00")
    assert Decimal(api.ledger(b, "2200", 2026)["saldo"]) == Decimal("-81.00")
    assert_clean(b)


def test_agent_reclassifies_and_sets_payment_account(book, monkeypatch):
    from allkvitt import tools
    pdf(book.root / "inbox" / "q.pdf", RECEIPT)
    d = api.bill_draft_create(Book(book.root), "inbox/q.pdf")["entwurf"]
    monkeypatch.setenv("ALLKVITT_BUCH", str(book.root))
    assert tools.complete_bill_draft(d["id"], "6500", "Büromaterial, privat bezahlt", zahlkonto="2800")["ok"]
    assert erfassung.draft(Book(book.root), d["id"])["zahlung"]["konto"] == "2800"
    assert tools.complete_bill_draft(d["id"], "6500", "doch offene Rechnung", art="kreditor")["ok"]
    assert erfassung.draft(Book(book.root), d["id"])["art"] == "kreditor"


def test_legacy_draft_folder_is_still_read(book):
    from allkvitt.files import write_yaml
    (book.root / "inbox" / "a.pdf").write_bytes(b"%PDF-1.4")
    write_yaml(book.root / "kreditoren" / "entwuerfe" / "ENT-0001.yaml",
               {"id": "ENT-0001", "datei": "inbox/a.pdf", "status": "unsicher", "felder": {}})
    assert erfassung.draft(book, "ENT-0001")["art"] == "kreditor"
    assert erfassung.next_id(book) == "ENT-0002"


def test_ui_receipt_and_external_invoice(book, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from allkvitt.web.app import create_app
    api.settings_update(book, kreditoren={"agent_automatisch": False})
    receipt = pdf(tmp_path / "quittung.pdf", RECEIPT).read_bytes()
    own = pdf(tmp_path / "eigene.pdf", OWN_INVOICE).read_bytes()
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/eingang/einlesen", headers=h, data={"art": ""},
                   files=[("dateien", ("quittung.pdf", receipt, "application/pdf")),
                          ("dateien", ("auszug.csv", b"a;b\n1;2\n", "text/csv"))])
        assert r.status_code == 204, r.text
        assert r.headers["HX-Redirect"] == "/kreditoren?status=entwurf"
        page = c.get("/kreditoren?status=entwurf").text
        assert 'id="entwuerfe"' in page and "ENT-0001" in page and "Quittung" in page
        review = c.get("/eingang/quittung?entwurf=ENT-0001").text
        assert 'value="45.50"' in review and "Bezahlt über Konto" in review
        r = c.post("/eingang/quittung", headers=h, data={"entwurf": "ENT-0001", "zahlung": "konto", "zahlkonto": "1000",
                                                          "datum": "2026-03-05", "betrag": "45.50", "text": "Papier",
                                                          "konto": "6500"})
        assert r.status_code == 204, r.text
        r = c.post("/eingang/einlesen", headers=h, data={"art": "debitor"},
                   files=[("dateien", ("eigene.pdf", own, "application/pdf"))])
        assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/debitoren")
        did = api.bill_drafts(Book(book.root))[0]["id"]
        assert did in c.get("/debitoren").text
        form = c.get(f"/debitoren/extern?entwurf={did}").text
        assert 'value="1500.00"' in form and "Kunde Muster AG" in form
        r = c.post("/debitoren/extern", headers=h, data={
            "entwurf": did, "kunde": "neu", "c_firma": "Kunde Muster AG", "c_name": "Muster",
            "c_strasse": "Gasse", "c_nr": "3", "c_plz": "3011", "c_ort": "Bern", "betrag": "1500.00",
            "rechnungsnr": "2026-77", "datum": "2026-03-02"})
        assert r.status_code == 204 and r.headers["HX-Redirect"] == "/debitoren?status=entwurf", r.text
        nummer = api.invoice_list(Book(book.root))[0]["nummer"]
        detail = c.get(f"/debitoren/rechnung/{nummer}").text
        assert "extern: 2026-77" in detail and "Original öffnen" in detail
    b = Book(book.root)
    assert api.bill_drafts(b) == [] and Decimal(api.ledger(b, "1000", 2026)["saldo"]) == Decimal("454.50")
    assert (b.root / "inbox" / "auszug.csv").exists()
    assert_clean(b)

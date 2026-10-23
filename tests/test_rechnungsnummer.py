"""Invoice numbers from a template (`rechnungsnummer: {vorlage, start}`), e.g. to continue a series from older software."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, check, invoices, kunden, reports
from aeradex.book import Book, BookError


def _book(tmp_path: Path, **nummer) -> Path:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    api.customer_add(Book(root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    if nummer:
        api.settings_update(Book(root), rechnungsnummer=nummer)
    return root


def inv(root, preis, datum):
    return api.invoice_create(Book(root), "K0001", [{"text": "Arbeit", "menge": 1, "preis": preis}], datum,
                              zahlungsfrist=30)["rechnung"]


def test_default_series_is_unchanged(tmp_path):
    root = _book(tmp_path)
    assert inv(root, "100", "2026-02-01")["nummer"] == "R-2026-0001"
    assert invoices.next_invoice_number(Book(root), 2027) == "R-2027-0001"


def test_template_continues_across_years_and_keeps_the_book_clean(tmp_path):
    root = _book(tmp_path, vorlage="INV-{n:04d}", start=250)
    a = inv(root, "1000", "2026-11-01")
    b = inv(root, "500", "2026-12-01")
    assert (a["nummer"], b["nummer"]) == ("INV-0250", "INV-0251")
    assert (root / "rechnungen" / "2026" / "INV-0250.md").exists()
    assert invoices.next_invoice_number(Book(root), 2027) == "INV-0252"           # no {jahr}: runs on
    assert a["referenz_typ"] == "SCOR" and a["referenz"].startswith("RF") and a["referenz"].endswith("INV0250")
    api.invoice_pay(Book(root), a["nummer"], None, "2026-11-20")
    api.invoice_credit(Book(root), b["nummer"], "100", "2026-12-10", grund="Skonto")
    b_ = Book(root)
    assert not [p for p in check.run(b_) if p.level == "fehler"]
    d = kunden.dossier(b_, "K0001", heute=date(2027, 1, 15))
    assert d["kennzahlen"]["umsatz_jahr"] == 0 and d["kennzahlen"]["umsatz_vorjahr"] == Decimal("1400.00")
    rep = reports.umsatz(b_, date(2026, 1, 1), date(2026, 12, 31))
    assert {r["label"]: r["werte"]["c0"] for r in rep["zeilen"]}["Kunde AG"] == Decimal("1400.00")


def test_external_invoices_take_the_original_numbers_in_order(tmp_path):
    root = _book(tmp_path, vorlage="INV-{n:04d}", start=247)
    got = [api.invoice_external(Book(root), "K0001", betrag, datum=d, rechnungsnr=f"INV-{n:04d}")["rechnung"]["nummer"]
           for n, betrag, d in ((247, "1680.01", "2026-08-14"), (248, "2439.99", "2026-10-01"))]
    assert got == ["INV-0247", "INV-0248"]
    assert inv(root, "300", "2026-10-23")["nummer"] == "INV-0249"


def test_template_with_year(tmp_path):
    root = _book(tmp_path, vorlage="{jahr}-{n:03d}")
    assert inv(root, "100", "2026-03-01")["nummer"] == "2026-001"
    assert invoices.next_invoice_number(Book(root), 2027) == "2027-001"


def test_bank_matches_and_assigns_templated_invoices(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from camt_sample import entry, statement
    root = _book(tmp_path, vorlage="INV-{n:04d}", start=250)
    a, b = inv(root, "200", "2026-03-01"), inv(root, "300", "2026-03-02")
    items = [(entry("200.00", "CRDT", "2026-03-20", ref=a["referenz"], party="Kunde AG"), "200.00"),
             (entry("300.00", "CRDT", "2026-03-21", party="Kunde AG", ustrd="Zahlung"), "300.00")]
    (root / "inbox" / "maerz.xml").write_bytes(statement("CH9300762011623852957", "0.00", items, "2026-03-01",
                                                         "2026-04-01"))
    api.bank_import(Book(root), "inbox/maerz.xml")
    status = {r["nummer"]: r["status"] for r in api.invoice_list(Book(root))}
    assert status[a["nummer"]] == "bezahlt"                                    # matched by its SCOR reference
    tx = next(t for t in api.bank_list(Book(root), "offen") if t["Betrag"] == "300.00")
    with pytest.raises(BookError, match="weder eine Rechnung"):
        api.bank_assign(Book(root), tx["ID"], "XYZ-1")
    api.bank_assign(Book(root), tx["ID"], b["nummer"])
    assert {r["nummer"]: r["status"] for r in api.invoice_list(Book(root))}[b["nummer"]] == "bezahlt"


@pytest.mark.parametrize("value, msg", [
    ({"vorlage": "INV-0001"}, "laufende Nummer"),
    ({"vorlage": "INV-{x}-{n}"}, "unbekanntes Feld"),
    ({"vorlage": "E-{n:04d}"}, "Kreditoren"),
    ({"vorlage": "INV {n}"}, "nur Buchstaben"),
    ({"vorlage": "INV-{n:04d}", "start": 0}, "Startnummer"),
])
def test_invalid_templates_are_refused(tmp_path, value, msg):
    root = _book(tmp_path)
    with pytest.raises(BookError, match=msg):
        api.settings_update(Book(root), rechnungsnummer=value)

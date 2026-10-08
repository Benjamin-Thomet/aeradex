"""Find and recode: another account (Soll or Haben) and/or MWST code for many Belege at once."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, check, kreditoren
from aeradex.book import Book, BookError

D = Decimal


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7", uid="CHE-123.456.789 MWST")
    api.settings_update(Book(root), mwst={"methode": "effektiv"})
    return Book(root)


def errors(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level != "hinweis"]


def rows_of(root: Path, beleg: str):
    return sorted(((r.soll, r.haben, r.betrag, r.mwst) for r in Book(root).rows if r.beleg == beleg))


def test_account_is_replaced_on_either_side_and_numbers_stay(book):
    a = api.post_entry(book, "2026-03-02", "6500", "1020", "120.00", "Büromaterial")["buchung"]["beleg"]
    b = api.post_entry(Book(book.root), "2026-03-09", "1020", "6500", "20.00", "Rückvergütung Papeterie")["buchung"]["beleg"]

    preview = api.recode_preview(Book(book.root), [a, b], "6500", "6570")
    assert preview["aenderbar"] == 2 and preview["uebersprungen"] == 0
    assert Book(book.root).rows[0].soll == "6500"            # the preview wrote nothing

    res = api.recode(Book(book.root), [a, b], "6500", "6570")
    assert res["umgebucht"] == [a, b]
    assert rows_of(book.root, a) == [("6570", "1020", D("120.00"), "")]
    assert rows_of(book.root, b) == [("1020", "6570", D("20.00"), "")]
    assert errors(book.root) == []


def test_new_mwst_code_splits_the_gross_anew(book):
    beleg = api.post_entry(book, "2026-03-02", "6500", "1020", "108.10", "Material", mwst="V81")["buchung"]["beleg"]
    api.recode(Book(book.root), [beleg], mwst_neu="V26")
    got = rows_of(book.root, beleg)
    net = [r for r in got if r[0] == "6500"][0]
    tax = [r for r in got if r[0] == "1170"][0]
    assert net[2] + tax[2] == D("108.10") and net[3] == tax[3] == "V26"
    assert tax[2] == (D("108.10") * D("2.6") / D("102.6")).quantize(D("0.01"))

    api.recode(Book(book.root), [beleg], mwst_neu="-")
    assert rows_of(book.root, beleg) == [("6500", "1020", D("108.10"), "")]
    assert errors(book.root) == []


def test_account_and_code_together_and_tax_account_is_explained(book):
    beleg = api.post_entry(book, "2026-03-02", "6500", "1020", "108.10", "Material", mwst="V81")["buchung"]["beleg"]
    preview = api.recode_preview(Book(book.root), [beleg], "1170", "1171")
    assert "MWST-Code" in preview["belege"][0]["problem"]
    api.recode(Book(book.root), [beleg], "6500", "6570", "V81")
    assert {r[0] for r in rows_of(book.root, beleg) if r[0]} == {"6570", "1170"}


def test_supplier_bill_changes_on_the_bill_and_its_rows(book):
    api.supplier_add(book, name="Hostpunkt GmbH", iban="CH7409000000300012345", konto="6500", mwst="V81")
    bill = api.bill_add(Book(book.root), "L0001", "108.10", datum="2026-04-01", rechnungsnr="H-1")["kreditor"]["nummer"]
    api.recode(Book(book.root), [bill], "6500", "6570")
    meta = kreditoren.bill(Book(book.root), bill)
    assert meta["konto"] == "6570" and kreditoren.bill_fingerprint_ok(meta)
    assert {r.soll for r in Book(book.root).rows if r.quelle == f"kreditor:{bill}" and r.soll} == {"6570", "1170"}
    assert errors(book.root) == []


def test_document_rows_locked_periods_and_strangers_are_skipped_with_reason(book):
    api.customer_add(book, name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    inv = api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "100"}], "2026-02-01")
    inv_beleg = inv["rechnung"]["nummer"]
    old = api.post_entry(Book(book.root), "2026-01-15", "6500", "1020", "10.00", "Jänner")["buchung"]["beleg"]
    other = api.post_entry(Book(book.root), "2026-05-15", "6000", "1020", "900.00", "Miete")["buchung"]["beleg"]
    api.lock(Book(book.root), "2026-01-31")
    keep = api.post_entry(Book(book.root), "2026-05-20", "6500", "1020", "33.00", "Toner")["buchung"]["beleg"]

    p = {b["beleg"]: b["problem"] for b in api.recode_preview(Book(book.root), [inv_beleg, old, other, keep],
                                                                "6500", "6570")["belege"]}
    assert p[keep] is None
    assert "dort ändern" in p[inv_beleg] and "gesperrt" in p[old].lower() and "kommt in" in p[other]
    res = api.recode(Book(book.root), [inv_beleg, old, other, keep], "6500", "6570")
    assert res["umgebucht"] == [keep] and len(res["uebersprungen"]) == 3
    with pytest.raises(BookError, match="Nichts umzubuchen"):
        api.recode(Book(book.root), [other], "6500", "6570")
    assert errors(book.root) == []


def test_parameters_are_checked(book):
    beleg = api.post_entry(book, "2026-03-02", "6500", "1020", "1.00", "x")["buchung"]["beleg"]
    with pytest.raises(BookError, match="Konto von"):
        api.recode_preview(Book(book.root), [beleg], "", "6570")
    with pytest.raises(BookError, match="existiert nicht"):
        api.recode_preview(Book(book.root), [beleg], "6500", "9999x")
    with pytest.raises(BookError, match="Neues Konto oder"):
        api.recode_preview(Book(book.root), [beleg])

"""Editing a manual Beleg in place: same number, receipt stays, previous version in the git history;
refused where a correction must be a Storno (locked period, booked MWST Abrechnung, document rows,
a reconciled bank amount)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, gitlog, journal
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path: Path) -> Book:
    return make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, iban=IBAN, git=True,
                     strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")


def rows(book: Book, beleg: str):
    return [r for r in Book(book.root).rows if r.beleg == beleg]


def test_simple_booking_is_corrected_under_the_same_number(book):
    api.post_entry(book, "2026-03-05", "6500", "1020", "45.80", "Büromaterial")
    receipt = book.root / "belege" / "2026" / "26-001 quittung.pdf"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_bytes(b"%PDF-1.4")
    api.amend_entry(Book(book.root), "26-001", "2026-03-06", "Druckerpatronen",
                    [{"soll": "6510", "haben": "1020", "betrag": "54.80"}])
    (r,) = rows(book, "26-001")
    assert (r.datum.isoformat(), r.text, r.soll, r.haben, r.betrag) == ("2026-03-06", "Druckerpatronen", "6510", "1020", Decimal("54.80"))
    assert receipt.exists()
    assert_clean(Book(book.root))
    msg = gitlog.log(book.root, 1)[0]["nachricht"]
    assert "26-001 geändert" in msg and "vorher" in msg and "45.80" in msg     # traceable in the Verlauf
    assert journal.next_beleg(Book(book.root), 2026) == "26-002"              # no number used up


def test_mwst_booking_edits_as_one_gross_line(book):
    api.settings_update(Book(book.root), mwst={"methode": "effektiv"})
    api.post_entry(Book(book.root), "2026-03-05", "4000", "1020", "108.10", "Material", mwst="V81")
    b = Book(book.root)
    assert len(rows(book, "26-001")) == 3
    lines = journal.edit_lines(b, rows(book, "26-001"))
    assert lines == [{"soll": "4000", "haben": "1020", "betrag": Decimal("108.10"), "mwst": "V81", "text": ""}]
    api.amend_entry(b, "26-001", "2026-03-05", "Material", [dict(lines[0], betrag="216.20")])
    assert sum(r.betrag for r in rows(book, "26-001") if r.haben == "1020") == Decimal("216.20")
    assert_clean(Book(book.root))


def test_split_booking_keeps_its_lines(book):
    api.post_split(book, "2026-03-05", "Versicherungen", [
        {"soll": "6300", "betrag": "300"}, {"soll": "6400", "betrag": "200"}, {"haben": "1020", "betrag": "500"}])
    lines = journal.edit_lines(Book(book.root), rows(book, "26-001"))
    assert len(lines) == 3
    lines[1]["betrag"], lines[2]["betrag"] = "250", "550"
    api.amend_entry(Book(book.root), "26-001", "2026-03-05", "Versicherungen", lines)
    assert sorted(r.betrag for r in rows(book, "26-001")) == [Decimal("250"), Decimal("300"), Decimal("550")]
    with pytest.raises(BookError, match="nicht ausgeglichen"):
        api.amend_entry(Book(book.root), "26-001", "2026-03-05", "x", [dict(lines[0], betrag="1")] + lines[1:])
    assert_clean(Book(book.root))


def test_edit_refused_where_a_storno_is_required(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "10", "alt")
    api.post_entry(Book(book.root), "2026-03-05", "6500", "1020", "20", "neu")
    api.lock(Book(book.root), "2026-01-31")
    line = [{"soll": "6500", "haben": "1020", "betrag": "11"}]
    with pytest.raises(BookError, match="gesperrt"):
        api.amend_entry(Book(book.root), "26-001", "2026-01-05", "alt", line)
    with pytest.raises(BookError, match="gesperrt"):                      # nor moved into the locked period
        api.amend_entry(Book(book.root), "26-002", "2026-01-20", "neu", line)
    with pytest.raises(BookError, match="Jahr 2026"):
        api.amend_entry(Book(book.root), "26-002", "2027-01-05", "neu", line)
    api.customer_add(Book(book.root), name="Peter Privat", strasse="Seeweg", nr="1", plz="3600", ort="Thun")
    inv = api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 1, "preis": "450"}], "2026-03-10")
    beleg = next(r.beleg for r in Book(book.root).rows if r.quelle.startswith("rechnung:"))
    with pytest.raises(BookError, match="dort ändern"):
        api.amend_entry(Book(book.root), beleg, "2026-03-10", "x", line)
    assert inv


def test_booked_mwst_period_is_refused(book):
    api.settings_update(Book(book.root), mwst={"methode": "effektiv"})
    api.post_entry(Book(book.root), "2026-02-05", "4000", "1020", "108.10", "Material", mwst="V81")
    api.mwst_book(Book(book.root), "2026-Q1")
    with pytest.raises(BookError, match="MWST-Abrechnung 2026-Q1"):
        api.amend_entry(Book(book.root), "26-001", "2026-02-05", "Material",
                        [{"soll": "4000", "haben": "1020", "betrag": "50", "mwst": "V81"}])


def test_reconciled_bank_amount_must_stay(book, tmp_path):
    api.post_entry(book, "2026-02-03", "6000", "1020", "1800", "Miete Februar")
    f = tmp_path / "feb.xml"
    f.write_bytes(statement(IBAN, "20000.00", [(entry("1800.00", "DBIT", "2026-02-03"), "-1800.00")],
                            "2026-02-01", "2026-02-28", stmt_id="FEB"))
    api.bank_import(Book(book.root), str(f))
    with pytest.raises(BookError, match="Bankbewegung"):
        api.amend_entry(Book(book.root), "26-001", "2026-02-03", "Miete", [{"soll": "6000", "haben": "1020", "betrag": "1900"}])
    api.amend_entry(Book(book.root), "26-001", "2026-02-03", "Miete Büro Februar",   # account and text may change
                    [{"soll": "6100", "haben": "1020", "betrag": "1800"}])
    assert rows(book, "26-001")[0].soll == "6100"
    assert_clean(Book(book.root))

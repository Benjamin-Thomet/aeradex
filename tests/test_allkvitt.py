"""End-to-end tests against throwaway books in a temp directory."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from allkvitt import api, check, journal, payroll, qrbill_ch as qr, qst, statements
from allkvitt.book import Book, BookError
from allkvitt.files import parse_table, render_table


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern",
                  iban="CH93 0076 2011 6238 5295 7")
    text = (root / "kontenplan.yaml").read_text()
    text = text.replace('{nr: "1020", name: Bank, klasse: aktiv}',
                        '{nr: "1020", name: Bank, klasse: aktiv, eroeffnung: 20000}')
    text = text.replace('{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv}',
                        '{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv, eroeffnung: -20000}')
    (root / "kontenplan.yaml").write_text(text)
    return Book(root)


def errors(book: Book) -> list[str]:
    book.reload()
    return [str(i) for i in check.run(book) if i.level == "fehler"]


# ---------- storage ----------

def test_markdown_table_roundtrip():
    text = "# Titel\n\n| A | B |\n| --- | ---: |\n| x \\| y | 1.00 |\n\nNotiz\n"
    t = parse_table(text)
    assert t.rows == [{"A": "x | y", "B": "1.00"}]
    assert parse_table(render_table(t)).rows == t.rows
    assert "Notiz" in render_table(t) and t.align == {"B": "right"}


# ---------- journal ----------

def test_booking_and_balance(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "45.80", "Büromaterial")
    tb = {r["konto"]: r for r in api.balances(Book(book.root), 2026)["konten"]}
    assert tb["1020"]["saldo"] == "19954.20"
    assert tb["6500"]["saldo"] == "45.80"
    assert errors(book) == []


def test_booking_refuses_bad_input(book):
    with pytest.raises(BookError):
        api.post_entry(book, "2026-01-05", "9999", "1020", "10", "kein Konto")
    with pytest.raises(BookError):
        api.post_entry(book, "2026-01-05", "6500", "1020", "-10", "negativ")
    with pytest.raises(BookError):
        api.post_entry(book, "2026-01-05", "6500", "1020", "10.001", "Rappenbruchteil")


def test_split_must_balance(book):
    with pytest.raises(BookError):
        api.post_split(book, "2026-02-01", "Einkauf", [{"soll": "6500", "betrag": "30"}, {"haben": "1020", "betrag": "20"}])
    api.post_split(book, "2026-02-01", "Einkauf", [{"soll": "6500", "betrag": "30"}, {"soll": "6641", "betrag": "20"},
                                                   {"haben": "1020", "betrag": "50"}])
    assert errors(book) == []


def test_beleg_numbers_are_sequential_and_never_reused(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "1", "a")
    api.post_entry(book, "2026-01-06", "6500", "1020", "1", "b")
    book.reload()
    assert journal.next_beleg(book, 2026) == "26-003"


def test_reverse_creates_counter_entry(book):
    api.post_entry(book, "2026-01-05", "6000", "1020", "1500", "Miete")
    api.reverse_entry(Book(book.root), "26-001", "2026-01-06")
    tb = {r["konto"]: r for r in api.balances(Book(book.root), 2026)["konten"]}
    assert tb["6000"]["saldo"] == "0.00"


def test_proposals_flow(book):
    api.propose(book, "2026-03-01", "6570", "1020", "29.90", "Abo", "IT")
    assert len(api.proposals(Book(book.root))) == 1
    api.approve(Book(book.root), ["V-001"])
    b = Book(book.root)
    assert api.proposals(b) == [] and len(b.rows) == 1


# ---------- check & lock ----------

def test_lock_detects_tampering_and_refuses_postings(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "45.80", "Büromaterial")
    api.lock(Book(book.root), "2026-01-31")
    with pytest.raises(BookError):
        api.post_entry(Book(book.root), "2026-01-20", "6500", "1020", "1", "zu spät")
    path = book.root / "journal" / "2026" / "2026-01.md"
    path.write_text(path.read_text().replace("45.80", "55.80"))
    assert any("gesperrte Periode" in e for e in errors(book))


def test_duplicate_row_is_an_error(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "10", "x")
    path = book.root / "journal" / "2026" / "2026-01.md"
    line = [l for l in path.read_text().splitlines() if "26-001" in l][0]
    path.write_text(path.read_text() + line + "\n")
    assert any("doppelt" in e for e in errors(book))


def test_row_in_wrong_month_file(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "10", "x")
    src = book.root / "journal" / "2026" / "2026-01.md"
    src.rename(book.root / "journal" / "2026" / "2026-02.md")
    assert any("gehört in" in e for e in errors(book))


# ---------- invoices ----------

def test_invoice_lifecycle(book):
    api.customer_add(book, name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    res = api.invoice_create(Book(book.root), "K0001", [{"text": "Beratung", "menge": 10, "preis": "150"},
                                                         {"text": "Spesen", "menge": 1, "preis": "80", "konto": "3600"}],
                             "2026-02-10")
    nr = res["rechnung"]["nummer"]
    assert nr == "R-2026-0001" and res["rechnung"]["referenz"].startswith("RF")
    assert (book.root / res["pdf"]).exists()
    b = Book(book.root)
    assert api.invoice_match(b, "1580", "Zahlung")["treffer"]["nummer"] == nr
    api.invoice_pay(b, nr, "1000", "2026-03-01")
    with pytest.raises(BookError):
        api.invoice_pay(Book(book.root), nr, "600", "2026-03-02")   # exceeds open 580
    with pytest.raises(BookError):
        api.invoice_void(Book(book.root), nr)                       # has a payment
    api.invoice_credit(Book(book.root), nr, None, "2026-03-05", grund="Kulanz")
    assert api.invoice_list(Book(book.root))[0]["status"] == "bezahlt"
    ar = api.receivables(Book(book.root), "2026-03-31")
    assert ar["total_offen"] == "0.00" and ar["differenz"] == "0.00"
    assert errors(book) == []


def test_issued_invoice_is_frozen(book):
    api.customer_add(book, name="Anna", plz="3011", ort="Bern", strasse="Gasse", nr="1")
    api.invoice_create(Book(book.root), "K0001", [{"text": "X", "menge": 1, "preis": "100"}], "2026-02-10")
    path = book.root / "rechnungen" / "2026" / "R-2026-0001.md"
    path.write_text(path.read_text().replace("preis: 100.00", "preis: 90.00"))
    assert any("Fingerprint" in e for e in errors(book))


def test_void_removes_booking(book):
    api.customer_add(book, name="Anna")
    api.invoice_create(Book(book.root), "K0001", [{"text": "X", "menge": 1, "preis": "100"}], "2026-02-10")
    api.invoice_void(Book(book.root), "R-2026-0001", "Fehler")
    b = Book(book.root)
    assert b.rows == [] and errors(b) == []


# ---------- QR bill ----------

def test_qr_references():
    assert qr.mod10r_check_digit("21000000000313947143000901") == 7
    assert qr.make_scor_reference("539007547034") == "RF18539007547034"   # ISO 11649 example
    assert qr.make_scor_reference("INV-0247") == "RF91INV0247"
    assert len(qr.make_qrr_reference("R-2026-0001")) == 27
    assert qr.iban_problem("CH9300762011623852957") is None
    assert qr.iban_problem("CH9300762011623852958") is not None
    assert qr.is_qr_iban("CH4431999123000889012")


# ---------- payroll ----------

def test_payroll_monthly_prorated_with_qst(book):
    api.employee_add(book, "Lea", "Muster", eintritt="2026-01-15", monatslohn=6000, pensum=80, bvg_betrag=250,
                     qst={"kanton": "BS", "jahr": 2026, "code": "A0N"})
    api.payroll_run(Book(book.root), "2026-01", "M0001", {"qst_gesamtpensum": 80})
    w = api.payslip_show(Book(book.root), "2026-01", "M0001")["werte"]
    assert w["bruttolohn"] == "2632.26"                     # 4800 × 17/31
    assert w["qst_satzbestimmend"] == "2632.26"            # Brutto / 80 × 80
    assert Decimal(w["quellensteuer"]) == (Decimal("2632.26") * qst.rate("BS", 2026, "A0N", Decimal("2632.26"))).quantize(Decimal("0.01"))
    api.payslip_close(Book(book.root), "2026-01", "M0001")
    b = Book(book.root)
    assert errors(b) == []
    assert all(r.quelle == "lohn:2026-01:M0001" for r in b.rows)


def test_payroll_hourly_ferien_included():
    emp = {"lohnart": "stunde", "stundenlohn": 35, "standard_stunden": 1, "ferienzuschlag_satz": "0.0833",
           "ferien_inbegriffen": True}
    w = payroll.calculate(emp, payroll.config.__globals__["DEFAULT_CONFIG"], 2026, 1, {})
    assert w["bruttolohn"] == Decimal("35.00")
    assert w["grundlohn"] == Decimal("32.31") and w["ferienzuschlag"] == Decimal("2.69")


def test_satzbestimmend_ks45_example():
    # KS 45 Ziff. 6.4: CHF 4'500 at 50 % own and 90 % total → 8'100.
    assert payroll.satzbestimmendes_einkommen(4500, 50, 90) == Decimal("8100.00")


def test_closed_payslip_tamper_detected(book):
    api.employee_add(book, "Tom", "Test", monatslohn=5000)
    api.payroll_run(Book(book.root), "2026-02")
    api.payslip_close(Book(book.root), "2026-02", "M0001")
    path = book.root / "lohn" / "2026" / "02" / "M0001.md"
    text = path.read_text()
    path.write_text(text.replace("bruttolohn: 5000.00", "bruttolohn: 6000.00"))
    assert any("Fingerprint" in e for e in errors(book))


def test_reopen_removes_booking(book):
    api.employee_add(book, "Tom", "Test", monatslohn=5000)
    api.payroll_run(Book(book.root), "2026-02")
    api.payslip_close(Book(book.root), "2026-02", "M0001")
    api.payslip_reopen(Book(book.root), "2026-02", "M0001")
    b = Book(book.root)
    assert b.rows == [] and errors(b) == []


def test_lohnausweis(book):
    api.employee_add(book, "Tom", "Test", monatslohn=5000, ahv_nr="756.1234.5678.97", geburtsdatum="1990-01-01")
    for m in ("01", "02"):
        api.payroll_run(Book(book.root), f"2026-{m}")
        api.payslip_close(Book(book.root), f"2026-{m}", "M0001")
    res = api.lohnausweis_create(Book(book.root), 2026, "M0001")
    assert res["ziffern"]["z1"] == 10000
    assert (book.root / res["pdf"]).read_bytes().startswith(b"%PDF")


# ---------- statements & year chain ----------

def test_statement_balances_and_year_chain(book):
    api.post_entry(book, "2026-03-01", "1020", "3400", "10000", "Umsatz")
    api.post_entry(Book(book.root), "2026-03-02", "6000", "1020", "4000", "Miete")
    st = api.statement(Book(book.root), 2026)
    assert st["differenz"] == "0.00" and st["jahresergebnis"] == "6000.00"
    api.post_entry(Book(book.root), "2027-01-10", "6500", "1020", "100", "Folgejahr")
    b = Book(book.root)
    st27 = statements.year_end_statement(b, 2027)
    vortrag = [r for r in st27["passiven"] if r["label"].startswith("Gewinnvortrag")][0]
    assert vortrag["aktuell"] == Decimal("6000.00")        # last year's result folded in
    assert st27["differenz"] == 0
    api.allocation_set(b, 2026, dividende="1000", reserve="300")
    api.allocation_book(Book(book.root), 2026, "2027-05-20")
    alloc = statements.profit_allocation(Book(book.root), 2026)
    assert alloc["gebucht"] and errors(book) == []


def test_git_commit_per_write(book):
    api.post_entry(book, "2026-01-05", "6500", "1020", "45.80", "Büromaterial")
    log = api.history(Book(book.root))
    assert log[0]["nachricht"].startswith("allkvitt: Beleg 26-001 gebucht")


def test_proposal_carries_receipt_to_belege(book):
    receipt = book.root / "inbox" / "quittung.txt"
    receipt.write_text("Papeterie, CHF 32.50, bar")
    api.propose(book, "2026-03-12", "6500", "1000", "32.50", "Papier", "bar bezahlt", datei="inbox/quittung.txt")
    api.approve(Book(book.root), ["V-001"])
    b = Book(book.root)
    assert not receipt.exists()
    assert journal.receipts_for(b, "26-001", 2026)[0].name == "26-001 quittung.txt"
    assert len(b.rows) == 1 and errors(b) == []

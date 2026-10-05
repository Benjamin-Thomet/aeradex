"""Statements in any format: learned CSV formats and credit card statements read by the agent."""
from decimal import Decimal
from pathlib import Path

import pytest

from batzen import api, bank, bankformat, tools
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book

# A UBS-like export: preamble with the IBAN, ';', Swiss apostrophes, debit/credit columns, newest first.
UBS = """Kontonummer:;0235-12345678.01
IBAN:;CH93 0076 2011 6238 5295 7
Bewertet in:;CHF
Abschlussdatum;Buchungstext;Beschreibung 2;Belastung;Gutschrift;Saldo
31.03.2026;Swisscom (Schweiz) AG;Rechnung März;89.00;;10'811.00
15.03.2026;Kunde AG;Zahlung R-2026-0001;;1'500.00;10'900.00
01.03.2026;Vermieter;Miete März;1'600.00;;9'400.00
;;Saldo per 31.03.2026;;;10'811.00
"""

SPEC = dict(kopfzeile=["Abschlussdatum", "Buchungstext", "Beschreibung 2", "Belastung", "Gutschrift", "Saldo"],
            datum="Abschlussdatum", datumsformat="%d.%m.%Y", belastung="Belastung", gutschrift="Gutschrift",
            text=["Buchungstext", "Beschreibung 2"], saldo="Saldo")


@pytest.fixture
def book(tmp_path):
    return make_book(tmp_path, iban="CH93 0076 2011 6238 5295 7", eroeffnung={"1020": 11000, "2800": -11000})


def _inbox(book: Book, name: str, data) -> str:
    path = book.root / "inbox" / name
    path.write_bytes(data.encode("cp1252") if isinstance(data, str) else data)
    return f"inbox/{name}"


def test_numbers_and_dates():
    assert bankformat.number("1'234.50") == Decimal("1234.50")
    assert bankformat.number("-1.234,50", ",") == Decimal("-1234.50")
    assert bankformat.number("(12.00)") == Decimal("-12.00")
    assert bankformat.number("12.00-") == Decimal("-12.00")
    assert bankformat.number("CHF 5.00") == Decimal("5.00")
    assert bankformat.number("") is None
    assert str(bankformat.to_date("31.03.2026 12:00", "%d.%m.%Y")) == "2026-03-31"


def test_unknown_csv_points_to_learning(book):
    datei = _inbox(book, "ubs.csv", UBS)
    with pytest.raises(BookError, match="bank format lernen"):
        api.bank_import(book, datei)


def test_learned_format_import(book):
    datei = _inbox(book, "ubs.csv", UBS)
    res = api.bank_format_propose(book, datei, "UBS Kontoauszug", **SPEC)
    assert res["pruefung"]["ok"], res["pruefung"]
    stmt = res["auszuege"][0]
    assert stmt["konto"] == "1020" and stmt["bewegungen"] == 3            # IBAN from the preamble
    assert stmt["eroeffnung"] == "11000.00" and stmt["schluss"] == "10811.00"
    assert [b["betrag"] for b in stmt["beispiele"]] == ["-1600.00", "1500.00", "-89.00"]

    with pytest.raises(BookError, match="bank format lernen"):           # not confirmed yet
        api.bank_import(Book(book.root), datei)
    api.bank_format_confirm(Book(book.root), res["format"])
    out = api.bank_import(Book(book.root), datei)
    assert out["import_"]["neu"] == 3 and out["import_"]["offen"] == 3

    # an overlapping export later: known movements are not imported twice
    later = UBS.replace("31.03.2026;Swisscom", "02.04.2026;Bäckerei;Znüni;12.50;;10'798.50\n31.03.2026;Swisscom")
    out = api.bank_import(Book(book.root), _inbox(book, "ubs-april.csv", later))
    assert out["import_"]["neu"] == 1 and out["import_"]["doppelt"] == 3
    assert_clean(Book(book.root))
    rec = {r["id"]: r for r in bank.reconciliation(Book(book.root))}
    assert all(r["differenz"] == 0 for r in rec.values())                # open movements explain the gap


def test_wrong_sign_fails_the_balance_check(book):
    datei = _inbox(book, "ubs.csv", UBS)
    spec = {**SPEC, "belastung": "Gutschrift", "gutschrift": "Belastung"}
    res = api.bank_format_propose(book, datei, "UBS falsch", **spec)
    assert not res["pruefung"]["ok"] and res["pruefung"]["saldo_fehler"]


def test_format_needs_an_account_without_iban(book):
    datei = _inbox(book, "export.csv", "Datum,Text,Betrag\n2026-03-01,Kaffee,-4.50\n")
    with pytest.raises(BookError, match="konto"):
        api.bank_format_propose(book, datei, "Neobank", kopfzeile=["Datum", "Text", "Betrag"], datum="Datum",
                                datumsformat="%Y-%m-%d", betrag="Betrag", text=["Text"])
    res = api.bank_format_propose(Book(book.root), datei, "Neobank", kopfzeile=["Datum", "Text", "Betrag"],
                                  datum="Datum", datumsformat="%Y-%m-%d", betrag="Betrag", text=["Text"], konto="1020")
    assert res["auszuege"][0]["beispiele"][0]["betrag"] == "-4.50"


def test_learning_through_the_agent(book, monkeypatch):
    datei = _inbox(book, "ubs.csv", UBS)

    def agent(root, prompt):                       # what the agent does: preview, then describe
        token = tools.BOOK_ROOT.set(root)
        try:
            assert "Abschlussdatum" in tools.bank_file_preview(datei)["zeilen"]
            assert tools.propose_bank_format(datei, "UBS", **SPEC)["pruefung"]["ok"]
        finally:
            tools.BOOK_ROOT.reset(token)
        return "Format beschrieben"
    monkeypatch.setattr(bankformat, "run_agent", agent)
    res = api.bank_format_learn(book, datei)
    assert res["format"] == "ubs" and not res["bestaetigt"] and res["pruefung"]["ok"]
    assert [f["format"] for f in api.bank_format_list(Book(book.root))] == ["ubs"]


# ---------- credit card ----------

CARD_LINES = [("2026-08-22", "Migros Bern", "45.60", ""), ("2026-08-25", "Hotel Bellevue Wien", "180.35", "EUR 192.00"),
              ("2026-08-25", "Bearbeitungsgebühr Fremdwährung", "3.15", ""), ("2026-09-02", "Ihre Zahlung - Danke", "-500.00", "")]


def _card_pdf(path: Path, lines=CARD_LINES, alt="500.00", neu="229.10"):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    y = 800
    for text in ["Viseca Card Services · Monatsrechnung 20.09.2026", f"Saldo letzte Rechnung CHF {alt}"] + \
            [f"{d[8:]}.{d[5:7]}.{d[:4]}  {t}  {o}  {a.replace('-', '')}{' -' if a.startswith('-') else ''}"
             for d, t, a, o in lines] + [f"Neuer Saldo zu unseren Gunsten CHF {neu}"]:
        c.drawString(40, y, text)
        y -= 18
    c.save()


def _card_tx(lines=CARD_LINES):
    return [{"datum": d, "text": t, "betrag": a, **({"original": o} if o else {})} for d, t, a, o in lines]


def test_card_statement(book, monkeypatch):
    api.add_account(book, "2040", "Kreditkarte Visa", "passiv")
    book = Book(book.root)
    # last month: the card balance (500) is owed; this month the bank paid it
    book.accounts["2040"].eroeffnung = Decimal("-500")
    book.accounts["1020"].eroeffnung = Decimal("11500")
    book.save_accounts()
    api.post_entry(Book(book.root), "2026-09-02", "2040", "1020", "500", "LSV Viseca")
    pdf = book.root / "inbox" / "visa-2026-09.pdf"
    _card_pdf(pdf)
    datei = "inbox/visa-2026-09.pdf"

    def agent(root, prompt):
        token = tools.BOOK_ROOT.set(root)
        try:
            assert "Migros" in tools.card_statement_text(datei)["text"]
            res = tools.propose_card_statement(datei, "2040", "500.00", "229.10", _card_tx(), "Viseca", "1234",
                                               "2026-08-21", "2026-09-20")
            assert res["pruefung"]["ok"], res
        finally:
            tools.BOOK_ROOT.reset(token)
        return "ok"
    monkeypatch.setattr(bankformat, "run_agent", agent)
    out = api.card_statement_read(Book(book.root), datei, "2040")
    imp = out["import"]["import_"]
    assert imp["neu"] == 4 and imp["abgeglichen"] == 1 and imp["offen"] == 3      # payment matched the LSV booking
    assert not pdf.exists()                                                      # filed under bank/auszuege

    rows = {t["Text"]: t for t in bank.transactions(Book(book.root))}
    hotel = rows["Hotel Bellevue Wien (EUR 192.00)"]
    assert hotel["Konto"] == "2040" and hotel["Betrag"] == "-180.35"
    api.bank_book(Book(book.root), hotel["ID"], "6640")                           # Reisespesen an Kreditkarte
    row = [r for r in Book(book.root).rows if r.text.startswith("Hotel")][0]
    assert (row.soll, row.haben) == ("6640", "2040")
    for t in rows.values():
        if t["Status"] == "offen" and t["ID"] != hotel["ID"]:
            api.bank_book(Book(book.root), t["ID"], "6940" if "Gebühr" in t["Text"] else "6500")
    assert_clean(Book(book.root))
    rec = [r for r in bank.reconciliation(Book(book.root)) if r["konto"] == "2040"]
    assert rec and rec[0]["bank"] == Decimal("-229.10") and rec[0]["differenz"] == 0


def test_card_checks_catch_misread_amounts(book):
    api.add_account(book, "2040", "Kreditkarte", "passiv")
    pdf = book.root / "inbox" / "karte.pdf"
    _card_pdf(pdf)
    wrong = _card_tx([*CARD_LINES[:1], ("2026-08-25", "Hotel Bellevue Wien", "180.53", ""), *CARD_LINES[2:]])
    res = api.card_statement_propose(Book(book.root), "inbox/karte.pdf", "2040", "500.00", "229.10", wrong)
    assert not res["pruefung"]["ok"]
    assert any("Saldo geht nicht auf" in p for p in res["pruefung"]["probleme"])
    assert any("180.53" in p for p in res["pruefung"]["probleme"])
    with pytest.raises(BookError, match="nicht geprüft"):
        api.bank_import(Book(book.root), "inbox/karte.pdf")


def test_card_needs_a_liability_account(book):
    pdf = book.root / "inbox" / "karte.pdf"
    _card_pdf(pdf)
    with pytest.raises(BookError, match="Passivkonto"):
        api.card_statement_propose(book, "inbox/karte.pdf", "1020", "500.00", "229.10", _card_tx())


def test_bank_page_learn_confirm_import(book, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from batzen.web.app import create_app

    def agent(root, prompt):
        datei = prompt.split("Die Datei ", 1)[1].split(" ", 1)[0]
        token = tools.BOOK_ROOT.set(root)
        try:
            tools.propose_bank_format(datei, "UBS", **SPEC)
        finally:
            tools.BOOK_ROOT.reset(token)
        return "ok"
    monkeypatch.setattr(bankformat, "run_agent", agent)
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        headers = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/bank/import", files={"datei": ("ubs.csv", UBS.encode("cp1252"), "text/csv")}, headers=headers)
        assert r.headers.get("HX-Redirect") == "/bank#neu", r.text
        page = c.get("/bank").text
        assert "Bestätigen und importieren" in page and "Saldo Zeile für Zeile geprüft" in page
        r = c.post("/bank/format/ubs/bestaetigen", data={"datei": "inbox/ubs.csv"}, headers=headers)
        assert r.headers.get("HX-Redirect") == "/bank", r.text
        assert "Bestätigen und importieren" not in c.get("/bank").text
    assert len(bank.transactions(Book(book.root))) == 3

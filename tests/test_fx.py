from datetime import date
from decimal import Decimal
from pathlib import Path

from batzen import api, fx
from batzen.book import Book

SAMPLE = b'''<?xml version="1.0"?><wechselkurse xmlns="https://www.backend-rates.bazg.admin.ch/xmldaily">
<datum>02.10.2026</datum><devise code="eur"><waehrung>1 EUR</waehrung><kurs>0.94445</kurs></devise>
<devise code="jpy"><waehrung>100 JPY</waehrung><kurs>0.56</kurs></devise></wechselkurse>'''


def test_rates_parse_cache_and_weekend_fallback(tmp_path: Path):
    api.init_book(tmp_path / "b", "X GmbH", 2026, git=False)
    b = Book(tmp_path / "b")
    calls = []
    def fetch(url):
        calls.append(url)
        return SAMPLE if "20261002" in url else b"<wechselkurse/>"
    assert fx.rate(b, "EUR", date(2026, 10, 4), fetch) == Decimal("0.94445")   # Sunday -> Friday
    assert fx.rate(b, "JPY", date(2026, 10, 2), fetch) == Decimal("0.0056")
    n = len(calls)
    fx.rate(b, "EUR", date(2026, 10, 2), fetch)
    assert len(calls) == n                                                       # cached


# ---------- foreign-currency accounts and revaluation ----------

import pytest

from batzen import check
from batzen.book import BookError

DAILY = {"20260302": "0.95", "20260415": "0.96", "20260630": "0.92", "20251231": "0.93"}


def bazg(url):
    d = url.split("d=")[1][:8]
    if d not in DAILY:
        return b"<wechselkurse/>"
    return (f'<wechselkurse><datum>x</datum><devise code="eur"><waehrung>1 EUR</waehrung>'
            f'<kurs>{DAILY[d]}</kurs></devise></wechselkurse>').encode()


@pytest.fixture
def fwbook(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fx, "_get", bazg)
    import batzen.fx as fxmod
    real_rates = fxmod.rates
    monkeypatch.setattr(fxmod, "rates", lambda book, day, fetch=None: real_rates(book, day, fetch or bazg))
    root = tmp_path / "b"
    api.init_book(root, "X GmbH", 2026, git=False)
    api.add_account(Book(root), "1021", "Bank EUR", "aktiv", waehrung="EUR")
    return root


def errors(root):
    return [str(i) for i in check.run(Book(root)) if i.level == "fehler"]


def test_foreign_booking_uses_bazg_rate_and_keeps_fw(fwbook):
    out = api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "1000", "Verkauf DE", waehrung="EUR")
    row = out["buchung"]
    assert row["betrag"] == "950.00" and row["fw"] == "1000.00" and row["waehrung"] == "EUR"
    api.post_entry(Book(fwbook), "2026-04-15", "6500", "1021", "100", "Material", waehrung="EUR", kurs="0.9612")
    led = api.ledger(Book(fwbook), "1021", 2026)
    assert led["saldo_fw"] == "900.00" and led["saldo"] == "853.88"         # 950 − 96.12
    text = (fwbook / "journal" / "2026" / "2026-03.md").read_text()
    assert "EUR 1000.00" in text and "| 0.95" in text
    assert errors(fwbook) == []


def test_foreign_account_refuses_chf_and_other_currency(fwbook):
    with pytest.raises(BookError, match="Währung"):
        api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "100", "ohne Währung")
    with pytest.raises(BookError, match="EUR"):
        api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "100", "USD", waehrung="USD", kurs="0.8")
    with pytest.raises(BookError, match="Bilanzkonten"):
        api.add_account(Book(fwbook), "6599", "Aufwand EUR", "aufwand", waehrung="EUR")


def test_mwst_split_in_foreign_currency_balances(fwbook):
    api.settings_update(Book(fwbook), mwst={"methode": "effektiv"})
    api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "1000", "Verkauf mit MWST", mwst="U81", waehrung="EUR")
    assert errors(fwbook) == []
    rows = [r for r in Book(fwbook).rows if r.beleg == "26-001"]
    assert sum(r.betrag for r in rows if r.soll) == sum(r.betrag for r in rows if r.haben)
    assert next(r for r in rows if r.soll == "1021").betrag == Decimal("950.00")


def test_proposal_fixes_rate_and_approval_books_it(fwbook):
    api.propose(Book(fwbook), "2026-03-02", "6500", "1021", "50", "Vorschlag EUR", waehrung="EUR")
    p = api.proposals(Book(fwbook))[0]
    assert p["FW"] == "EUR" and p["Kurs"] == "0.95"
    api.approve(Book(fwbook), [p["ID"]])
    row = Book(fwbook).rows[-1]
    assert row.betrag == Decimal("47.50") and row.fw == Decimal("50.00")


def test_revaluation_books_loss_and_check_guards_it(fwbook):
    api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "1000", "Verkauf DE", waehrung="EUR")
    prev = api.fx_preview(Book(fwbook), "2026-06-30")
    assert prev["konten"][0]["chf_neu"] == "920.00" and prev["konten"][0]["differenz"] == "-30.00"
    api.fx_revalue(Book(fwbook), "2026-06-30")
    b = Book(fwbook)
    led = api.ledger(b, "1021", 2026)
    assert led["saldo"] == "920.00" and led["saldo_fw"] == "1000.00"
    assert api.ledger(b, "6942", 2026)["saldo"] == "30.00"
    assert errors(fwbook) == []
    assert not (fwbook / "bewertung" / "2026-12-31.yaml").exists()
    with pytest.raises(BookError, match="bereits bewertet"):
        api.fx_revalue(Book(fwbook), "2026-06-30")
    # tampering with the revaluation row is caught
    path = fwbook / "journal" / "2026" / "2026-06.md"
    path.write_text(path.read_text().replace("30.00", "31.00"))
    assert any("Bewertung" in e for e in errors(fwbook))


def test_revaluation_gain_and_next_year_opening(fwbook):
    api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "1000", "Verkauf DE", waehrung="EUR")
    DAILY["20260630"] = "0.97"
    try:
        api.fx_revalue(Book(fwbook), "2026-06-30")
    finally:
        DAILY["20260630"] = "0.92"
    b = Book(fwbook)
    assert api.ledger(b, "6952", 2026)["saldo"] == "-20.00"
    api.post_entry(b, "2027-01-05", "6500", "1021", "100", "Material 2027", waehrung="EUR", kurs="0.97")
    led = api.ledger(Book(fwbook), "1021", 2027)
    assert led["eroeffnung"] == "970.00" and led["eroeffnung_fw"] == "1000.00" and led["saldo_fw"] == "900.00"
    assert errors(fwbook) == []


def test_fw_columns_checked(fwbook):
    api.post_entry(Book(fwbook), "2026-03-02", "1021", "3200", "1000", "Verkauf DE", waehrung="EUR")
    path = fwbook / "journal" / "2026" / "2026-03.md"
    path.write_text(path.read_text().replace("950.00", "960.00"))
    assert any("passt nicht" in e for e in errors(fwbook))
    path.write_text(path.read_text().replace("EUR 1000.00", "").replace("| 0.95 ", "|      "))
    assert any("FW-Betrag" in e for e in errors(fwbook))

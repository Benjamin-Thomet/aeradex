from decimal import Decimal

import pytest

import batzen_revolut
from batzen import api, bank, plugins
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book, problems

BUSINESS = """Date started (UTC),Date completed (UTC),ID,Type,State,Description,Reference,Payer,Card number,Orig currency,Orig amount,Payment currency,Amount,Total amount,Exchange rate,Fee,Fee currency,Balance,Account
2026-03-02 09:12:01,2026-03-02 09:12:05,a1,CARD_PAYMENT,COMPLETED,Swisscom,,,4111********1111,CHF,-59.00,CHF,-59.00,-59.00,,0.00,CHF,941.00,Main CHF
2026-03-03 10:00:00,2026-03-03 10:00:02,a2,TRANSFER,COMPLETED,Kunde AG,R-2026-0001,Kunde AG,,CHF,500.00,CHF,500.00,499.50,,0.50,CHF,1440.50,Main CHF
2026-03-04 11:00:00,,a3,CARD_PAYMENT,PENDING,Migros,,,4111********1111,CHF,-20.00,CHF,-20.00,-20.00,,0.00,CHF,,Main CHF
"""

PERSONAL = """Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
CARD_PAYMENT,Current,2026-03-05 08:00:00,2026-03-05 08:01:00,Coop,-12.40,0.00,CHF,COMPLETED,1428.10
"""


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["revolut"])
    api.add_account(b, "1022", "Revolut CHF", "aktiv")
    acct = Book(b.root)
    acct.accounts["1022"].eroeffnung = Decimal(1000)
    acct.accounts["2800"].eroeffnung = Decimal(-1000)
    acct.save_accounts()
    return Book(b.root)


def test_plugin_registered():
    assert "revolut" in plugins.installed() and plugins.installed()["revolut"].ok


def test_needs_account_mapping(book, tmp_path):
    assert any("kein Konto zugeordnet" in p for p in problems(book, ("hinweis",)))
    src = tmp_path / "revolut.csv"
    src.write_text(BUSINESS)
    with pytest.raises(BookError, match="revolut-konto CHF"):
        api.bank_import(book, str(src))


def test_business_export_imports_with_fees_and_reconciles(book, tmp_path):
    api.write(book, "Revolut CHF", batzen_revolut._assign, "CHF", "1022")
    src = tmp_path / "revolut.csv"
    src.write_text(BUSINESS)
    out = api.bank_import(Book(book.root), str(src))["import_"]
    assert out["neu"] == 3 and out["auszuege"][0]["format"] == "revolut"        # 2 payments + 1 fee, pending skipped
    tx = bank.transactions(Book(book.root))
    assert sorted(t["Betrag"] for t in tx) == ["-0.50", "-59.00", "500.00"]
    # re-import is idempotent
    assert api.bank_import(Book(book.root), str(src))["import_"]["doppelt"] == 3
    # book the open movements, then the statement reconciles to Revolut's balance
    for t in bank.transactions(Book(book.root)):
        konto = {"-59.00": "6510", "-0.50": "6940", "500.00": "3400"}[t["Betrag"]]
        api.bank_book(Book(book.root), t["ID"], konto, t["Text"])
    rec = bank.reconciliation(Book(book.root))
    assert rec and all(Decimal(str(r["differenz"])) == 0 for r in rec)
    assert_clean(Book(book.root))


def test_personal_export(book, tmp_path):
    api.write(book, "Revolut CHF", batzen_revolut._assign, "CHF", "1022")
    src = tmp_path / "privat.csv"
    src.write_text(PERSONAL)
    out = api.bank_import(Book(book.root), str(src))["import_"]
    assert out["neu"] == 1


def test_cli_assign_refuses_expense_account(book):
    with pytest.raises(BookError, match="kein Aktivkonto"):
        api.write(book, "x", batzen_revolut._assign, "CHF", "6500")

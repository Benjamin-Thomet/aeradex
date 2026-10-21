"""Deleting a file from the inbox, and giving a customer another number while nothing refers to it."""
from __future__ import annotations

import re

import pytest

from aeradex import api, invoices
from aeradex.book import Book, BookError
from aeradex.testing import make_book


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, iban="CH9300762011623852957", strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", git=True)
    for name in ("Test Eins", "Test Zwei", "Test Drei", "Daniel Schmid"):
        api.customer_add(Book(b.root), name=name, ort="Bern", plz="3000")
    return Book(b.root)


def remove_test_customers(book):
    for nr in ("K0001", "K0002", "K0003"):
        invoices.customer(book, nr)["_pfad"].unlink()


def test_renumber_a_customer_without_references(book):
    remove_test_customers(book)
    res = api.customer_renumber(Book(book.root), "K0004", "k0001")
    assert res["kunde"] == "K0001"
    custs = invoices.customers(Book(book.root))
    assert list(custs) == ["K0001"] and custs["K0001"]["name"] == "Daniel Schmid"
    assert custs["K0001"]["_pfad"].name == "K0001-daniel-schmid.md"
    assert not (book.root / "kunden" / "K0004-daniel-schmid.md").exists()
    # the next customer gets the next free number
    assert api.customer_add(Book(book.root), name="Neu", ort="Bern")["kunde"]["nummer"] == "K0002"


def test_renumber_is_refused_when_something_refers_to_the_customer(book):
    remove_test_customers(book)
    api.invoice_create(Book(book.root), "K0004", [{"text": "Arbeit", "menge": 1, "preis": "100"}], "2026-01-10")
    with pytest.raises(BookError, match="rechnungen/"):
        api.customer_renumber(Book(book.root), "K0004", "K0001")
    with pytest.raises(BookError, match="Form K0001"):
        api.customer_renumber(Book(book.root), "K0004", "1")


def test_renumber_refuses_a_taken_number(book):
    with pytest.raises(BookError, match="vergeben"):
        api.customer_renumber(Book(book.root), "K0004", "K0001")


def test_inbox_remove(book):
    api.inbox_add(book, "falsch.pdf", b"%PDF-1.4 x")
    assert api.inbox_remove(Book(book.root), "inbox/falsch.pdf")["ok"]
    assert not (book.root / "inbox" / "falsch.pdf").exists()
    with pytest.raises(BookError, match="nicht in der Inbox"):
        api.inbox_remove(Book(book.root), "inbox/falsch.pdf")
    with pytest.raises(BookError):
        api.inbox_remove(Book(book.root), "../aeradex.yaml")
    assert (book.root / "aeradex.yaml").exists()


def test_inbox_file_of_a_draft_stays(book):
    stored = api.inbox_add(book, "beleg.txt", b"Farben AG\nTotal CHF 10.00\n")
    draft = api.bill_draft_create(Book(book.root), stored["datei"])["entwurf"]
    with pytest.raises(BookError, match="Entwurf"):
        api.inbox_remove(Book(book.root), stored["datei"])
    api.bill_draft_discard(Book(book.root), draft["id"])
    assert api.inbox_remove(Book(book.root), stored["datei"])["ok"]


def test_web(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    remove_test_customers(book)
    api.inbox_add(Book(book.root), "doppelt.pdf", b"%PDF-1.4 x")
    with TestClient(create_app(book.root, token="tok")) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/").text
        assert "/inbox/loeschen" in page
        h = {"X-CSRF": re.search(r'"X-CSRF": "([^"]+)"', page).group(1), "HX-Request": "true"}
        assert c.post("/inbox/loeschen", data={"datei": "inbox/doppelt.pdf"}, headers=h).status_code == 204
        assert not (book.root / "inbox" / "doppelt.pdf").exists()
        assert "Nummer ändern" in c.get("/debitoren/kunden/K0004").text
        r = c.post("/debitoren/kunden/K0004/nummer", data={"neu": "K0001"}, headers=h)
        assert r.headers["HX-Redirect"] == "/debitoren/kunden/K0001"
        r = c.post("/m/kunden/K0001/nummer", data={"neu": "K0007"}, headers=h)
        assert r.headers["HX-Redirect"] == "/m/kunden/K0007"
        api.inbox_add(Book(book.root), "foto.pdf", b"%PDF-1.4 x")
        assert "/m/inbox/loeschen" in c.get("/m/belege").text
        assert c.post("/m/inbox/loeschen", data={"datei": "inbox/foto.pdf"}, headers=h).headers["HX-Redirect"] == "/m/belege"

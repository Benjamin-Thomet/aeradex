"""Agent tools for customers: no duplicates, details are completed with update_customer."""
from aeradex import api, tools
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book


def test_add_refuses_duplicate_and_update_completes(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    first = tools.add_customer(name="Torfix AG", firma="Torfix AG", plz="4207", ort="Bretzwil")
    assert first["ok"]
    nr = first["kunde"]["nummer"]
    again = tools.add_customer(name="Pascal Mühlberg", firma="torfix ag.", strasse="Hagmattstrasse", nr="10")
    assert again["ok"] is False and nr in again["fehler"] and "update_customer" in again["fehler"]
    assert len(api.customer_list(Book(b.root))) == 1

    res = tools.update_customer(nr, name="Pascal Mühlberg", strasse="Hagmattstrasse", nr="10", email="info@torfix.ch")
    assert res["ok"]
    cust = res["kunde"]
    assert cust["name"] == "Pascal Mühlberg" and cust["adresse"]["strasse"] == "Hagmattstrasse"
    assert cust["adresse"]["ort"] == "Bretzwil" and cust["firma"] == "Torfix AG"        # untouched fields stay
    assert tools.update_customer(nr)["ok"] is False                                      # nothing to change
    assert tools.add_customer(name="Torfix AG", firma="Torfix AG", ort="Basel", trotzdem_neu=True)["ok"]
    assert_clean(Book(b.root))


def test_private_person_duplicate(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    assert tools.add_customer(name="Peter Huber", ort="Thun")["ok"]
    assert tools.add_customer(name="peter  huber")["ok"] is False
    assert tools.add_customer(name="Petra Huber")["ok"]


def test_update_customer_can_clear_a_field(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    nr = tools.add_customer(name="Anna Meier", firma="Torfix AG", ort="Bretzwil", email="anna@torfix.ch")["kunde"]["nummer"]
    res = tools.update_customer(nr, email="info@torfix.ch", leeren=["name"])
    assert res["ok"] and res["kunde"]["name"] == "" and res["kunde"]["email"] == "info@torfix.ch"
    assert res["kunde"]["firma"] == "Torfix AG"
    assert tools.update_customer(nr, leeren=["rechnung_an"])["ok"] is False
    assert_clean(Book(b.root))


def test_customer_details_show_contact_and_address(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    nr = tools.add_customer(name="Pascal Mühlberg", firma="Torfix AG", strasse="Hagmattstrasse", nr="10",
                            plz="4207", ort="Bretzwil", email="info@torfix.ch")["kunde"]["nummer"]
    d = tools.customer_details(nr)
    assert d["name"] == "Pascal Mühlberg" and d["adresse"]["strasse"] == "Hagmattstrasse" and d["email"] == "info@torfix.ch"
    assert not any(k.startswith("_") for k in d)
    assert tools.customer_details("K9999")["ok"] is False

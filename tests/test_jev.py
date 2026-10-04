"""Jev (TypeSafe) account suggestions, against a mocked API in the documented response shape."""
from __future__ import annotations

from pathlib import Path

import pytest

from batzen import api, jev
from batzen.book import Book, BookError

from camt_sample import entry, statement

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path: Path, monkeypatch) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, strasse="Weg", nr="1", plz="3000", ort="Bern", iban=IBAN)
    api.employee_add(Book(root), "Lea", "Muster", monatslohn=5000)
    data = statement(IBAN, "1000.00", [
        (entry("59.00", "DBIT", "2026-03-10", party="Swisscom (Schweiz) AG", ustrd="Rechnung Mobile", acct_ref="A1"), "-59.00"),
        (entry("400.00", "DBIT", "2026-03-11", party="Unklar AG", ustrd="Div.", acct_ref="A2"), "-400.00"),
        (entry("4500.00", "DBIT", "2026-03-25", party="Lea Muster", ustrd="Lohn", acct_ref="A3"), "-4500.00"),
        (entry("59.00", "DBIT", "2026-02-10", party="Swisscom (Schweiz) AG", ustrd="Rechnung Februar", acct_ref="A0"), "-59.00"),
    ], "2026-02-01", "2026-03-31")
    (root / "inbox" / "a.xml").write_bytes(data)
    api.bank_import(Book(root), "inbox/a.xml")
    feb = [t for t in api.bank_list(Book(root), "offen") if t["Datum"] == "2026-02-10"][0]
    api.bank_book(Book(root), feb["ID"], "6510", "Swisscom Februar")          # history for the counterparty
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    return Book(root)


def fake_api(calls: list, answers: dict):
    def post(payload, key, timeout=30):
        calls.append(payload)
        party = payload["state"]["bewegung"]["gegenpartei"]
        choice, probs, conf = answers[party]
        return {"model": "jev-1.13.0", "answers": {"gegenkonto": {"type": "choice", "choice": choice,
                "probabilities": probs, "confidence": conf}}, "usage": {"input_tokens": 300, "output_tokens": 20}}
    return post


def test_off_by_default(book):
    with pytest.raises(BookError, match="nicht eingeschaltet"):
        api.bank_suggest(book)


def test_confident_becomes_proposal_unsure_becomes_hint_employees_never_sent(book, monkeypatch):
    api.settings_update(book, jev={"aktiv": True, "schwelle": 0.7})
    calls = []
    monkeypatch.setattr(jev, "_post", fake_api(calls, {
        "Swisscom (Schweiz) AG": ("6510", {"6510": 0.93, "6570": 0.05, "6500": 0.02}, 0.9),
        "Unklar AG": ("6700", {"6700": 0.4, "4400": 0.35, "6500": 0.25}, 0.1),
    }))
    res = api.bank_suggest(Book(book.root))["jev"]
    assert [p["konto"] for p in res["vorgeschlagen"]] == ["6510"]
    assert [u["konto"] for u in res["unsicher"]] == ["6700"]
    assert len(res["uebersprungen"]) == 1
    sent_parties = {c["state"]["bewegung"]["gegenpartei"] for c in calls}
    assert "Lea Muster" not in sent_parties                                         # salary data stays home
    swisscom = [c for c in calls if c["state"]["bewegung"]["gegenpartei"].startswith("Swisscom")][0]
    assert swisscom["model"] == "jev-latest" and swisscom["questions"]["gegenkonto"]["type"] == "choice"
    assert "1020" not in swisscom["questions"]["gegenkonto"]["criteria"]             # never the bank itself
    assert swisscom["state"]["fruehere_buchungen_gleiche_gegenpartei"][0]["konto"] == "6510"
    props = api.proposals(Book(book.root))
    assert props[0]["Soll"] == "6510" and props[0]["Haben"] == "1020" and "Konfidenz 0.90" in props[0]["Begründung"]
    hint = [t for t in api.bank_list(Book(book.root)) if t["Gegenpartei"] == "Unklar AG"][0]["Hinweis"]
    assert hint.startswith("Jev unsicher (0.10): 6700")
    api.approve(Book(book.root), [props[0]["ID"]])
    tx = [t for t in api.bank_list(Book(book.root)) if t["Text"] == "Rechnung Mobile"][0]
    assert tx["Status"] == "gebucht"
    # running again does not propose the same transaction twice
    calls.clear()
    api.bank_suggest(Book(book.root))
    assert all(c["state"]["bewegung"]["gegenpartei"] != "Swisscom (Schweiz) AG" for c in calls)


def test_api_errors_are_reported_per_transaction(book, monkeypatch):
    api.settings_update(book, jev={"aktiv": True})

    def broken(payload, key, timeout=30):
        raise BookError("TypeSafe lehnt den API-Schlüssel ab (401)")
    monkeypatch.setattr(jev, "_post", broken)
    res = api.bank_suggest(Book(book.root))["jev"]
    assert res["fehler"] and not res["vorgeschlagen"]

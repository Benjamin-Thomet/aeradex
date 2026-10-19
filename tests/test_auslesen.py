"""Journal: read a receipt or a card statement into the grid (counter account open) and book it."""
from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from aeradex import api, auslesen, bankformat, erfassung
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book

RECEIPT = "Coop Pronto Thun\n12.09.2026 14:31\nFarbe weiss 2x\nTotal CHF 48.60\nMWST 8.1% inkl. 3.64\nBezahlt Karte"
CARD = """Viseca Kreditkarte · Monatsabrechnung vom 25.09.2026
Saldo letzte Rechnung 512.30
Ihre Zahlung - Danke 21.09.2026 -512.30
03.09.2026 Jumbo Thun 89.90
11.09.2026 Shell Steffisburg 95.40
15.09.2026 Hornbach Bern 230.15
18.09.2026 Gutschrift Hornbach Bern -30.15
Jahresgebühr 25.09.2026 50.00
Neuer Saldo 435.30"""


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, rechtsform="Einzelfirma", eroeffnung={"1020": 5000, "2800": -5000},
                  uid="CHE-123.456.789", iban="CH93 0076 2011 6238 5295 7")
    api.settings_update(b, mwst={"methode": "effektiv"})
    b = Book(b.root)
    b.accounts["2040"] = type(b.accounts["2000"])(nr="2040", name="Kreditkarte", klasse="passiv",
                                                  gruppe=b.accounts["2000"].gruppe)
    b.save_accounts()
    return Book(b.root)


def fake_reader(monkeypatch, text, fields=None):
    monkeypatch.setattr(erfassung, "analyse", lambda book, path: (fields or {}, text, []))


def fake_agent(monkeypatch, lines, art, datum):
    """The agent: reads the ref from the prompt and proposes the lines through the tool's API."""
    seen = []

    def run(root, prompt):
        seen.append(prompt)
        ref = re.search(r'ref="([0-9a-f]{16})"', prompt).group(1)
        res = api.journal_lines_propose(Book(root), ref, lines, art, datum)
        assert res["ok"], res
        return "erledigt"
    monkeypatch.setattr(bankformat, "run_agent", run)
    return seen


def test_a_receipt_jev_accounts_becomes_one_line(book, monkeypatch):
    fake_reader(monkeypatch, RECEIPT, {"betrag": {"wert": "48.60"}, "datum": {"wert": "2026-09-12"},
                                       "name": {"wert": "Coop Pronto"}, "mwst_satz": {"wert": "8.1"}})

    def jev(book, meta, text):
        meta["konto"] = {"wert": "4000", "quelle": "Jev 0.93"}
    monkeypatch.setattr(erfassung, "_jev", jev)
    res = api.journal_read(book, "coop.jpg", b"jpegdata")
    assert res["art"] == "beleg" and res["quelle"] == "Jev 0.93"
    [z] = res["zeilen_raster"]
    assert (z["datum"], z["text"], z["soll"], z["haben"], z["betrag"], z["mwst"]) == \
        ("12.09.2026", "Coop Pronto", "4000", "", "48.60", "V81")
    again = api.journal_read(Book(book.root), "coop-kopie.jpg", b"jpegdata")                  # same file: no re-read
    assert again["wieder"] and again["ref"] == res["ref"]


def test_a_card_statement_goes_to_the_agent_and_is_one_beleg(book, monkeypatch):
    fake_reader(monkeypatch, CARD)
    lines = [{"datum": "2026-09-03", "text": "Jumbo Thun", "konto": "4000", "betrag": "89.90"},
             {"datum": "2026-09-11", "text": "Shell Steffisburg", "konto": "6200", "betrag": "95.40"},
             {"datum": "2026-09-15", "text": "Hornbach Bern", "konto": "4000", "betrag": "230.15"},
             {"datum": "2026-09-18", "text": "Gutschrift Hornbach", "konto": "4000", "betrag": "-30.15"},
             {"datum": "2026-09-25", "text": "Jahresgebühr", "konto": "6940", "betrag": "50.00"}]
    prompts = fake_agent(monkeypatch, lines, "karte", "2026-09-25")
    res = api.journal_read(book, "visa-09.pdf", b"%PDF card")
    assert "Kreditkartenabrechnung" in prompts[0] and "keine Codes" in prompts[0]
    grid = res["zeilen_raster"]
    assert len(grid) == 5 and {z["datum"] for z in grid} == {"25.09.2026"}                  # one Beleg, statement date
    assert grid[0]["text"] == "03.09. Jumbo Thun" and grid[4]["text"] == "Jahresgebühr"

    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        assert "data-gridread" in c.get("/journal?jahr=2026").text
        n = len(grid)
        data = {"jahr": "2026", "datum": [z["datum"] for z in grid], "text": [z["text"] for z in grid],
                "soll": [z["soll"] for z in grid], "haben": ["2040"] * n, "betrag": [z["betrag"] for z in grid],
                "mwst": [""] * n, "ref": [res["ref"]] * n, "gruppe": [res["ref"]] * n}
        r = c.post("/journal/raster", headers=h, data=data, files=[("datei", ("", b"", "application/octet-stream"))] * n)
        assert r.status_code == 204, r.text
    b = Book(book.root)
    assert {r.beleg for r in b.rows} == {"26-001"}
    assert Decimal(api.ledger(b, "2040", 2026)["saldo"]) == Decimal("-435.30")
    refund = next(r for r in b.rows if "Gutschrift" in r.text)
    assert (refund.soll, refund.haben, refund.betrag) == ("2040", "4000", Decimal("30.15"))
    assert [p.name for p in (b.root / "belege" / "2026").iterdir()] == ["26-001 visa-09.pdf"]
    assert not auslesen.folder(b, res["ref"]).exists()                                          # cleaned up
    assert_clean(b)


def test_the_agents_lines_are_checked_against_the_text(book, monkeypatch):
    fake_reader(monkeypatch, CARD)
    monkeypatch.setattr(bankformat, "run_agent", lambda root, prompt: "")
    with pytest.raises(Exception, match="keine Zeilen"):
        api.journal_read(book, "visa.pdf", b"%PDF x")
    ref = auslesen.store(Book(book.root), "visa.pdf", b"%PDF x")[0]
    bad = api.journal_lines_propose(Book(book.root), ref, [{"datum": "2026-09-03", "text": "Jumbo", "konto": "4000",
                                                            "betrag": "98.90"}], "karte", "2026-09-25")
    assert not bad["ok"] and "98.90 steht nicht im Text" in bad["pruefung"]["fehler"][0]
    bad = api.journal_lines_propose(Book(book.root), ref, [{"datum": "2026-09-03", "text": "Jumbo", "konto": "9999",
                                                            "betrag": "89.90"}], "karte")
    assert not bad["ok"] and "9999" in bad["pruefung"]["fehler"][0]


def test_a_receipt_jev_cannot_account_goes_to_the_agent_and_duplicates_are_flagged(book, monkeypatch):
    api.post_entry(book, "2026-09-12", "4000", "1020", "48.60", "Coop")
    fake_reader(monkeypatch, RECEIPT, {"betrag": {"wert": "48.60"}})
    monkeypatch.setattr(erfassung, "_jev", lambda book, meta, text: None)
    fake_agent(monkeypatch, [{"datum": "2026-09-12", "text": "Coop Pronto Farbe", "konto": "4000", "betrag": "48.60",
                              "mwst": "V81"}], "beleg", "2026-09-12")
    res = api.journal_read(Book(book.root), "coop.jpg", b"other")
    [z] = res["zeilen_raster"]
    assert z["mwst"] == "V81" and "schon gebucht: Beleg 26-001" in z["hinweis"]


def test_lines_of_one_statement_need_one_date(book, tmp_path):
    ref, path = auslesen.store(book, "v.pdf", b"%PDF y")
    with pytest.raises(api.RowErrors) as exc:
        api.post_entries(book, [
            {"datum": "2026-09-25", "text": "A", "soll": "4000", "haben": "2040", "betrag": "1", "gruppe": ref,
             "datei": str(path)},
            {"datum": "2026-09-26", "text": "B", "soll": "4000", "haben": "2040", "betrag": "2", "gruppe": ref,
             "datei": str(path)}])
    assert "dasselbe Datum" in json.dumps(exc.value.fehler, ensure_ascii=False)

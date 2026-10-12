"""«Freigeben» from the draft list: ready drafts are booked as they stand; the list shows their VAT."""
from __future__ import annotations

from decimal import Decimal
from types import ModuleType

import pytest

from aeradex import api, erfassung, kreditoren, plugins
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from test_eingang import RECEIPT, pdf
from test_erfassung import text_pdf

IBAN = "CH9300762011623852957"


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "1000": 500, "2800": -5500})
    (b.root / "inbox").mkdir(exist_ok=True)
    api.settings_update(Book(b.root), kreditoren={"agent_automatisch": False}, mwst={"methode": "effektiv"})
    return Book(b.root)


def test_receipt_is_booked_from_the_list(book):
    pdf(book.root / "inbox" / "q.pdf", RECEIPT)
    d = api.bill_draft_create(book, "inbox/q.pdf")["entwurf"]
    api.bill_draft_update(Book(book.root), d["id"], "Hand", "6500", mwst="I81")
    assert erfassung.draft(Book(book.root), d["id"])["status"] == "bereit"
    api.bill_draft_approve(Book(book.root), d["id"])
    b = Book(book.root)
    assert api.bill_drafts(b) == []
    assert Decimal(api.ledger(b, "6500", 2026)["saldo"]) == Decimal("42.09")          # 45.50 less 3.41 VAT
    assert Decimal(api.ledger(b, "1171", 2026)["saldo"]) == Decimal("3.41")
    assert_clean(b)


def test_bill_from_a_new_supplier_needs_a_person(book):
    text_pdf(book.root / "inbox" / "r.pdf")
    d = api.bill_draft_create(book, "inbox/r.pdf")["entwurf"]
    api.bill_draft_update(Book(book.root), d["id"], "Hand", "6600", mwst="I81")
    assert erfassung.draft(Book(book.root), d["id"])["status"] == "bereit"
    with pytest.raises(BookError, match="einzeln prüfen"):
        api.bill_draft_approve(Book(book.root), d["id"])
    assert len(api.bill_drafts(Book(book.root))) == 1


def test_bill_from_a_known_supplier_becomes_an_open_item(book):
    api.supplier_add(book, name="Druckerei Beispiel AG", strasse="Bahnhofstrasse", nr="5", plz="3000", ort="Bern",
                     iban=IBAN, konto="6600", mwst="I81")
    text_pdf(book.root / "inbox" / "r.pdf")
    d = api.bill_draft_create(Book(book.root), "inbox/r.pdf")["entwurf"]
    assert d["status"] == "bereit", d
    api.bill_draft_approve(Book(book.root), d["id"])
    b = Book(book.root)
    [bill] = kreditoren.bills(b).values()
    assert bill["betrag"] == Decimal("486.45") and bill["rechnungsnr"] == "2026-0815"
    assert api.bill_list(b, "offen")[0]["nummer"] == bill["nummer"]
    assert api.bill_drafts(b) == []
    assert_clean(b)


def test_not_ready_is_refused(book):
    text_pdf(book.root / "inbox" / "r.pdf")
    d = api.bill_draft_create(book, "inbox/r.pdf")["entwurf"]
    with pytest.raises(BookError, match="nicht bereit"):
        api.bill_draft_approve(Book(book.root), d["id"])


def test_list_shows_vat_and_approves_ticked_drafts(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    pdf(book.root / "inbox" / "q.pdf", RECEIPT)
    text_pdf(book.root / "inbox" / "r.pdf")
    q = api.bill_draft_create(book, "inbox/q.pdf")["entwurf"]
    r = api.bill_draft_create(Book(book.root), "inbox/r.pdf")["entwurf"]
    api.bill_draft_update(Book(book.root), q["id"], "Hand", "6500", mwst="I81")
    api.bill_draft_update(Book(book.root), r["id"], "Hand", "6600", mwst="I81")
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        page = c.get("/kreditoren?status=entwurf").text
        assert "Ausgewählte freigeben" in page and "<th>MWST</th>" in page
        assert "I81</span> 3.41" in page and "I81</span> 36.45" in page
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true", "HX-Current-URL": "/kreditoren?status=entwurf"}
        res = c.post("/eingang/freigeben", headers=h, data={"entwurf": [q["id"], r["id"]]})
        assert res.status_code == 204, res.text
    left = api.bill_drafts(Book(book.root))
    assert [d["id"] for d in left] == [r["id"]]               # the new supplier stays for a person
    assert_clean(Book(book.root))


def test_agent_asked_again_keeps_the_account(book, monkeypatch):
    """«Agent fragen» on a draft that already has an account must not leave it as «Konto offen»."""
    text_pdf(book.root / "inbox" / "r.pdf")
    d = api.bill_draft_create(book, "inbox/r.pdf")["entwurf"]
    api.bill_draft_update(Book(book.root), d["id"], "Agent", "6600", mwst="I81", begruendung="Werbung")
    api.bill_draft_mark(Book(book.root), d["id"], "agent", "An den Agenten übergeben")

    class SilentAgent:
        def __init__(self, root):
            pass

        def run(self, events, prompt):
            events.put({"type": "text", "text": "Das Konto ist schon gesetzt."})

    from aeradex.web import chat
    monkeypatch.setattr(chat, "backend", lambda preferred=None: "api")
    monkeypatch.setitem(chat.AGENTS, "api", SilentAgent)
    out = api.bill_draft_agent(Book(book.root), d["id"])
    assert out["entwurf"]["status"] == "bereit"
    assert out["entwurf"]["konto"]["wert"] == "6600"


def test_plugin_services_for_the_ui(book):
    mod = ModuleType("dienst_sample")

    @plugins.hookimpl
    def aeradex_dienste(book):
        return [plugins.Dienst("probe", ["status"]), "kein Dienst"]

    mod.aeradex_dienste = aeradex_dienste
    mod.AERADEX_PLUGIN_API = 1
    plugins.register("dienst_sample", mod)
    try:
        assert plugins.dienste(book) == []                           # not enabled for this book
        api.plugin_enable(book, "dienst_sample")
        assert plugins.dienste(Book(book.root)) == [plugins.Dienst("probe", ["status"])]
    finally:
        plugins.unregister("dienst_sample")

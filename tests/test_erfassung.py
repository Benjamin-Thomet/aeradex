"""Kreditoren upload: reading (QR, text, OCR), account chain (supplier, Jev, agent), review and booking."""
from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from batzen import api, erfassung, jev, kreditoren
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book

INVOICE = [
    "Druckerei Beispiel AG",
    "Bahnhofstrasse 5",
    "3000 Bern",
    "CHE-123.456.789 MWST",
    "",
    "Rechnung Nr. 2026-0815",
    "Rechnungsdatum: 12.03.2026",
    "Zahlbar innert 30 Tagen netto",
    "",
    "Flyer A5, 1000 Stueck            450.00",
    "MWST 8.1 %                         36.45",
    "Total CHF                         486.45",
    "",
    "IBAN CH93 0076 2011 6238 5295 7",
]


def text_pdf(path: Path) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    y = 780
    for line in INVOICE:
        c.drawString(60, y, line)
        y -= 18
    c.save()
    return path


def photo(path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 900), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=34)
    for i, line in enumerate(INVOICE):
        draw.text((60, 40 + i * 58), line, fill="black", font=font)
    img.save(path)
    return path


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, eroeffnung={"1020": 5000, "2800": -5000})
    (b.root / "inbox").mkdir(exist_ok=True)
    return b


def test_parse_text_finds_the_usual_fields():
    f = erfassung.parse_text("\n".join(INVOICE), "Test GmbH")
    assert f["name"] == "Druckerei Beispiel AG"
    assert f["rechnungsnr"] == "2026-0815"
    assert f["datum"] == "2026-03-12" and f["faellig"] == "2026-04-11"
    assert f["betrag"] == "486.45" and f["mwst_satz"] == "8.1" and f["mwst_betrag"] == "36.45"
    assert f["uid"] == "CHE-123.456.789" and f["iban"] == "CH9300762011623852957"


def test_parse_text_formats():
    f = erfassung.parse_text("Facture n° F-77\nDate: 3 mars 2026\nÉchéance 2.4.26\nMontant total CHF 1'234.50")
    assert f["rechnungsnr"] == "F-77" and f["datum"] == "2026-03-03" and f["faellig"] == "2026-04-02"
    assert f["betrag"] == "1234.50"


def test_text_pdf_becomes_draft_without_account(book, tmp_path):
    src = text_pdf(book.root / "inbox" / "druckerei.pdf")
    out = api.bill_draft_create(book, "inbox/druckerei.pdf")
    d = out["entwurf"]
    assert d["felder"]["betrag"] == {"wert": "486.45", "quelle": "Text"}
    assert d["status"] == "unsicher" and not d.get("konto")             # no supplier, Jev off → agent's turn
    assert erfassung.text_cache(Book(book.root), d["id"]).read_text().startswith("Druckerei")
    assert src.exists()                                                  # nothing moved, nothing booked
    assert Book(book.root).rows == []
    with pytest.raises(BookError, match="schon einen Entwurf"):
        api.bill_draft_create(Book(book.root), "inbox/druckerei.pdf")


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract fehlt")
def test_photo_is_read_with_ocr(book):
    photo(book.root / "inbox" / "foto.png")
    d = api.bill_draft_create(book, "inbox/foto.png")["entwurf"]
    assert d["felder"]["betrag"]["quelle"] == "OCR"
    assert d["felder"]["betrag"]["wert"] == "486.45"
    assert d["felder"]["rechnungsnr"]["wert"] == "2026-0815"


def test_known_supplier_brings_account_and_vat(book):
    api.settings_update(book, mwst={"methode": "effektiv"})
    api.supplier_add(Book(book.root), name="Druckerei Beispiel AG", iban="CH93 0076 2011 6238 5295 7", konto="6600")
    text_pdf(book.root / "inbox" / "d.pdf")
    d = api.bill_draft_create(Book(book.root), "inbox/d.pdf")["entwurf"]
    assert d["konto"] == {"wert": "6600", "quelle": "Lieferant L0001"}
    assert d["felder"]["lieferant"]["wert"] == "L0001"
    assert d["mwst"]["wert"] == "I81"                                     # 8.1 % on a 6xxx account
    assert d["status"] == "bereit"


def test_jev_confident_and_unsure(book, monkeypatch):
    api.settings_update(book, jev={"aktiv": True, "schwelle": 0.7})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    answers = iter([{"choice": "6600", "confidence": 0.91, "probabilities": {"6600": 0.91, "6500": 0.05}},
                    {"choice": "6500", "confidence": 0.41, "probabilities": {"6500": 0.41, "6600": 0.38}}])
    sent = []

    def fake(state, questions, model):
        sent.append(state)
        return {"answers": {"aufwandkonto": next(answers)}}
    monkeypatch.setattr(jev, "system_one", fake)
    text_pdf(book.root / "inbox" / "a.pdf")
    text_pdf(book.root / "inbox" / "b.pdf")
    a = api.bill_draft_create(Book(book.root), "inbox/a.pdf")["entwurf"]
    assert a["konto"]["wert"] == "6600" and a["konto"]["quelle"] == "Jev 0.91"
    b = api.bill_draft_create(Book(book.root), "inbox/b.pdf")["entwurf"]
    assert not b.get("konto") and b["status"] == "unsicher"
    assert any("Jev unsicher (0.41)" in h for h in b["hinweise"])
    assert sent[0]["rechnung"]["betrag_chf"] == "486.45" and "Druckerei" in sent[0]["rechnung"]["text"]


def test_agent_completes_draft_through_its_tool(book, monkeypatch):
    """The agent run is simulated: it calls complete_bill_draft like the MCP tool would."""
    from batzen import tools
    text_pdf(book.root / "inbox" / "c.pdf")
    d = api.bill_draft_create(book, "inbox/c.pdf")["entwurf"]
    monkeypatch.setenv("BATZEN_BUCH", str(book.root))

    class FakeAgent:
        def __init__(self, root):
            pass

        def run(self, events, prompt):
            assert d["id"] in prompt and "complete_bill_draft" in prompt
            assert tools.bill_draft(d["id"])["text"].startswith("Druckerei")
            res = tools.complete_bill_draft(d["id"], "6600", "Flyer sind Werbeaufwand", iban="CH5604835012345678009")
            assert res["ok"], res
            events.put({"type": "text", "text": "Konto 6600 gesetzt."})

    from batzen.web import chat
    monkeypatch.setattr(chat, "backend", lambda preferred=None: "api")
    monkeypatch.setitem(chat.AGENTS, "api", FakeAgent)
    out = api.bill_draft_agent(Book(book.root), d["id"])
    e = out["entwurf"]
    assert e["konto"] == {"wert": "6600", "quelle": "Agent", "begruendung": "Flyer sind Werbeaufwand"}
    assert e["felder"]["iban"]["quelle"] == "Agent"          # text IBAN may be corrected by the agent …
    assert e["status"] == "bereit" and out["agent"] == "Konto 6600 gesetzt."


def test_agent_cannot_redirect_a_qr_payment(book):
    meta = {"id": "ENT-0001", "datei": "inbox/x.pdf", "status": "unsicher",
            "felder": {"iban": {"wert": "CH4431999123000889012", "quelle": "QR"}, "betrag": {"wert": "10.00", "quelle": "QR"}}}
    (book.root / "inbox" / "x.pdf").write_bytes(b"%PDF-1.4")
    from batzen.files import write_yaml
    write_yaml(erfassung.folder(book) / "ENT-0001.yaml", meta)
    api.bill_draft_update(Book(book.root), "ENT-0001", "Agent", "6500", iban="CH5604835012345678009")
    d = erfassung.draft(Book(book.root), "ENT-0001")
    assert erfassung.value(d, "iban") == "CH4431999123000889012"            # … but never the QR-IBAN
    assert any("IBAN aus dem QR-Zahlteil bleibt" in h for h in d["hinweise"])


def test_qr_bill_draft_then_booking_closes_draft(book, tmp_path):
    supplier_root = tmp_path / "lieferant"
    api.init_book(supplier_root, "Papeterie Muster AG", 2026, strasse="Marktgasse", nr="14", plz="3011", ort="Bern",
                  iban="CH44 3199 9123 0008 8901 2", git=False)
    api.customer_add(Book(supplier_root), name="Test", firma="Test GmbH", strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    res = api.invoice_create(Book(supplier_root), "K0001", [{"text": "Druckerpapier", "menge": 5, "preis": "21.62"}], "2026-03-02")
    shutil.copy(supplier_root / res["pdf"], book.root / "inbox" / "papeterie.pdf")
    d = api.bill_draft_create(Book(book.root), "inbox/papeterie.pdf")["entwurf"]
    assert d["felder"]["iban"]["quelle"] == "QR" and d["felder"]["name"]["wert"] == "Papeterie Muster AG"
    assert d["felder"]["betrag"] == {"wert": "108.10", "quelle": "QR"}
    sup = api.supplier_add(Book(book.root), name="Papeterie Muster AG", iban=d["felder"]["iban"]["wert"])["lieferant"]
    v = erfassung.form_values(d)
    out = api.bill_add(Book(book.root), sup["nummer"], v["betrag"], entwurf=d["id"], konto="6500",
                       iban=v["iban"], referenz=v["referenz"], referenz_typ=v["referenz_typ"], datum=v["datum"] or None)
    assert out["kreditor"]["datei"].startswith("belege/")
    assert api.bill_drafts(Book(book.root)) == []
    assert not (book.root / "inbox" / "papeterie.pdf").exists()
    assert_clean(Book(book.root))


def test_ui_upload_review_and_book(tmp_path, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from batzen.web import views
    from batzen.web.app import create_app
    book = make_book(tmp_path, eroeffnung={"1020": 5000, "2800": -5000})
    api.settings_update(book, kreditoren={"agent_automatisch": False})
    pdf = text_pdf(tmp_path / "druckerei.pdf")
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        headers = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/kreditoren/einlesen", files={"dateien": ("druckerei.pdf", pdf.read_bytes(), "application/pdf")},
                   headers=headers)
        assert r.status_code == 204, r.text
        page = c.get("/kreditoren").text
        assert "ENT-0001" in page and "Druckerei Beispiel AG" in page and "Konto offen" in page
        review = c.get("/kreditoren/neu?entwurf=ENT-0001").text
        assert 'value="486.45"' in review and 'value="2026-04-11"' in review and ">Text</span>" in review
        assert "nicht aus einem QR-Zahlteil" in review                       # IBAN from text: warn
        started = []
        monkeypatch.setattr(views, "_agent_in_background", lambda root, ids: started.extend(ids))
        assert c.post("/kreditoren/entwurf/ENT-0001/agent", headers=headers).status_code == 204
        assert started == ["ENT-0001"]
        api.bill_draft_update(Book(book.root), "ENT-0001", "Agent", "6600", begruendung="Werbung")
        r = c.post("/kreditoren/neu", data={"entwurf": "ENT-0001", "lieferant": "neu", "s_name": "Druckerei Beispiel AG",
                                            "betrag": "486.45", "datum": "2026-03-12", "faellig": "2026-04-11",
                                            "konto": "6600", "iban": "CH9300762011623852957", "rechnungsnr": "2026-0815"},
                   headers=headers)
        assert r.status_code == 204, r.text
    b = Book(book.root)
    assert api.bill_drafts(b) == [] and len(kreditoren.bills(b)) == 1
    assert_clean(b)


def test_iban_of_another_supplier_is_a_conflict(book):
    """The bill names one company but carries the IBAN of another known supplier: never matched silently."""
    api.supplier_add(book, name="Immobilien Muster AG", iban="CH93 0076 2011 6238 5295 7", konto="6000")
    text_pdf(book.root / "inbox" / "d.pdf")                       # «Druckerei Beispiel AG», same IBAN
    d = api.bill_draft_create(Book(book.root), "inbox/d.pdf")["entwurf"]
    assert d["status"] == "konflikt" and not d.get("konto") and "lieferant" not in d["felder"]
    assert d["felder"]["name"]["wert"] == "Druckerei Beispiel AG"
    assert "gehört dem Lieferanten L0001 Immobilien Muster AG" in d["konflikt"]
    assert erfassung.needs_agent(d)
    assert any("gehört dem Lieferanten" in w for w in erfassung.warnings(Book(book.root), d))


def test_sender_address_is_read():
    f = erfassung.parse_text("\n".join(INVOICE), "Test GmbH")
    assert (f["strasse"], f["nr"], f["plz"], f["ort"]) == ("Bahnhofstrasse", "5", "3000", "Bern")

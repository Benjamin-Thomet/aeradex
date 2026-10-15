"""Kreditoren upload: reading (QR, text, OCR), account chain (supplier, Jev, agent), review and booking."""
from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from aeradex import api, erfassung, jev, kreditoren
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book

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
    from aeradex import tools
    text_pdf(book.root / "inbox" / "c.pdf")
    d = api.bill_draft_create(book, "inbox/c.pdf")["entwurf"]
    monkeypatch.setenv("AERADEX_BUCH", str(book.root))

    class FakeAgent:
        def __init__(self, root):
            pass

        def run(self, events, prompt):
            assert d["id"] in prompt and "complete_bill_draft" in prompt
            assert tools.bill_draft(d["id"])["text"].startswith("Druckerei")
            res = tools.complete_bill_draft(d["id"], "6600", "Flyer sind Werbeaufwand", iban="CH5604835012345678009")
            assert res["ok"], res
            events.put({"type": "text", "text": "Konto 6600 gesetzt."})

    from aeradex.web import chat
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
    from aeradex.files import write_yaml
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
    from aeradex.web import views
    from aeradex.web.app import create_app
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


def test_foreign_currency_is_detected():
    f = erfassung.parse_text("Druckhaus Berlin GmbH\nRechnung Nr. B-17\nDatum 02.03.2026\nGesamtbetrag EUR 1.190,00")
    assert f["waehrung"] == "EUR" and f["betrag"] == "1190.00"
    assert erfassung.parse_text("Total € 59,90")["waehrung"] == "EUR"


def test_agent_splits_a_draft(book, monkeypatch):
    from aeradex import tools
    text_pdf(book.root / "inbox" / "s.pdf")
    d = api.bill_draft_create(book, "inbox/s.pdf")["entwurf"]
    monkeypatch.setenv("AERADEX_BUCH", str(book.root))
    res = tools.complete_bill_draft(d["id"], "6600", "Flyer Werbung, Rest Büromaterial",
                                    aufteilung=[{"konto": "6600", "betrag": "400.00"},
                                                {"konto": "6500", "betrag": "86.45", "text": "Papier"}])
    assert res["ok"], res
    meta = erfassung.draft(Book(book.root), d["id"])
    v = erfassung.form_values(meta)
    assert [p["konto"] for p in v["positionen"]] == ["6600", "6500"] and meta["positionen"]["quelle"] == "Agent"
    bad = tools.complete_bill_draft(d["id"], "6600", "x", aufteilung=[{"konto": "6600", "betrag": "1.00"}])
    assert bad["ok"]                                    # accepted as a draft, but flagged
    assert any("Positionen ergeben 1.00" in h for h in erfassung.draft(Book(book.root), d["id"])["hinweise"])


def test_foreign_iban_address_and_confirmed_fields(book, monkeypatch):
    text = ("Druckhaus Berlin GmbH\nUnter den Linden 12\n10117 Berlin\nRechnung Nr. DB-1\nDatum 01.10.2026\n"
            "Gesamtbetrag EUR 1.190,00\nIBAN DE89 3704 0044 0532 0130 00  BIC COBADEFFXXX")
    f = erfassung.parse_text(text)
    assert f["iban"] == "DE89370400440532013000" and f["land"] == "DE"
    assert (f["plz"], f["ort"]) == ("10117", "Berlin")
    from aeradex.files import write_yaml
    (book.root / "inbox" / "x.pdf").write_bytes(b"%PDF-1.4")
    write_yaml(erfassung.folder(book) / "ENT-0001.yaml", {"id": "ENT-0001", "datei": "inbox/x.pdf", "status": "unsicher",
               "felder": {"waehrung": {"wert": "EUR", "quelle": "Text"}}})
    api.bill_draft_update(Book(book.root), "ENT-0001", "Agent", "6600", waehrung="EUR")
    assert erfassung.draft(Book(book.root), "ENT-0001")["felder"]["waehrung"]["quelle"] == "Text"


def test_agent_also_when_amount_date_or_a_named_period_is_missing(book):
    """The account alone is not enough: a draft whose amount or date was not read, or whose text speaks of a
    subscription/rent without a period that was read, goes to the agent too, which is told what is open."""
    api.supplier_add(book, name="Druckerei Beispiel AG", iban="CH93 0076 2011 6238 5295 7", konto="6600")
    inbox = book.root / "inbox"
    (inbox / "abo.txt").write_text("\n".join(INVOICE[:9] + ["Abo Druckvorlagen-Software"] + INVOICE[9:]), encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/abo.txt")["entwurf"]
    assert d["konto"]["wert"] == "6600" and d["zeitraum_pruefen"]
    assert erfassung.missing(d) == ["leistung_von"] and erfassung.needs_agent(d)
    prompt = erfassung.agent_prompt(erfassung.draft(Book(book.root), d["id"]), Book(book.root))
    assert "Noch offen: Leistungszeitraum" in prompt and "Das Konto 6600 steht schon" in prompt
    assert "leistung_von und leistung_bis" in prompt

    (inbox / "abo2.txt").write_text("\n".join(INVOICE[:9] + ["Abo Druckvorlagen-Software",
                                    "Leistungszeitraum 01.03.2026 - 28.02.2027"] + INVOICE[9:]), encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/abo2.txt")["entwurf"]
    assert d["felder"]["leistung_von"]["wert"] == "2026-03-01" and not d.get("zeitraum_pruefen")
    assert erfassung.missing(d) == [] and not erfassung.needs_agent(d)

    (inbox / "ohne.txt").write_text("\n".join(l for l in INVOICE if not l.startswith("Total")), encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/ohne.txt")["entwurf"]
    assert erfassung.missing(d) == ["betrag"] and erfassung.needs_agent(d)


def test_receipt_prompt_asks_for_the_period(book):
    (book.root / "inbox" / "q.txt").write_text("Migros\nQuittung\nBar bezahlt\n12.03.2026\nTotal CHF 23.40\n",
                                               encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/q.txt")["entwurf"]
    assert d["art"] == "quittung"
    assert "leistung_von und leistung_bis" in erfassung.agent_prompt(erfassung.draft(Book(book.root), d["id"]), Book(book.root))


RECEIPT_NO_TOTAL = [
    "Farben Keller AG", "Industriestrasse 4", "3600 Thun", "Lieferschein 03.03.2026",
    "Rechnung vom 05.03.2026", "Dispersionsfarbe weiss 15 l      240.00", "Abdeckband 10 Rollen  45.00",
    "Summe                          308.10", "Zahlbar bis 04.04.2026", "IBAN CH93 0076 2011 6238 5295 7",
]


def test_jev_picks_amount_date_and_vat_from_the_document(book, monkeypatch):
    """Jev chooses among what is on the bill (never a value of its own): a confident pick fills an empty field,
    a confident different pick only warns, the Vorsteuer code comes from its choice when no rate was read."""
    api.settings_update(book, jev={"aktiv": True, "schwelle": 0.7}, mwst={"methode": "effektiv"})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sent = []
    reply = {"aufwandkonto": {"choice": "4000", "confidence": 0.88, "probabilities": {"4000": 0.88}},
             "rechnungsbetrag": {"choice": "308.10", "confidence": 0.93, "probabilities": {"308.10": 0.93}},
             "rechnungsdatum": {"choice": "2026-03-05", "confidence": 0.81, "probabilities": {"2026-03-05": 0.81}},
             "mwst_code": {"choice": "V81", "confidence": 0.77, "probabilities": {"V81": 0.77}}}

    def fake(state, questions, model):
        sent.append(questions)
        return {"answers": {k: v for k, v in reply.items() if k in questions}}
    monkeypatch.setattr(jev, "system_one", fake)
    (book.root / "inbox" / "k.txt").write_text("\n".join(RECEIPT_NO_TOTAL), encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/k.txt")["entwurf"]
    q = sent[0]
    assert set(q) == {"aufwandkonto", "rechnungsbetrag", "rechnungsdatum", "mwst_code"}
    assert "308.10" in q["rechnungsbetrag"]["criteria"] and "45.00" in q["rechnungsbetrag"]["criteria"]
    assert "Zeile «Summe" in q["rechnungsbetrag"]["criteria"]["308.10"]
    assert set(q["rechnungsdatum"]["criteria"]) >= {"2026-03-03", "2026-03-05", "2026-04-04"}
    assert d["felder"]["betrag"] == {"wert": "308.10", "quelle": "Jev 0.93"}       # «Summe» is no total word
    assert d["konto"]["wert"] == "4000" and d["mwst"] == {"wert": "V81", "quelle": "Jev 0.77"}
    read_date = d["felder"]["datum"]["wert"]
    if read_date != "2026-03-05":                       # the reader took another date: Jev only warns
        assert any("Jev hält Datum 2026-03-05" in w for w in erfassung.warnings(Book(book.root), d))
    # unsure answers change nothing; a value Jev may not choose from is ignored
    reply.update(rechnungsbetrag={"choice": "999.99", "confidence": 0.99},
                 aufwandkonto={"choice": "4000", "confidence": 0.3, "probabilities": {"4000": 0.3}})
    (book.root / "inbox" / "k2.txt").write_text("\n".join(RECEIPT_NO_TOTAL), encoding="utf-8")
    d2 = api.bill_draft_create(Book(book.root), "inbox/k2.txt")["entwurf"]
    assert "betrag" not in d2["felder"] and not d2.get("konto") and erfassung.missing(d2)[:2] == ["konto", "betrag"]


def test_confident_other_amount_blocks_approval_from_the_list(book, monkeypatch):
    api.supplier_add(book, name="Druckerei Beispiel AG", iban="CH93 0076 2011 6238 5295 7", konto="6600")
    api.settings_update(book, jev={"aktiv": True, "schwelle": 0.7})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(jev, "system_one", lambda s, q, m: {"answers": {
        "rechnungsbetrag": {"choice": "450.00", "confidence": 0.9}}})
    text_pdf(book.root / "inbox" / "x.pdf")
    d = api.bill_draft_create(Book(book.root), "inbox/x.pdf")["entwurf"]
    assert d["felder"]["betrag"]["wert"] == "486.45"                               # the reader's value stays
    assert any("Jev hält Betrag 450.00" in w for w in erfassung.warnings(Book(book.root), d))
    with pytest.raises(BookError, match="Jev hält Betrag"):
        api.bill_draft_approve(Book(book.root), d["id"])


def test_a_very_sure_jev_overrules_the_reader_and_dates_are_no_amounts(book, monkeypatch):
    api.settings_update(book, jev={"aktiv": True, "schwelle": 0.7})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    sent = []

    def fake(state, questions, model):
        sent.append(questions)
        return {"answers": {"rechnungsdatum": {"choice": "2026-03-05", "confidence": 0.99}}}
    monkeypatch.setattr(jev, "system_one", fake)
    lines = ["Farben Keller AG", "Lieferschein 03.03.2026", "Rechnung vom 05.03.2026", "Total CHF 308.10"]
    (book.root / "inbox" / "v.txt").write_text("\n".join(lines), encoding="utf-8")
    d = api.bill_draft_create(Book(book.root), "inbox/v.txt")["entwurf"]
    assert d["felder"]["datum"] == {"wert": "2026-03-05", "quelle": "Jev 0.99"}
    assert any("gelesen war 2026-03-03" in h for h in d["hinweise"])
    assert not erfassung.warnings(Book(book.root), d)
    assert list(jev.bill_candidates("\n".join(lines))[0]) == ["308.10"]          # not «03.03» of a date


def test_jev_second_opinion_on_a_known_suppliers_account(book, monkeypatch):
    """Known supplier with its account: Jev is asked anyway; a confident other account never replaces the
    supplier's, it warns and blocks approval from the list until the account is changed."""
    api.supplier_add(book, name="Druckerei Beispiel AG", iban="CH93 0076 2011 6238 5295 7", konto="6600")
    api.settings_update(book, jev={"aktiv": True, "schwelle": 0.7})
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    answer = {"choice": "1500", "confidence": 0.88}
    sent = []

    def fake(state, questions, model):
        sent.append(questions)
        return {"answers": {"aufwandkonto": answer}}
    monkeypatch.setattr(jev, "system_one", fake)
    text_pdf(book.root / "inbox" / "m.pdf")
    d = api.bill_draft_create(Book(book.root), "inbox/m.pdf")["entwurf"]
    assert "aufwandkonto" in sent[0] and d["konto"]["wert"] == "6600"
    warn = erfassung.warnings(Book(book.root), d)
    assert any("Jev hält Konto 1500" in w and "eingesetzt ist 6600" in w for w in warn)
    with pytest.raises(BookError, match="Jev hält Konto 1500"):
        api.bill_draft_approve(Book(book.root), d["id"])
    erfassung.update(Book(book.root), d["id"], "Delia", konto="1500")          # changed: the warning is gone
    assert not any("Jev hält Konto" in w for w in erfassung.warnings(Book(book.root), erfassung.draft(Book(book.root), d["id"])))
    answer.update(choice="6600", confidence=0.95)                               # agrees: nothing to see
    text_pdf(book.root / "inbox" / "n.pdf")
    d = api.bill_draft_create(Book(book.root), "inbox/n.pdf")["entwurf"]
    assert d["konto"]["wert"] == "6600" and not erfassung.warnings(Book(book.root), d)

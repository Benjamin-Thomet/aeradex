"""The web UI: every page renders on the demo book and every form goes through
the same api path as the CLI (validated, written, checked, committed)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from batzen import check, gitlog  # noqa: E402
from batzen.book import Book  # noqa: E402
from batzen.web.app import create_app  # noqa: E402

DEMO = Path(__file__).resolve().parent.parent / "examples" / "muster-gmbh"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    target = tmp_path / "buch"
    shutil.copytree(DEMO, target)
    gitlog.init_repo(target)
    gitlog.commit(target, "Demo-Stand")
    return target


@pytest.fixture
def client(root: Path):
    app = create_app(root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        c.csrf = app.state.ui.csrf
        yield c


def post(c, url, data=None, files=None):
    return c.post(url, data=data or {}, files=files, headers={"X-CSRF": c.csrf, "HX-Request": "true"})


def ok(r) -> str:
    """A successful HTMX form post answers 204 + HX-Redirect; a refusal answers an error box."""
    assert r.status_code == 204, r.text
    return r.headers["HX-Redirect"]


def errors(root: Path) -> list[str]:
    return [str(i) for i in check.run(Book(root)) if i.level == "fehler"]


def last_commit(root: Path) -> str:
    return gitlog.log(root, 1)[0]["nachricht"]


PAGES = ["/", "/pruefen", "/journal", "/journal?monat=alle&q=Lohn", "/journal?ohne_beleg=1", "/konten",
         "/konten/saldenliste?periode=q1", "/konten/1020", "/debitoren", "/debitoren/neu", "/debitoren/kunden",
         "/debitoren/kunden?edit=K0001", "/debitoren/offene-posten", "/debitoren/rechnung/R-2026-0001", "/lohn",
         "/lohn/abrechnung/2026-03/M0002", "/lohn/mitarbeiter?edit=M0001", "/lohn/lohnkonto/2026/M0001",
         "/abschluss", "/verlauf", "/einstellungen", "/pdf/jahresrechnung", "/pdf/journal", "/pdf/debitoren",
         "/pdf/kontoblatt?konto=1020"]


def test_every_page_renders(client):
    for page in PAGES:
        r = client.get(page)
        assert r.status_code == 200, f"{page}: {r.text[-400:]}"


def test_access_needs_token_and_csrf(root):
    app = create_app(root, token="tok")
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/?t=falsch").status_code == 403
        c.get("/?t=tok")
        assert c.get("/").status_code == 200
        r = c.post("/journal/buchen", data={"datum": "2026-04-01"})
        assert r.status_code == 403                       # no CSRF header
        r = c.post("/journal/buchen", data={}, headers={"X-CSRF": app.state.ui.csrf, "Origin": "http://evil.example"})
        assert r.status_code == 403                       # foreign origin
        assert c.get("/datei/../../etc/passwd").status_code == 404


def test_book_inbox_receipt_from_pruefen(client, root):
    r = post(client, "/pruefen/buchen", {"datum": "2026-03-12", "betrag": "32.50", "text": "Papier",
                                          "soll": "6500  Büromaterial", "haben": "1000  Kasse",
                                          "datei": "quittung-papeterie.txt"})
    ok(r)
    assert not (root / "inbox" / "quittung-papeterie.txt").exists()
    assert any(p.name.endswith("quittung-papeterie.txt") for p in (root / "belege" / "2026").iterdir())
    assert "gebucht" in last_commit(root) and errors(root) == []


def test_refused_booking_shows_error_and_writes_nothing(client, root):
    before = (root / "journal" / "2026" / "2026-03.md").read_text()
    r = post(client, "/journal/buchen", {"modus": "einfach", "datum": "2026-03-12", "betrag": "10", "text": "x",
                                          "soll": "9999", "haben": "1020"})
    assert r.status_code == 200 and "Konto 9999 existiert nicht" in r.text
    assert (root / "journal" / "2026" / "2026-03.md").read_text() == before


def test_split_booking_with_upload_and_storno(client, root):
    r = post(client, "/journal/buchen", {"modus": "split", "datum": "2026-03-20", "text": "Einkauf",
                                          "z_soll": ["6500", "6641", ""], "z_haben": ["", "", "1020"],
                                          "z_betrag": ["30", "20", "50"], "z_text": ["", "", ""]},
             files={"datei": ("kassenzettel.txt", b"Coop 50.00", "text/plain")})
    ok(r)
    b = Book(root)
    beleg = [x for x in b.rows if x.text == "Einkauf"][0].beleg
    assert any(p.name.startswith(beleg) for p in (root / "belege" / "2026").iterdir())
    ok(post(client, "/journal/storno", {"beleg": beleg, "datum": "2026-03-21"}))
    assert "storniert" in last_commit(root) and errors(root) == []


def test_attach_receipt_to_existing_beleg(client, root):
    ok(post(client, "/journal/beleg", {"beleg": "26-001"}, files={"datei": ("quittung.txt", b"x", "text/plain")}))
    assert (root / "belege" / "2026" / "26-001 quittung.txt").exists()


def test_proposal_approve_and_reject(client, root):
    ok(post(client, "/vorschlaege/freigeben", {"id": "V-001"}))
    assert any(r.text == "Swisscom März" for r in Book(root).rows)
    r = post(client, "/vorschlaege/verwerfen", {"id": "V-999"})
    assert "nicht gefunden" in r.text


def test_invoice_flow(client, root):
    r = post(client, "/debitoren/vorschau", {"p_text": ["Beratung"], "p_menge": ["2"], "p_einheit": ["h"],
                                              "p_preis": ["150"], "p_konto": ["3400"]})
    assert "300.00" in r.text
    to = ok(post(client, "/debitoren/neu", {"kunde": "K0001", "datum": "2026-04-02", "zahlungsfrist": "30",
                                             "p_text": ["Beratung"], "p_menge": ["2"], "p_einheit": ["h"],
                                             "p_preis": ["150"], "p_konto": ["3400  Dienstleistungserlöse"]}))
    nr = to.rsplit("/", 1)[-1]
    assert (root / "rechnungen" / "2026" / f"{nr}.pdf").exists()
    r = post(client, "/debitoren/zuordnen", {"betrag": "300", "text": f"Zahlung {nr}"})
    assert nr in r.text
    ok(post(client, f"/debitoren/rechnung/{nr}/zahlung", {"betrag": "300", "datum": "2026-04-10", "konto": "1020"}))
    assert client.get(f"/debitoren/rechnung/{nr}").status_code == 200
    r = post(client, f"/debitoren/rechnung/{nr}/storno", {"grund": "x"})
    assert "Zahlungen" in r.text                                # paid invoices cannot be voided
    assert errors(root) == []


def test_customer_add_and_edit(client, root):
    ok(post(client, "/debitoren/kunden/neu", {"name": "Neu Kunde", "firma": "Neu AG", "plz": "8000", "ort": "Zürich",
                                               "rechnung_an": "firma"}))
    ok(post(client, "/debitoren/kunden/K0003", {"name": "Neu Kunde", "firma": "Neu AG", "strasse": "Weg", "nr": "1",
                                                 "plz": "8001", "ort": "Zürich", "land": "CH", "rechnung_an": "person",
                                                 "notizen": "VIP"}))
    text = next((root / "kunden").glob("K0003-*.md")).read_text()
    assert "plz: '8001'" in text and "rechnung_an: person" in text and "VIP" in text


def test_payroll_inputs_close_reopen(client, root):
    to = "/lohn/abrechnung/2026-03/M0002"
    ok(post(client, f"{to}/eingaben", {"stunden": "40", "qst_satzbestimmend": "3000", "korrektur": "",
                                        "korrektur_text": ""}))
    page = client.get(to).text
    assert "QST-Eingabe fehlt" not in page
    ok(post(client, f"{to}/abschliessen"))
    assert (root / "lohn" / "2026" / "03" / "M0002.pdf").exists()
    assert any(r.quelle == "lohn:2026-03:M0002" for r in Book(root).rows)
    ok(post(client, f"{to}/oeffnen"))
    assert not any(r.quelle == "lohn:2026-03:M0002" for r in Book(root).rows)
    assert errors(root) == []


def test_employee_add_and_edit(client, root):
    ok(post(client, "/lohn/mitarbeiter/neu", {"vorname": "Mia", "nachname": "Neu", "lohnart": "monat",
                                               "monatslohn": "5000", "pensum": "60", "eintritt": "2026-04-01",
                                               "qst_tabelle": "BS-2026", "qst_code": "a0n"}))
    ok(post(client, "/lohn/mitarbeiter/M0003", {"vorname": "Mia", "nachname": "Neu", "lohnart": "monat",
                                                 "monatslohn": "5200", "pensum": "60", "aktiv": "1",
                                                 "qst_tabelle": "BS-2026", "qst_code": ""}))
    text = next((root / "personal").glob("M0003-*.md")).read_text()
    assert "monatslohn: 5200.00" in text and "qst: null" in text
    ok(post(client, "/lohn/lauf", {"monat": "2026-04"}))
    assert (root / "lohn" / "2026" / "04" / "M0003.md").exists()


def test_settings_accounts_and_closing(client, root):
    ok(post(client, "/einstellungen", {"firma": "Muster GmbH", "rechtsform": "GmbH", "uid": "", "telefon": "031 000 00 00",
                                        "email": "", "iban": "CH93 0076 2011 6238 5295 7", "qr_referenz_praefix": "",
                                        "zahlungsfrist_tage": "20", "agent_modus": "direkt", "co": "",
                                        "a_strasse": "Bahnhofstrasse", "a_nr": "1", "a_plz": "3000", "a_ort": "Bern",
                                        "a_land": "CH", "k_bank": "1020  Bank"}))
    assert Book(root).settings.get("agent_modus") == "direkt"
    ok(post(client, "/einstellungen/lohn", {"an_ahv": "5.3", "an_alv": "1.1", "an_uvg": "0.8", "an_ktg": "0.7",
                                             "ag_bvg": "gleich_an", "buchen": "1"}))
    assert "uvg: 0.008" in (root / "lohn" / "einstellungen.yaml").read_text()
    ok(post(client, "/konten/neu", {"nr": "6575", "name": "Software-Abos", "klasse": ""}))
    ok(post(client, "/konten/6575/aendern", {"name": "Software-Abonnemente", "gruppe": "betrieb", "aktiv": "1"}))
    assert Book(root).accounts["6575"].name == "Software-Abonnemente"
    ok(post(client, "/abschluss/anhang", {"jahr": "2026", "text": "## Firma\nMuster GmbH, Bern."}))
    assert (root / "abschluss" / "2026" / "anhang.md").exists()
    # close the March drafts first, then lock Q1
    for nr in ("M0001", "M0002"):
        ok(post(client, f"/lohn/abrechnung/2026-03/{nr}/abschliessen"))
    ok(post(client, "/abschluss/sperre", {"jahr": "2026", "bis": "2026-03-31"}))
    r = post(client, "/journal/buchen", {"modus": "einfach", "datum": "2026-03-30", "betrag": "1", "text": "zu spät",
                                          "soll": "6500", "haben": "1020"})
    assert "gesperrt" in r.text
    ok(post(client, "/abschluss/entsperren", {"jahr": "2026", "bis": "", "grund": "Test"}))
    assert errors(root) == []


def test_inbox_upload(client, root):
    to = ok(post(client, "/pruefen/upload", files={"datei": ("Rechnung Swisscom.pdf", b"%PDF-1.4 test", "application/pdf")}))
    assert "rechnung-swisscom.pdf" in to
    assert (root / "inbox" / "rechnung-swisscom.pdf").exists()
    assert client.get("/pruefen?datei=rechnung-swisscom.pdf").status_code == 200


def test_ui_and_cli_writes_are_one_history(client, root):
    """A write from another process (CLI/agent) lands in the same history and the UI sees it."""
    from batzen import api
    api.post_entry(Book(root), "2026-03-25", "6500", "1020", "5", "von der Kommandozeile")
    assert "von der Kommandozeile" in client.get("/journal?monat=3").text

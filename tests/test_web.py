"""The web UI: every page renders on the demo book and every form goes through
the same api path as the CLI (validated, written, checked, committed)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from allkvitt import check, gitlog  # noqa: E402
from allkvitt.book import Book  # noqa: E402
from allkvitt.web.app import create_app  # noqa: E402

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


def test_balance_chart_preserves_opening_on_january_first():
    from datetime import date
    from decimal import Decimal
    from allkvitt.web.views import _balance_chart

    chart = _balance_chart({"eroeffnung": Decimal("1000"), "zeilen": [
        {"datum": date(2025, 1, 1), "saldo": Decimal("750")},
        {"datum": date(2025, 1, 1), "saldo": Decimal("500")},
        {"datum": date(2025, 2, 1), "saldo": Decimal("600")},
    ]}, 2025, False)
    assert chart["max"] == Decimal("1000")
    assert chart["ends"][0]["v"] == Decimal("500")
    assert chart["ends"][1]["v"] == Decimal("600")


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


def test_payroll_year_picker_shows_uncomputed_months(client):
    from lxml import html
    doc = html.fromstring(client.get("/lohn?jahr=2027").text)
    options = doc.xpath('//select[@name="monat"]/option')
    assert {o.get("value") for o in options} == {f"2027-{m:02d}" for m in range(1, 13)}
    assert doc.xpath('//select[@name="jahr"]/option[@selected]')[0].get("value") == "2027"
    assert len(doc.xpath('//details[@id="monate"]//tbody/tr')) == 12
    assert len(doc.xpath('//details[@id="monate"]//a[contains(@href, "2027-")]')) == 12
    assert 'href="/lohn/lohnkonto/2027/M0001"' in client.get("/lohn/mitarbeiter?jahr=2027").text
    assert client.get("/lohn/lohnkonto/2026/M0001?jahr=2027").url.path == "/lohn/lohnkonto/2027/M0001"


def test_lohnausweis_readiness_and_year_status(client, root):
    from lxml import html
    from allkvitt import api
    api.employee_add(Book(root), "Mia", "Jahresabschluss", monatslohn=5000, eintritt="2026-10-15")
    api.employee_update(Book(root), "M0003", austritt="2026-11-10", aktiv=False)
    # Inactive today must still be visible for missing historical months of employment.
    assert "Mia Jahresabschluss" in client.get("/lohn?monat=2026-10").text
    url = "/lohn/lohnkonto/2026/M0003"
    target = "/lohn/lohnausweis/2026/M0003"
    doc = html.fromstring(client.get(url).text)
    rows = doc.xpath('//table[contains(@class,"ledger")]/tbody/tr')[:12]
    assert len(rows) == 12
    assert "ausserhalb Anstellung" in rows[0].text_content()
    assert "nicht gerechnet" in rows[9].text_content()
    assert doc.xpath(f'//form[@hx-post="{target}"]/button[@disabled]')
    result = post(client, target)
    assert "nicht gerechnet: 2026-10, 2026-11" in result.text
    assert not (root / "lohnausweise" / "2026" / "M0003.pdf").exists()
    api.payroll_run(Book(root), "2026-10", "M0003")
    api.payslip_close(Book(root), "2026-10", "M0003")
    api.payroll_run(Book(root), "2026-11", "M0003")
    assert "nicht abgeschlossen: 2026-11" in post(client, target).text
    api.payslip_close(Book(root), "2026-11", "M0003")
    doc = html.fromstring(client.get(url).text)
    assert not doc.xpath(f'//form[@hx-post="{target}"]/button[@disabled]')
    ok(post(client, target))
    assert "Lohnausweis 2026 öffnen" in client.get(url).text


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
    assert to == "/#inbox"
    assert (root / "inbox" / "rechnung-swisscom.pdf").exists()
    assert 'id="inbox"' in client.get("/").text and "rechnung-swisscom.pdf" in client.get("/").text
    assert client.get("/pruefen?datei=rechnung-swisscom.pdf", follow_redirects=False).headers["location"] == "/#inbox"


def test_ui_and_cli_writes_are_one_history(client, root):
    """A write from another process (CLI/agent) lands in the same history and the UI sees it."""
    from allkvitt import api
    api.post_entry(Book(root), "2026-03-25", "6500", "1020", "5", "von der Kommandozeile")
    assert "von der Kommandozeile" in client.get("/journal?monat=3").text


def test_mwst_screens_and_booking(client, root):
    ok(post(client, "/einstellungen", {"firma": "Muster GmbH", "rechtsform": "GmbH", "uid": "CHE-123.456.789 MWST",
                                        "telefon": "", "email": "", "iban": "CH93 0076 2011 6238 5295 7",
                                        "qr_referenz_praefix": "", "zahlungsfrist_tage": "30", "agent_modus": "vorschlag",
                                        "co": "", "a_strasse": "Bahnhofstrasse", "a_nr": "1", "a_plz": "3000",
                                        "a_ort": "Bern", "a_land": "CH", "mwst_methode": "effektiv", "mwst_periode": ""}))
    assert "MWST-Code" in client.get("/journal").text
    ok(post(client, "/journal/buchen", {"modus": "einfach", "datum": "2026-04-02", "betrag": "108.10", "text": "Papier",
                                         "soll": "6500", "haben": "1020", "mwst": "V81"}))
    r = post(client, "/debitoren/vorschau", {"p_text": ["Beratung"], "p_menge": ["10"], "p_einheit": ["h"],
                                              "p_preis": ["100"], "p_konto": ["3400"], "p_mwst": ["U81"]})
    assert "1&#39;081.00" in r.text or "1'081.00" in r.text
    ok(post(client, "/debitoren/neu", {"kunde": "K0001", "datum": "2026-04-03", "p_text": ["Beratung"], "p_menge": ["10"],
                                        "p_einheit": ["h"], "p_preis": ["100"], "p_konto": ["3400"], "p_mwst": ["U81"]}))
    page = client.get("/mwst?jahr=2026&periode=2026-Q2").text
    assert "72.90" in page                                        # 81.00 − 8.10
    assert client.get("/mwst/pdf?periode=2026-Q2").content.startswith(b"%PDF")
    ok(post(client, "/mwst/buchen", {"periode": "2026-Q2"}))
    assert "gebucht" in client.get("/mwst?jahr=2026&periode=2026-Q2").text
    page = client.get("/mwst/abstimmung?jahr=2026").text
    assert "Umsatzabstimmung 2026" in page and "Noch nicht gebuchte Abrechnungen" in page
    response = client.get("/mwst/abstimmung/pdf?jahr=2026")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert 'MWST-Umsatzabstimmung 2026.pdf' in response.headers["content-disposition"]
    assert errors(root) == []


def test_kreditoren_from_inbox_qr_to_payment(client, root, tmp_path):
    from allkvitt import api as core
    sup = tmp_path / "lieferant"
    core.init_book(sup, "Papeterie Muster AG", 2026, strasse="Marktgasse", nr="14", plz="3011", ort="Bern",
                   iban="CH44 3199 9123 0008 8901 2", git=False)
    core.customer_add(Book(sup), name="X", firma="Muster GmbH", strasse="Bahnhofstrasse", nr="1", plz="3000", ort="Bern")
    pdf = sup / core.invoice_create(Book(sup), "K0001", [{"text": "Papier", "menge": 1, "preis": "86.40"}],
                                    "2026-03-02")["pdf"]
    shutil.copy(pdf, root / "inbox" / "papeterie.pdf")
    assert "papeterie.pdf" in client.get("/").text                              # waits in the inbox
    page = client.get("/kreditoren/neu?datei=inbox/papeterie.pdf").text
    assert "Papeterie Muster AG" in page and "86.40" in page
    to = ok(post(client, "/kreditoren/neu", {"lieferant": "neu", "s_name": "Papeterie Muster AG", "s_strasse": "Marktgasse",
                                             "s_nr": "14", "s_plz": "3011", "s_ort": "Bern", "s_land": "CH",
                                             "betrag": "86.40", "datum": "2026-03-03", "konto": "6500  Büromaterial",
                                             "iban": "CH4431999123000889012", "referenz_typ": "QRR",
                                             "referenz": client.get("/kreditoren/neu?datei=inbox/papeterie.pdf").text.split('name="referenz" value="')[1].split('"')[0],
                                             "datei": "inbox/papeterie.pdf"}))
    nr = to.rsplit("/", 1)[-1]
    assert not (root / "inbox" / "papeterie.pdf").exists()
    assert client.get(to).status_code == 200 and client.get("/kreditoren/lieferanten").status_code == 200
    ok(post(client, "/kreditoren/zahlungslauf", {"nr": [nr], "datum": "2026-03-20"}))
    zl = client.get("/kreditoren/zahlungen").text
    assert "angewiesen" in zl
    datei = zl.split('href="/datei/zahlungen/')[1].split('"')[0]
    assert b"pain.001.001.09" in client.get(f"/datei/zahlungen/{datei}").content
    ok(post(client, "/kreditoren/zahlungslauf/bezahlt", {"datei": datei, "datum": "2026-03-20"}))
    assert "bezahlt" in client.get(f"/kreditoren/rechnung/{nr}").text
    assert errors(root) == []


def test_bank_screen_import_and_book(client, root):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from camt_sample import entry, statement
    data = statement("CH9300762011623852957", "30000.00",
                     [(entry("7.50", "DBIT", "2026-03-31", ustrd="Kontoführung", acct_ref="F1"), "-7.50")],
                     "2026-03-01", "2026-03-31")
    ok(post(client, "/bank/import", files={"datei": ("maerz.xml", data, "application/xml")}))
    page = client.get("/bank").text
    tid = page.split('<article class="bankcard" id="karte-')[1].split('"')[0]
    assert "Kontoführung" in page and "Bankbewegung abgleichen" in client.get("/").text
    ok(post(client, f"/bank/{tid}/buchen", {"konto": "6940  Bankspesen", "text": "Kontoführung März", "mwst": ""}))
    assert "gebucht" in client.get("/bank/bewegungen?status=gebucht").text
    assert errors(root) == []


@pytest.fixture
def server(root, tmp_path):
    from allkvitt.web.auth import UserStore
    cfg = tmp_path / "cfg"
    store = UserStore(cfg)
    store.add("anna", "geheim-genug-1", "admin", "Anna Admin")
    store.add("bruno", "geheim-genug-2", "buchhaltung", "Bruno Buch")
    store.add("lea", "geheim-genug-3", "lesen", "Lea Lesen")
    app = create_app(root, auth_dir=cfg)
    with TestClient(app) as c:
        c.app_ui = app.state.ui
        yield c


def login(c, name, password):
    c.cookies.clear()
    r = c.post("/login", data={"name": name, "passwort": password}, follow_redirects=False)
    if r.status_code == 303:
        page = c.get("/").text
        c.csrf = page.split('"X-CSRF": "')[1].split('"')[0]
    return r


def test_server_login_roles_and_authorship(server, root):
    c = server
    assert c.get("/", follow_redirects=False).headers["location"].startswith("/login")
    assert c.get("/?t=anything", follow_redirects=False).status_code == 303          # no token back door
    assert login(c, "anna", "falsch").status_code == 401
    assert login(c, "bruno", "geheim-genug-2").status_code == 303
    r = post(c, "/journal/buchen", {"modus": "einfach", "datum": "2026-04-02", "betrag": "10", "text": "Test",
                                     "soll": "6500", "haben": "1020"})
    ok(r)
    assert gitlog.log(root, 1)[0]["autor"] == "Bruno Buch"
    r = post(c, "/einstellungen", {"firma": "X"})
    assert "Nur Admins" in r.text
    assert login(c, "lea", "geheim-genug-3").status_code == 303
    assert c.get("/journal").status_code == 200
    r = post(c, "/journal/buchen", {"modus": "einfach", "datum": "2026-04-02", "betrag": "1", "text": "x",
                                     "soll": "6500", "haben": "1020"})
    assert "Leserecht" in r.text
    assert c.post("/chat/send", data={"message": "hi"}, headers={"X-CSRF": c.csrf}).status_code == 403
    r = c.post("/journal/buchen", data={}, headers={"X-CSRF": "falsch"})
    assert r.status_code == 403


def test_login_throttle_and_password_change_ends_sessions(server):
    c = server
    for _ in range(5):
        login(c, "anna", "falsch")
    assert "Zu viele Fehlversuche" in login(c, "anna", "geheim-genug-1").text
    c.app_ui.throttle.failures.clear()
    assert login(c, "anna", "geheim-genug-1").status_code == 303
    assert c.get("/", follow_redirects=False).status_code == 200
    c.app_ui.users.set_password("anna", "ganz-neues-passwort")
    assert c.get("/", follow_redirects=False).status_code == 303                       # old session ended


def test_foreign_currency_screens(client, root, monkeypatch):
    from allkvitt import fx
    page = (b'<wechselkurse><datum>x</datum><devise code="eur"><waehrung>1 EUR</waehrung>'
            b'<kurs>0.95</kurs></devise></wechselkurse>')
    monkeypatch.setattr(fx, "_get", lambda url: page)
    ok(post(client, "/konten/neu", {"nr": "1021", "name": "Bank EUR", "klasse": "aktiv", "waehrung": "eur"}))
    assert "EUR" in client.get("/kurs?waehrung=EUR&datum=2026-03-02").text
    ok(post(client, "/journal/buchen", {"modus": "einfach", "datum": "2026-03-02", "text": "Verkauf DE",
                                         "soll": "1021", "haben": "3200", "betrag": "100", "waehrung": "EUR"}))
    assert "EUR 100.00" in client.get("/journal?jahr=2026&monat=3").text
    blatt = client.get("/konten/1021?jahr=2026").text
    assert "Saldo EUR" in blatt and "95.00" in blatt
    assert "Fremdwährungen per" in client.get("/abschluss?jahr=2026").text
    assert errors(root) == []


def test_unterlagen_downloads(client, root):
    page = client.get("/abschluss").text
    assert 'id="unterlagen"' in page and "/abschluss/unterlagen/journal?jahr=2026&format=csv" in page
    r = client.get("/abschluss/unterlagen/journal?jahr=2026&format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "filename*=UTF-8''Journal%202026.csv" in r.headers["content-disposition"]
    r = client.get("/abschluss/unterlagen/kontoblaetter?jahr=2026&format=pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "inline" in r.headers["content-disposition"]
    r = client.get("/abschluss/unterlagen.zip?jahr=2026&teil=journal&teil=belege&format=pdf")
    import io
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert r.status_code == 200 and any(n.endswith("05 Belegordner 2026.pdf") for n in names)
    assert not any(n.endswith(".csv") for n in names)
    assert client.get("/abschluss/unterlagen/journal?format=xlsx").status_code == 400


def test_buch_export(client, root, tmp_path):
    r = client.post("/einstellungen/buch-export", data={"historie": "1", "inbox": "1"}, headers={"X-CSRF": client.csrf})
    assert r.status_code == 200 and r.headers["content-type"] == "application/vnd.allkvitt+zip"
    (tmp_path / "x.allkvitt").write_bytes(r.content)
    from allkvitt import austausch
    assert austausch.inspect(tmp_path / "x.allkvitt")["firma"] == "Muster GmbH"
    r = client.post("/einstellungen/buch-export", data={"passwort": "a", "passwort2": "b"},
                    headers={"X-CSRF": client.csrf})
    assert r.status_code == 400
    assert client.post("/einstellungen/buch-export", data={}).status_code == 403     # no CSRF token


def test_top_navigation():
    from allkvitt.web.app import navigation
    counts = {"todo": 9, "bank": 7, "entwuerfe": 0, "einkauf": 2, "verkauf": 0, "vorschlaege": 1}
    pages = [{"url": "/p/leistungen/leistungen", "label": "Leistungen", "bereich": "debitoren"},
             {"url": "/p/leistungen/offerten", "label": "Offerten", "bereich": "leistungen"},
             {"url": "/p/anlagen/anlagen", "label": "Anlagen", "bereich": "abschluss"},
             {"url": "/p/x/seite", "label": "Fremd", "bereich": ""}]

    def at(path, methode="effektiv", status=""):
        nv = navigation(path, counts, pages, methode, status)
        return nv, {s["key"]: s for s in nv["bereiche"] if s}

    def on(nv):
        return [t["label"] for t in nv["aktiv"]["tabs"] if t.get("on")]

    nv, s = at("/")
    assert [k for k in s] == ["uebersicht", "einkauf", "verkauf", "bank", "lohn", "buchhaltung", "plugins"]
    assert s["uebersicht"]["on"] and s["uebersicht"]["badge"] == 9 and s["bank"]["badge"] == 7
    nv, s = at("/debitoren/kunden")
    assert nv["aktiv"]["key"] == "verkauf" and on(nv) == ["Kunden"]
    assert "Leistungen" not in [t["label"] for t in nv["aktiv"]["tabs"]]
    assert [t["label"] for t in s["plugins"]["tabs"]] == ["Leistungen", "Offerten", "Anlagen", "Fremd"]
    nv, _ = at("/debitoren/rechnung/R-2026-0001")
    assert on(nv) == ["Offen"]
    nv, _ = at("/kreditoren", status="entwurf")
    assert on(nv) == ["Entwürfe"] and nv["aktiv"]["tabs"][0]["badge"] == 2
    assert on(at("/kreditoren", status="storniert")[0]) == ["Alle"]
    assert on(at("/eingang/quittung")[0]) == ["Entwürfe"]
    for slug, label in (("/p/leistungen/leistungen", "Leistungen"), ("/p/leistungen/offerten", "Offerten"),
                        ("/p/anlagen/anlagen", "Anlagen"), ("/p/anlagen/anlagen/abschreiben", "Anlagen"),
                        ("/p/x/seite", "Fremd")):
        nv, _ = at(slug)
        assert nv["aktiv"]["key"] == "plugins" and on(nv) == [label]
    nv, _ = at("/abschluss")
    assert not any(t["label"] == "Anlagen" for t in nv["aktiv"]["tabs"])
    assert on(at("/lohn/lohnkonto/2026/M0001")[0]) == ["Mitarbeitende"]
    assert on(at("/bank/regeln")[0]) == ["Regeln"] and on(at("/bank")[0]) == ["Abgleichen"]
    nv, s = at("/mwst", "keine")
    assert nv["aktiv"]["key"] == "buchhaltung" and "MWST" not in [t["label"] for t in nv["aktiv"]["tabs"]]
    assert "MWST" in [t["label"] for t in at("/journal")[0]["aktiv"]["tabs"]]
    assert not any(s and s["key"] == "plugins" for s in navigation("/", counts, [])["bereiche"])
    nv = navigation("/p/anlagen/anlagen", counts, pages[2:3])
    assert nv["aktiv"]["key"] == "plugins" and nv["aktiv"]["tabs"][0]["on"]


def test_top_navigation_renders(client):
    page = client.get("/debitoren/kunden").text
    assert 'class="top"' in page and 'aria-label="Hauptnavigation"' in page
    assert 'class="tabsrow"' in page and 'href="/debitoren/offene-posten"' in page
    assert 'class="navlist"' not in page
    assert 'class="tabsrow"' not in client.get("/").text


def test_salary_bank_file_download(client, root):
    from allkvitt import api
    for nr in ('M0001', 'M0002'):
        api.employee_update(Book(root), nr, iban='CH9300762011623852957', ort='Bern', land='CH')
        ok(post(client, f'/lohn/abrechnung/2026-03/{nr}/abschliessen'))
    ok(post(client, '/lohn/zahlung/erstellen', {'monat': '2026-03', 'datum': '2026-03-25'}))
    page = client.get('/lohn?monat=2026-03')
    assert 'Bankdatei herunterladen' in page.text
    from allkvitt.payroll_payments import active
    info = active(Book(root), 2026, 3)
    r = client.get('/datei/' + info['datei'])
    assert r.status_code == 200 and b'pain.001.001.09' in r.content
    assert client.post('/lohn/zahlung/erstellen', data={'monat': '2026-03'}).status_code == 403


def test_estv_import_ui(client, root, monkeypatch):
    from allkvitt import qst_estv
    from test_payroll_review import records
    monkeypatch.setattr(qst_estv, 'download', lambda k, y: qst_estv.parse(records(k, y), k, y, 'fixture'))
    ok(post(client, '/lohn/qst-import', {'kanton': 'ZH', 'jahr': '2026'}))
    assert 'ZH 2026' in client.get('/lohn/mitarbeiter').text


def test_employee_can_clear_flat_withholding_rate(client, root):
    from allkvitt import api, payroll
    api.employee_update(Book(root), 'M0001', qst=None, qst_satz='0.05')
    ok(post(client, '/lohn/mitarbeiter/M0001', {'vorname': 'Lea', 'nachname': 'Muster',
                                              'lohnart': 'monat', 'qst_code': '', 'qst_satz_pct': ''}))
    assert payroll.employee(Book(root), 'M0001')['qst_satz'] == 0

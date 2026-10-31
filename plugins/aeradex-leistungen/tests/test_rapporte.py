"""Scanned paper reports: printed with number and QR, recognised in the Belegeingang, read (Claude replaced by a
fake), checked and taken over — Regie/Material into the customer's invoice draft, the Wochenrapport into working time
checked against the Regie hours."""
from decimal import Decimal

import pytest

from aeradex import api, check, invoices
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from aeradex_leistungen import abwesenheit, daten, entwuerfe, formulare, rapporte


def W(book, fn, *args, **kw):
    return api.write(Book(book.root), "test", fn, *args, **kw).get("ergebnis")


@pytest.fixture
def book(tmp_path):
    b = make_book(tmp_path, plugins=["leistungen"], strasse="Hauptstrasse", nr="1", plz="3600", ort="Thun",
                  iban="CH93 0076 2011 6238 5295 7")
    api.customer_add(b, name="Anna Beispiel", firma="Beispiel AG", strasse="Marktgasse", nr="5", plz="3011", ort="Bern")
    api.customer_add(Book(b.root), name="Peter Keller", strasse="Lindenweg", nr="3", plz="3600", ort="Thun")
    api.employee_add(Book(b.root), "Lea", "Muster", monatslohn=6000, pensum=100, vollzeit_stunden_woche=42,
                     eintritt="2026-01-01")
    api.employee_add(Book(b.root), "Tom", "Steiner", lohnart="stunde", stundenlohn=32, eintritt="2026-01-01")
    W(b, daten.set_person, "M0001", 0, 70)
    W(b, daten.add_product, "Regiearbeiten", "0", "h")
    W(b, daten.add_product, "Dispersionsfarbe weiss", "45", "l")
    W(b, daten.add_project, "K0001", "MFH Marktgasse", strasse="Marktgasse", hausnr="5", plz="3011", ort="Bern")
    return Book(b.root)


def png(seed: int) -> bytes:
    """A small real photo, different per seed (a scan is refused when the same file comes twice)."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (seed % 256, 120, 90)).save(buf, "PNG")
    return buf.getvalue()


def fake(monkeypatch, raw):
    seen = {}

    def reader(images, art, hints):
        seen.update(art=art, hints=hints, pages=len(images))
        return raw
    monkeypatch.setattr(rapporte, "READER", reader)
    return seen


REGIE = {"kopf": {"nr": "", "auftraggeber": "Beispiel AG", "adresse": "Marktgasse 5", "baustelle": "MFH Marktgasse"},
         "arbeiten": ["Wände abgedeckt", "Decke 2× gestrichen"],
         "stunden": [{"person": "Lea", "datum": "13.10.", "stunden": "7½", "ansatz": "", "unsicher": False},
                     {"person": "Tom Steiner", "datum": "2026-10-13", "stunden": "8", "ansatz": "", "unsicher": False},
                     {"person": "Tom Steiner", "datum": "2026-10-14", "stunden": "4:30", "ansatz": "", "unsicher": True}],
         "total_chf_auf_blatt": "", "unterschrift_firma": True, "unterschrift_auftraggeber": False, "hinweise": ""}


def _scan(book, rel):
    """A printed form as if it had been scanned: into the inbox."""
    return api.inbox_add(Book(book.root), "scan.pdf", (book.root / rel).read_bytes())["datei"]


def test_printed_regie_report_finds_its_way_into_the_invoice_draft(book, monkeypatch):
    printed = W(book, formulare.reports, "regie", "P:P0001", 2)
    year = __import__("datetime").date.today().year
    assert printed["nummern"] == [f"R-{year}-001", f"R-{year}-002"]
    nr = printed["nummern"][0]
    seen = fake(monkeypatch, REGIE)
    res = api.inbox_take(Book(book.root), _scan(book, printed["datei"]))      # no kind chosen: the QR says it
    assert res["art"] == "regie" and res["url"].endswith(nr) and seen["art"] == "regie"
    assert "Lea Muster" in seen["hints"]["personen"]
    r = rapporte.report(Book(book.root), nr)
    assert (r["kunde"], r["projekt"], r["status"]) == ("K0001", "P0001", "ausgelesen")
    assert [(s["person"], s["datum"], s["stunden"]) for s in r["stunden"]] == [
        ("M0001", rapporte._day("13.10.", year), "7.5"),
        ("M0002", "2026-10-13", "8"), ("M0002", "2026-10-14", "4.5")]
    assert not list((book.root / "inbox").glob("*.pdf"))                         # the scan moved to the report
    with pytest.raises(BookError, match="schon eingelesen"):
        rapporte.take_in(Book(book.root), book.root / _scan(book, printed["datei"]))
    # the person corrects the year and takes it over
    stunden = [{**s, "datum": s["datum"].replace(s["datum"][:4], "2026")} for s in r["stunden"]]
    W(book, rapporte.save, nr, {"stunden": stunden})
    res = W(book, rapporte.take_over, nr)
    assert res["entwurf"] == "RE-0001" and len(res["eintraege"]) == 3
    v = entwuerfe.view(Book(book.root), "RE-0001")
    assert v["rapporte"] == [nr] and not v["ausstellbar"]                       # Regiearbeiten has no price yet
    assert any("ohne Unterschrift" in w for w in v["warnungen"])
    e = daten.entry(Book(book.root), res["eintraege"][0])
    assert e["text"] == "Wände abgedeckt; Decke 2× gestrichen" and e["produkt"] == "P001" and e["projekt"] == "P0001"
    assert rapporte.report(Book(book.root), nr)["status"] == "uebernommen"
    assert_clean(Book(book.root))


def test_material_report_brings_new_material_into_the_range(book, monkeypatch):
    fake(monkeypatch, {"kopf": {"nr": "", "auftraggeber": "Peter Keller", "adresse": "Lindenweg 3, Thun", "baustelle": ""},
                       "zeilen": [{"datum": "2026-10-14", "material": "Dispersionsfarbe weiß", "einheit": "l",
                                   "menge": "3", "preis": "", "unsicher": False},
                                  {"datum": "2026-10-14", "material": "Abdeckvlies", "einheit": "Rolle", "menge": "1",
                                   "preis": "", "unsicher": False}],
                       "total_chf_auf_blatt": "", "unterschrift_firma": True, "unterschrift_auftraggeber": True,
                       "hinweise": ""})
    datei = api.inbox_add(Book(book.root), "material.png", png(48))["datei"]
    res = api.inbox_take(Book(book.root), datei, "material")
    nr = res["url"].split("nr=")[1]
    r = rapporte.report(Book(book.root), nr)
    assert nr.startswith("M-") and r["kunde"] == "K0002"
    assert [z["produkt"] for z in r["zeilen"]] == ["P002", ""]
    out = W(book, rapporte.take_over, nr)
    b = Book(book.root)
    vlies = next(p for p in daten.products(b).values() if p["text"] == "Abdeckvlies")
    assert vlies["einheit"] == "Rolle"
    v = entwuerfe.view(b, out["entwurf"])
    assert len(v["fehler"]) == 1 and "1 Eintrag ohne Preis" in v["fehler"][0]


def test_unreadable_scan_is_kept_for_typing_in(book, monkeypatch):
    def broken(images, art, hints):
        raise BookError("Zum Auslesen braucht es Zugang zur Claude API")
    monkeypatch.setattr(rapporte, "READER", broken)
    datei = api.inbox_add(Book(book.root), "r.png", png(49))["datei"]
    res = api.inbox_take(Book(book.root), datei, "regie")
    assert "Claude API" in res["meldung"]
    r = rapporte.report(Book(book.root), res["url"].split("nr=")[1])
    assert r["fehler"] and "Kunde bzw. Objekt wählen" in rapporte.problems(Book(book.root), r)


def test_week_report_is_working_time_and_meets_the_regie_hours(book, monkeypatch):
    fake(monkeypatch, {**REGIE, "stunden": [{"person": "Lea", "datum": "2026-10-13", "stunden": "7.5", "ansatz": "95",
                                             "unsicher": False}]})
    datei = api.inbox_add(Book(book.root), "regie.png", png(200))["datei"]
    regie_nr = api.inbox_take(Book(book.root), datei, "regie")["url"].split("nr=")[1]
    W(book, rapporte.save, regie_nr, {"kunde": "K0001", "projekt": "P0001"})
    regie = {"id": W(book, rapporte.take_over, regie_nr)["eintraege"][0]}
    printed = W(book, formulare.weeks, "2026-W42", ["M0001"])
    fake(monkeypatch, {"name": "Lea Muster", "kw": "42", "jahr": "2026", "total_auf_blatt": "", "visum": True,
                       "hinweise": "",
                       "tage": [{"wochentag": "Dienstag", "datum": "", "reisezeit": "0.5", "zeilen": [
                                    {"kunde_objekt": "MFH Marktgasse", "stunden": "8", "bemerkung": "", "unsicher": False}]},
                                {"wochentag": "Mittwoch", "datum": "", "reisezeit": "", "zeilen": [
                                    {"kunde_objekt": "Peter Keller", "stunden": "6", "bemerkung": "", "unsicher": False},
                                    {"kunde_objekt": "Intern", "stunden": "2", "bemerkung": "Magazin", "unsicher": False}]},
                                {"wochentag": "Donnerstag", "datum": "", "reisezeit": "", "zeilen": [
                                    {"kunde_objekt": "", "stunden": "8.5", "bemerkung": "Ferien", "unsicher": False}]}]})
    res = api.inbox_take(Book(book.root), _scan(book, printed["datei"]))
    nr = res["url"].split("nr=")[1]
    assert nr == "W-2026-KW42-M0001"
    r = rapporte.report(Book(book.root), nr)
    assert [t["datum"] for t in r["tage"]] == ["2026-10-13", "2026-10-14", "2026-10-15"]
    out = W(book, rapporte.take_over, nr)
    b = Book(book.root)
    r = rapporte.report(b, nr)
    assert r["abgleich"] == [{"datum": "2026-10-13", "kunde": "K0001", "projekt": "P0001", "woche": "8", "regie": "7.5",
                              "differenz": "0.5"}]
    assert [a["kunde"] for a in r["ohne_regie"]] == ["K0002"]                      # Keller had no Regie-Rapport
    entries = {e["id"]: e for e in daten.entries(b)}
    made = [entries[i] for i in out["eintraege"]]
    assert sorted((e["kategorie"] or e["kunde"], str(e["menge"])) for e in made) == [
        ("Intern", "2"), ("K0002", "6"), ("Reisezeit", "0.5")]
    assert regie["id"] not in out["eintraege"]                                     # not recorded twice
    assert [a["art"] for a in abwesenheit.items(b)] == ["Ferien"]
    row = next(z for z in __import__("aeradex_leistungen.woche_rapport", fromlist=["overview"]).overview(b, 2026, 42)
               if z["person"] == "M0001")
    assert row["status"] == "uebernommen" and row["reisezeit"] == Decimal("0.5") and row["abgleich"]
    assert_clean(b)


def test_plain_forms_carry_a_readable_qr(book):
    from aeradex import kreditoren
    printed = W(book, formulare.reports, "material", "K:K0002", 1)
    payloads = kreditoren._decode_images(book.root / printed["datei"])
    assert rapporte.parse_qr(payloads[0]) == {"art": "material", "nummer": printed["nummern"][0], "kunde": "K0002",
                                              "projekt": ""}
    again = W(book, formulare.reports, "material", "", 1)
    assert again["nummern"][0] != printed["nummern"][0]                            # a printed number is never reused


def test_report_pages(book, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    fake(monkeypatch, REGIE)
    datei = api.inbox_add(Book(book.root), "r.png", png(50))["datei"]
    nr = api.inbox_take(Book(book.root), datei, "regie")["url"].split("nr=")[1]
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get(f"/p/leistungen/rapporte?nr={nr}").text
        assert "So gelesen" in page and "Wände abgedeckt" in page and "Wochenrapporte KW" in page
        assert "Regie-Rapport" in c.get("/m/scannen").text
        form = {"nummer": nr, "ziel": "P:P0001", "arbeiten": "Wände abgedeckt",
                "s_person": ["M0001", "M0002"], "s_datum": ["2026-10-13", "2026-10-13"], "s_stunden": ["7.5", "8"],
                "s_ansatz": ["", ""]}
        r = c.post("/p/leistungen/rapporte/uebernehmen", data=form, headers=h)
        assert r.status_code in (200, 204), r.text
        assert "/p/leistungen/entwuerfe?nr=RE-0001" in r.headers.get("HX-Redirect", "")
        assert "Abgleich" in c.get("/p/leistungen/rapporte?status=").text
        r = c.post("/p/leistungen/rapporte/formulare", data={"art": "woche", "kw": "2026-W42"}, headers=h)
        assert r.status_code in (200, 204), r.text
        assert "Wochenrapporte" in r.headers.get("HX-Redirect", "")
    assert rapporte.report(Book(book.root), nr)["stunden"][0]["person"] == "M0001"


def test_a_rate_set_in_the_draft_is_remembered_and_fills_the_rest(book, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    fake(monkeypatch, {**REGIE, "stunden": [{**s, "datum": "2026-10-13" if s["datum"] == "13.10." else s["datum"]}
                                             for s in REGIE["stunden"]]})
    datei = api.inbox_add(Book(book.root), "r.png", png(51))["datei"]
    nr = api.inbox_take(Book(book.root), datei, "regie")["url"].split("nr=")[1]
    W(book, rapporte.save, nr, {"kunde": "K0001", "projekt": "P0001"})
    out = W(book, rapporte.take_over, nr)                    # Tom has no rate at all, Lea 0: both wait for one
    v = entwuerfe.view(Book(book.root), out["entwurf"])
    toms = [e for e in v["eintraege_offen"] if e["wer"] == "M0002"]
    assert len(toms) == 2 and all(e["preis_fehlt"] for e in toms)
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post("/p/leistungen/entwuerfe/preis", data={"nummer": out["entwurf"], "id": toms[0]["id"], "preis": "88",
                                                         "sortiment": "1"}, headers=h)
        assert r.status_code in (200, 204), r.text
    b = Book(book.root)
    assert daten.person(b, "M0002")["satz"] == Decimal("88.00")
    v = entwuerfe.view(b, out["entwurf"])
    assert [str(e["preis"]) for e in v["eintraege_offen"] if e["wer"] == "M0002"] == ["88.00", "88.00"]
    assert len([e for e in v["eintraege_offen"] if e["preis_fehlt"]]) == 1          # Lea still open


def test_review_findings(book, monkeypatch):
    """Object lines of a week, keywords vs. a chosen customer, QR payloads, half hours, one product per material."""
    from aeradex_leistungen import seiten, woche_rapport
    assert seiten._ziel(Book(book.root), "P:P0001") == {"kunde": "K0001", "projekt": "P0001", "kategorie": "",
                                                        "absenz": ""}
    b = Book(book.root)
    assert woche_rapport.kind_of(b, {"text": "Ferienwohnung Meier", "kunde": "K0002"}) == "kunde"
    assert woche_rapport.kind_of(b, {"text": "", "bemerkung": "Ferien"}) == "absenz"
    assert woche_rapport.kind_of(b, {"text": "MFH", "projekt": "P0001"}) == "kunde"
    assert rapporte.parse_qr("aeradex:rapport:regie:../../x") is None
    assert rapporte.parse_qr("aeradex:rapport:woche:2027-W05:../x") is None
    assert rapporte.parse_qr("aeradex:rapport:regie:R-2027-001:K0001:../x")["projekt"] == ""
    assert [rapporte.amount(x) for x in ("½", "7 ½", "7½", "0")] == ["0.5", "7.5", "7.5", "0"]
    fake(monkeypatch, {"kopf": {"nr": "", "auftraggeber": "Peter Keller", "adresse": "", "baustelle": ""},
                       "zeilen": [{"datum": "2026-10-14", "material": "Klebeband", "einheit": "Rolle", "menge": "1",
                                   "preis": "", "unsicher": False},
                                  {"datum": "2026-10-15", "material": "Klebeband", "einheit": "Rolle", "menge": "2",
                                   "preis": "", "unsicher": False}],
                       "total_chf_auf_blatt": "", "unterschrift_firma": True, "unterschrift_auftraggeber": True,
                       "hinweise": ""})
    datei = api.inbox_add(Book(book.root), "m.png", png(52))["datei"]
    nr = api.inbox_take(Book(book.root), datei, "material")["url"].split("nr=")[1]
    W(book, rapporte.take_over, nr)
    assert [p["text"] for p in daten.products(Book(book.root)).values()].count("Klebeband") == 1
    # a plugin never takes a file from outside the inbox
    with pytest.raises(BookError, match="nicht in der Inbox"):
        api.inbox_take(Book(book.root), "aeradex.yaml", "regie")


def test_regie_after_the_week_is_not_billed_twice(book, monkeypatch):
    fake(monkeypatch, {"name": "Lea Muster", "kw": "42", "jahr": "2026", "total_auf_blatt": "", "visum": True,
                       "hinweise": "", "tage": [{"wochentag": "Dienstag", "datum": "2026-10-13", "reisezeit": "", "zeilen": [
                           {"kunde_objekt": "MFH Marktgasse", "stunden": "8", "bemerkung": "", "unsicher": False}]}]})
    datei = api.inbox_add(Book(book.root), "w.png", png(53))["datei"]
    week = api.inbox_take(Book(book.root), datei, "woche")["url"].split("nr=")[1]
    W(book, rapporte.take_over, week)
    assert len(rapporte.report(Book(book.root), week)["ohne_regie"]) == 1
    fake(monkeypatch, {**REGIE, "stunden": [{"person": "Lea", "datum": "2026-10-13", "stunden": "7.5", "ansatz": "",
                                             "unsicher": False}]})
    datei = api.inbox_add(Book(book.root), "r.png", png(54))["datei"]
    regie = api.inbox_take(Book(book.root), datei, "regie")["url"].split("nr=")[1]
    W(book, rapporte.save, regie, {"kunde": "K0001", "projekt": "P0001"})
    W(book, rapporte.take_over, regie)
    b = Book(book.root)
    lea = [e for e in daten.entries(b) if e["wer"] == "M0001" and e["kunde"] == "K0001"]
    assert [str(e["menge"]) for e in lea] == ["7.5"]                         # the week's 8 h placeholder is gone
    w = rapporte.report(b, week)
    assert w["ohne_regie"] == [] and w["abgleich"][0]["differenz"] == "0.5"
    assert_clean(b)


def test_where_the_pages_live(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        verkauf = c.get("/debitoren").text
        tabs = verkauf[verkauf.index('class="subnav"') if 'class="subnav"' in verkauf else 0:]
        assert tabs.index(">Entwürfe<") < tabs.index("Rechnungsentwürfe") < tabs.index(">Offen<")
        assert "/p/leistungen/arbeitszeit" in c.get("/lohn").text
        kunde = c.get("/debitoren/kunden/K0001").text
        assert "Regie-Rapport" in kunde and "/p/leistungen/rapporte/formulare" in kunde
        assert 'value="K:K0002" selected' in c.get("/p/leistungen/rapporte?ziel=K:K0002").text

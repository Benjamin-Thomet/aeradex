"""End to end: the plugin syncs a throwaway book with an in-process portal; a simulated phone talks to the portal."""
import base64
import hashlib
import uuid as uuidlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from aeradex import api
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from aeradex_leistungen import abwesenheit, kontrolle
from aeradex_leistungen import daten as leistungen
from aeradex_team import abgleich, daten, krypto, uebernahme, verbindung
from aeradex_team_portal import auth
from aeradex_team_portal.app import Config, create_app

TOKEN = "t" * 40
H = {"x-aeradex": "1"}
TODAY = date.today()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


def W(book, fn, *args, **kw):
    return api.write(Book(book.root), "test", fn, *args, **kw).get("ergebnis")


@pytest.fixture
def portal(tmp_path):
    return create_app(Config(tmp_path / "portal", b"s" * 40, auth.sha256(TOKEN)))


@pytest.fixture
def send(portal):
    pc = TestClient(portal, base_url="https://testserver")
    pc.headers["authorization"] = f"Bearer {TOKEN}"

    def transport(method, path, body=None):
        r = pc.request(method, f"/app/api/sync{path}", json=body)
        if r.status_code >= 400:
            raise verbindung.PortalError(r.json().get("fehler"))
        return r.json()
    return transport


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("AERADEX_CONFIG", str(tmp_path / "config"))
    b = make_book(tmp_path, firma="Muster Malerei", jahr=TODAY.year, rechtsform="Einzelfirma",
                  plugins=["leistungen", "team"], eroeffnung={"1020": 10000, "2800": -10000},
                  strasse="Musterweg", nr="1", plz="3600", ort="Thun", iban="CH93 0076 2011 6238 5295 7")
    api.customer_add(b, name="Hans Huber", strasse="Dorfstrasse", nr="1", plz="3600", ort="Thun")
    api.employee_add(Book(b.root), "Anna", "Muster", monatslohn=5200, pensum=100, vollzeit_stunden_woche=42,
                     eintritt=f"{TODAY.year}-01-01")
    api.employee_add(Book(b.root), "Luca", "Beispiel", monatslohn=1100, pensum=100, vollzeit_stunden_woche=42,
                     eintritt=f"{TODAY.year}-01-01")
    W(b, leistungen.set_person, "M0001", 95)
    W(b, leistungen.set_person, "M0002", 60)
    W(b, leistungen.add_product, "Malerarbeiten", "95", "h")
    W(b, leistungen.add_product, "Dispersionsfarbe weiss", "18", "l")
    W(b, leistungen.add_project, "K0001", "Fassade Huber")
    return Book(b.root)


def phone_for(portal, benutzer, code, password="geheim-1234"):
    phone = TestClient(portal, base_url="https://testserver")
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    a = hashlib.pbkdf2_hmac("sha256", password.encode(), f"aeradex-auth:{benutzer}".encode(), 100_000, 32).hex()
    r = phone.post("/app/api/aktivieren", headers=H, json={"benutzer": benutzer, "code": code, "auth": a,
                                                            "pubkey": base64.b64encode(spki).decode(),
                                                            "wrapped": {"salt": "s", "iv": "i", "ct": "c", "iter": 1}})
    assert r.status_code == 200, r.text
    phone.private_key = priv
    return phone


def post(phone, kind, data, uid=None):
    uid = uid or str(uuidlib.uuid4())
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": kind, "data": data})
    assert r.status_code == 200, r.text
    return uid


def onboard(book, portal, send, person="M0001"):
    inv = abgleich.invite(book, person, send)
    assert inv["zugestellt"] and inv["benutzer"] == {"M0001": "anna", "M0002": "luca"}[person]
    return phone_for(portal, inv["benutzer"], inv["code"].lower())


def test_full_round_trip(book, portal, send):
    phone = onboard(book, portal, send)
    t = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "6.5", "projekt": "P0001", "leistung": "P001", "text": ""})
    i = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "1", "kategorie": "Intern", "text": "Werkstatt aufräumen"})
    m = post(phone, "material", {"datum": YESTERDAY, "projekt": "P0001", "produkt": "P002", "menge": "12"})
    k = post(phone, "abwesenheit", {"art": "Krank", "von": TODAY.isoformat()})
    f = post(phone, "abwesenheit", {"art": "Ferien", "von": (TODAY + timedelta(days=30)).isoformat(),
                                    "bis": (TODAY + timedelta(days=34)).isoformat(), "notiz": "Sommer"})

    out = abgleich.run(book, send)
    assert out["uebernommen"] == 5 and out["abgelehnt"] == []
    b = Book(book.root)
    rows = {e["id"]: e for e in leistungen.entries(b)}
    assert len(rows) == 3
    time_ = next(e for e in rows.values() if e["menge"] == Decimal("6.5"))
    assert time_["wer"] == "M0001" and time_["kunde"] == "K0001" and time_["abrechenbar"] and time_["preis"] == Decimal("95.00")
    assert time_["text"] == "Malerarbeiten"
    assert next(e for e in rows.values() if e["art"] == "Produkt")["menge"] == Decimal("12")
    assert next(e for e in rows.values() if e["kategorie"] == "Intern")["abrechenbar"] is False
    assert [a["art"] for a in abwesenheit.items(b)] == ["Krank"]
    reqs = daten.requests(b)
    assert [(r["art"], r["status"], r["uuid"]) for r in reqs] == [("Ferien", "offen", f)]
    assert set(daten.imported(b)) == {t, i, m, k, f}
    assert_clean(b)

    # the phone sees everything taken over, and its own balances
    me = phone.get("/app/api/ich").json()
    assert {e["status"] for e in me["eintraege"]} == {"uebernommen"}
    assert len(me["mein"]["eintraege"]) == 3
    want = kontrolle.month(b, TODAY.year, TODAY.month, "M0001")[0]
    konto = me["mein"]["konto"]
    assert Decimal(konto["monat"]["ist"]) == want["ist"]
    # the running month counts its target only up to today
    assert Decimal(konto["monat"]["soll"]) <= want["soll"]
    assert Decimal(konto["monat"]["saldo"]) == Decimal(konto["monat"]["ist"]) - Decimal(konto["monat"]["soll"])
    # fewer hours due means a higher balance: the year's balance rises by exactly the hours not yet due
    assert Decimal(konto["saldo_jahr"]) - want["saldo_jahr"] == want["soll"] - Decimal(konto["monat"]["soll"])
    assert me["mein"]["antraege"][0]["status"] == "offen"
    assert {p["nummer"] for p in me["shared"]["projekte"]} == {"P0001"}
    assert "preis" not in str(me["shared"]) and "95" not in str(me["shared"]["leistungen"])

    # a second run takes over nothing again
    again = abgleich.run(Book(book.root), send)
    assert again["uebernommen"] == 0 and len(leistungen.entries(Book(book.root))) == 3

    # the office approves the holiday request: it lands in the calendar, the app shows it
    W(book, daten.decide, reqs[0]["id"], True)
    abgleich.run(Book(book.root), send)
    b = Book(book.root)
    assert [a["art"] for a in abwesenheit.items(b)] == ["Krank", "Ferien"]
    me = phone.get("/app/api/ich").json()["mein"]
    assert me["antraege"][0]["status"] == "bewilligt"
    assert Decimal(me["konto"]["ferien"]["geplant"]) > 0


def test_refused_entries_come_back_with_a_reason(book, portal, send):
    phone = onboard(book, portal, send)
    bad = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "projekt": "P0099", "text": "x"})
    good = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "projekt": "P0001", "text": "Grundieren"})
    out = abgleich.run(book, send)
    assert out["uebernommen"] == 1 and [a["uuid"] for a in out["abgelehnt"]] == [bad]
    mine = {e["uuid"]: e for e in phone.get("/app/api/ich").json()["eintraege"]}
    assert mine[bad]["status"] == "abgelehnt" and "P0099" in mine[bad]["reason"]
    assert mine[good]["status"] == "uebernommen"
    # corrected on the phone, taken over on the next run
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "projekt": "P0001", "text": "x"}, bad)
    assert abgleich.run(Book(book.root), send)["uebernommen"] == 1
    assert len(leistungen.entries(Book(book.root))) == 2


def test_lost_ack_does_not_book_twice(book, portal, send):
    phone = onboard(book, portal, send)
    uid = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "3", "projekt": "P0001", "text": "Abdecken"})
    entries = send("GET", "/pending")["entries"]
    first = uebernahme.take_over(book, entries)           # booked, but the ack never reaches the portal
    second = uebernahme.take_over(Book(book.root), entries)
    assert first[0]["aeradex_id"] == second[0]["aeradex_id"]
    assert len(leistungen.entries(Book(book.root))) == 1
    assert uid in daten.imported(Book(book.root))


def test_blocked_account_entries_are_refused(book, portal, send):
    phone = onboard(book, portal, send)
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "3", "projekt": "P0001", "text": "Abdecken"})
    entries = send("GET", "/pending")["entries"]
    W(book, daten.set_account, "M0001", "", False)
    acks = uebernahme.take_over(Book(book.root), entries)
    assert acks[0]["status"] == "abgelehnt" and leistungen.entries(Book(book.root)) == []


def test_payslips_reach_only_their_owner_encrypted(book, portal, send):
    mel = onboard(book, portal, send, "M0001")
    sha = onboard(book, portal, send, "M0002")
    month = f"{TODAY.year}-{TODAY.month:02d}"
    api.payroll_run(Book(book.root), month)
    api.payslip_close(Book(book.root), month, "M0001")
    out = abgleich.run(Book(book.root), send)
    assert out["hochgeladen"] >= 1

    docs = mel.get("/app/api/dokumente").json()["docs"]
    lohn = next(d for d in docs if d["kind"] == "lohn")
    assert lohn["doc_id"] == f"lohn-{month}"
    env = mel.get(f"/app/api/dokumente/{lohn['doc_id']}").json()["doc"]
    plain = krypto.decrypt(mel.private_key, "M0001", lohn["doc_id"], env["envelope"])
    pdf = (book.root / "lohn" / str(TODAY.year) / f"{TODAY.month:02d}" / "M0001.pdf").read_bytes()
    assert plain == pdf
    with pytest.raises(Exception):   # someone else's key cannot open it
        krypto.decrypt(sha.private_key, "M0001", lohn["doc_id"], env["envelope"])
    assert not any(d["kind"] == "lohn" for d in sha.get("/app/api/dokumente").json()["docs"])

    # unchanged documents are not uploaded again
    assert abgleich.run(Book(book.root), send)["hochgeladen"] == 0

    # a new code: new key, documents encrypted again for it
    inv = abgleich.invite(Book(book.root), "M0001", send)
    mel2 = phone_for(portal, inv["benutzer"], inv["code"])
    assert mel2.get("/app/api/dokumente").json()["docs"] == []
    abgleich.run(Book(book.root), send)
    env = mel2.get(f"/app/api/dokumente/lohn-{month}").json()["doc"]
    assert krypto.decrypt(mel2.private_key, "M0001", f"lohn-{month}", env["envelope"]) == pdf


def test_team_page_renders(book, portal, send, monkeypatch):
    from aeradex.web.app import create_app as aeradex_app
    onboard(book, portal, send)
    ui = TestClient(aeradex_app(book.root, token="tok"))
    ui.get("/?t=tok")
    r = ui.get("/p/team/team")
    assert r.status_code == 200 and "Anna Muster" in r.text and "anna" in r.text


def test_hours_without_a_rate_are_taken_over_and_flagged(book, portal, send):
    W(book, leistungen.set_person, "", None, None, "Petra Muster")          # X01, no billing rate
    W(book, daten.set_account, "X01", "petra")
    inv = abgleich.invite(Book(book.root), "X01", send)
    phone = phone_for(portal, "petra", inv["code"])
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "4", "projekt": "P0001", "text": "Offerte besichtigt"})
    out = abgleich.run(Book(book.root), send)
    assert out["uebernommen"] == 1 and out["abgelehnt"] == []
    entry = leistungen.entries(Book(book.root))[0]
    assert entry["preis"] == Decimal("0.00") and entry["abrechenbar"]
    assert verbindung.state(Book(book.root))["ohne_satz"] == [entry["id"]]


def test_running_month_counts_target_only_until_today(book):
    from aeradex_team import abbild
    W(book, daten.set_account, "M0001", "anna")
    W(book, leistungen.set_holidays, None, f"{TODAY.year}-10-01")   # hours control starts in October
    today = date(TODAY.year, 10, 14)   # a Wednesday; Oct 1–14 has 10 workdays
    snap = abbild.build(Book(book.root), today)["people"]["M0001"]["konto"]
    assert Decimal(snap["monat"]["soll"]) == Decimal("8.40") * 10    # 42 h / 5 per workday, not the whole month
    assert Decimal(snap["monat"]["saldo"]) == -Decimal("84.00")
    assert Decimal(snap["saldo_jahr"]) == Decimal(snap["monat"]["saldo"])   # nothing recorded before October


# ---------- changing and deleting booked entries from the app ----------

def booked_item(phone, uuid):
    mein = phone.get("/app/api/ich").json()["mein"]
    for group in ("eintraege", "abwesenheiten", "antraege"):
        for item in mein.get(group) or []:
            if item.get("uuid") == uuid:
                return item
    return None


def test_employee_changes_and_deletes_booked_entries(book, portal, send):
    phone = onboard(book, portal, send)
    uid = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "6", "projekt": "P0001", "text": "Grundieren"})
    abgleich.run(book, send)
    item = booked_item(phone, uid)
    assert item["aenderbar"] and item["data"]["stunden"] == "6.00" and item["version"] == 1

    # changed on the phone: the booked entry is replaced, keeping one entry for this uuid
    post(phone, "zeit", {**item["data"], "stunden": "7.5", "text": "Grundieren und Abdecken"}, uid)
    abgleich.run(Book(book.root), send)
    rows = leistungen.entries(Book(book.root))
    assert [(e["menge"], e["text"]) for e in rows] == [(Decimal("7.5"), "Grundieren und Abdecken")]
    assert daten.imported(Book(book.root))[uid]["version"] == 2
    assert booked_item(phone, uid)["data"]["stunden"] == "7.50"

    # deleted on the phone: gone from the book and from the app
    r = phone.delete(f"/app/api/eintraege/{uid}", headers=H)
    assert r.status_code == 200, r.text
    abgleich.run(Book(book.root), send)
    assert leistungen.entries(Book(book.root)) == []
    assert daten.imported(Book(book.root))[uid]["geloescht"]
    me = phone.get("/app/api/ich").json()
    assert all(e["uuid"] != uid for e in me["eintraege"]) and booked_item(phone, uid) is None
    assert_clean(Book(book.root))


def test_closed_period_locks_the_app(book, portal, send):
    phone = onboard(book, portal, send)
    uid = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "6", "projekt": "P0001", "text": "Grundieren"})
    abgleich.run(book, send)
    W(book, daten.close_until, YESTERDAY)
    abgleich.run(Book(book.root), send)
    item = booked_item(phone, uid)
    assert not item["aenderbar"] and item["gesperrt"] == "abgeschlossen"
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": {**item["data"], "stunden": "8"}})
    assert r.status_code == 400 and "abgeschlossen" in r.json()["fehler"]
    assert phone.delete(f"/app/api/eintraege/{uid}", headers=H).status_code == 400
    # a new entry into the closed period is refused by the book, with the reason in the app
    late = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "1", "projekt": "P0001", "text": "Nachtrag"})
    out = abgleich.run(Book(book.root), send)
    assert [a["uuid"] for a in out["abgelehnt"]] == [late]
    mine = {e["uuid"]: e for e in phone.get("/app/api/ich").json()["eintraege"]}
    assert "abgeschlossen" in mine[late]["reason"]
    assert len(leistungen.entries(Book(book.root))) == 1


def test_change_that_arrives_after_closing_is_refused_by_the_book(book, portal, send):
    phone = onboard(book, portal, send)
    uid = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "6", "projekt": "P0001", "text": "Grundieren"})
    abgleich.run(book, send)
    item = booked_item(phone, uid)
    post(phone, "zeit", {**item["data"], "stunden": "9"}, uid)      # accepted: the phone still saw it open
    W(book, daten.close_until, YESTERDAY)                            # the office closes before the next sync
    out = abgleich.run(Book(book.root), send)
    assert out["abgelehnt"][0]["uuid"] == uid and "abgeschlossen" in out["abgelehnt"][0]["reason"]
    assert [e["menge"] for e in leistungen.entries(Book(book.root))] == [Decimal("6")]


def test_invoiced_entries_are_locked(book, portal, send):
    from aeradex_leistungen import abrechnung
    phone = onboard(book, portal, send)
    uid = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "projekt": "P0001", "text": "Grundieren"})
    abgleich.run(book, send)
    eid = leistungen.entries(Book(book.root))[0]["id"]
    W(book, abrechnung.bill, [eid])
    abgleich.run(Book(book.root), send)
    assert booked_item(phone, uid)["gesperrt"] == "verrechnet"


def test_requests_change_until_decided_and_sick_days_can_be_deleted(book, portal, send):
    phone = onboard(book, portal, send)
    later = (TODAY + timedelta(days=40)).isoformat()
    f = post(phone, "abwesenheit", {"art": "Ferien", "von": later, "bis": later})
    k = post(phone, "abwesenheit", {"art": "Krank", "von": YESTERDAY})
    abgleich.run(book, send)
    req = booked_item(phone, f)
    post(phone, "abwesenheit", {**req["data"], "bis": (TODAY + timedelta(days=44)).isoformat()}, f)
    abgleich.run(Book(book.root), send)
    r = daten.requests(Book(book.root))
    assert len(r) == 1 and r[0]["bis"] == TODAY + timedelta(days=44) and r[0]["id"] == "ANT-0001"
    W(book, daten.decide, "ANT-0001", True)
    abgleich.run(Book(book.root), send)
    assert not booked_item(phone, f)["aenderbar"]
    assert phone.delete(f"/app/api/eintraege/{f}", headers=H).status_code == 400
    # the sick day reported by mistake is withdrawn
    assert phone.delete(f"/app/api/eintraege/{k}", headers=H).status_code == 200
    abgleich.run(Book(book.root), send)
    assert [a["art"] for a in abwesenheit.items(Book(book.root))] == ["Ferien"]


# ---------- The office's side: overview, checking, closing, Pensum ----------

def test_check_list_flags_and_marking(book, portal, send):
    from aeradex_team import uebersicht
    phone = onboard(book, portal, send)
    sat = TODAY - timedelta(days=(TODAY.weekday() - 5) % 7 or 7)            # last Saturday
    a = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "9", "projekt": "P0001", "text": "Fassade"})
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2.5", "kategorie": "Werkstatt", "text": "Aufräumen"})
    post(phone, "zeit", {"datum": sat.isoformat(), "stunden": "3", "projekt": "P0001", "text": "Notfall"})
    def add_category(b):
        cfg = leistungen.settings(b)
        cfg["kategorien"].append("Werkstatt")
        return None, [leistungen.save_settings(b, cfg)]
    W(book, add_category)
    abgleich.run(Book(book.root), send)
    items = uebersicht.check_list(Book(book.root))
    assert len(items) == 3
    flags = {i["text"]: i["flags"] for i in items}
    assert any("11,50 h" in f for f in flags["Fassade"]) and "Wochenende" in flags["Notfall"]

    first = items[0]["id"]
    W(book, uebersicht.mark, [first])
    assert len(uebersicht.check_list(Book(book.root))) == 2
    # changed on the phone after checking: back on the list, marked as changed
    item = booked_item(phone, daten.by_id(Book(book.root))[first])
    post(phone, item["kind"], {**item["data"], "text": item["data"]["text"] + " (korr.)"}, item["uuid"])
    abgleich.run(Book(book.root), send)
    again = uebersicht.check_list(Book(book.root))
    assert len(again) == 3 and any(i["geaendert"] for i in again)
    assert a in daten.imported(Book(book.root))


def test_team_overview_and_page_actions(book, portal, send, monkeypatch):
    from aeradex.web.app import create_app as aeradex_app
    from aeradex_team import uebersicht
    phone = onboard(book, portal, send)
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "8", "projekt": "P0001", "text": "Fassade"})
    abgleich.run(book, send)
    monkeypatch.setattr(verbindung, "transport", lambda: send)
    rows = {r["nummer"]: r for r in uebersicht.team(Book(book.root))}
    assert rows["M0001"]["app"] == "aktiv" and rows["M0001"]["pensum"]["wert"] == Decimal(100)
    assert rows["M0001"]["zu_pruefen"] == 1 and rows["M0002"]["app"] == "kein Konto"

    app = aeradex_app(book.root, token="tok")
    ui = TestClient(app)
    ui.get("/?t=tok")
    hdr = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
    page = ui.get("/p/team/team")
    assert page.status_code == 200 and "Zu prüfen (1)" in page.text and "Anna Muster" in page.text
    r = ui.post("/p/team/team/pensum", data={"wer": "M0001", "ab": f"{TODAY.year}-07-01", "wert": "80"}, headers=hdr)
    assert r.status_code in (200, 204), r.text
    r = ui.post("/p/team/team/geprueft", data={"alle": "1"}, headers=hdr)
    assert r.status_code in (200, 204), r.text
    r = ui.post("/p/team/team/abschliessen", data={"bis": YESTERDAY}, headers=hdr)
    assert r.status_code in (200, 204), r.text
    b = Book(book.root)
    assert daten.closed_until(b).isoformat() == YESTERDAY and uebersicht.check_list(b) == []
    assert uebersicht.team(b)[0]["pensum"]["verlauf"][-1]["wert"] == Decimal(80)
    item = next(i for i in phone.get("/app/api/ich").json()["mein"]["eintraege"] if i.get("uuid"))
    assert item["gesperrt"] == "abgeschlossen"   # the page pushed the closing to the portal right away


# ---------- recording per customer (projects optional) ----------

def test_entries_per_customer_without_project(book, portal, send):
    api.customer_add(Book(book.root), name="Erika Muster", firma="Muster AG", strasse="Seeweg", nr="2", plz="3600", ort="Thun")
    phone = onboard(book, portal, send)
    abgleich.run(Book(book.root), send)
    shared = phone.get("/app/api/ich").json()["shared"]
    assert {k["nummer"]: k["name"] for k in shared["kunden"]} == {"K0001": "Hans Huber", "K0002": "Muster AG"}
    assert "Seeweg" not in str(shared) and "Dorfstrasse" not in str(shared)       # names only, no addresses
    assert shared["projekte"][0]["kunde_nr"] == "K0001"

    t = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "4", "kunde": "K0002", "leistung": "P001", "text": ""})
    m = post(phone, "material", {"datum": YESTERDAY, "kunde": "K0002", "produkt": "P002", "menge": "5"})
    p = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "kunde": "K0001", "projekt": "P0001", "text": "Fassade"})
    wrong = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "1", "kunde": "K0002", "projekt": "P0001", "text": "x"})
    out = abgleich.run(Book(book.root), send)
    assert [a["uuid"] for a in out["abgelehnt"]] == [wrong]
    rows = {e["id"]: e for e in leistungen.entries(Book(book.root))}
    by_uuid = {u: rows[i["id"]] for u, i in daten.imported(Book(book.root)).items() if i["id"] in rows}
    assert by_uuid[t]["kunde"] == "K0002" and by_uuid[t]["projekt"] == "" and by_uuid[t]["abrechenbar"]
    assert by_uuid[t]["preis"] == Decimal("95.00")                                # rate of the person
    assert by_uuid[m]["kunde"] == "K0002" and by_uuid[m]["menge"] == Decimal("5")
    assert by_uuid[p]["kunde"] == "K0001" and by_uuid[p]["projekt"] == "P0001"
    # the phone shows the customer, and can change the entry with the customer prefilled
    item = booked_item(phone, t)
    assert item["was"] == "Muster AG" and item["data"]["kunde"] == "K0002"
    assert_clean(Book(book.root))


def test_portal_requires_customer_or_category(book, portal, send):
    phone = onboard(book, portal, send)
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": str(uuidlib.uuid4()), "kind": "zeit",
                                                          "data": {"datum": YESTERDAY, "stunden": "2", "text": "x"}})
    assert r.status_code == 400 and "Kunde" in r.json()["fehler"]
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": str(uuidlib.uuid4()), "kind": "material",
                                                          "data": {"datum": YESTERDAY, "produkt": "P002", "menge": "1"}})
    assert r.status_code == 400 and "Kunde" in r.json()["fehler"]


def test_office_entries_can_be_changed_by_their_person(book, portal, send):
    from aeradex_team import uebersicht
    phone = onboard(book, portal, send)
    office = W(book, leistungen.add_time, YESTERDAY, "M0001", "8", "Fassade (Büro erfasst)", "", "P0001")["id"]
    other = W(book, leistungen.add_time, YESTERDAY, "M0002", "3", "Luca", "", "P0001")["id"]
    abgleich.run(Book(book.root), send)
    b = Book(book.root)
    mine = [e for e in phone.get("/app/api/ich").json()["mein"]["eintraege"] if e["id"] == office][0]
    assert mine["aenderbar"] and mine["uuid"] and mine["version"] == 0
    assert other not in daten.by_id(b)                      # M0002 has no app account: nothing exposed
    assert uebersicht.check_list(b) == []                   # the office's own entry is not "to check"

    post(phone, "zeit", {**mine["data"], "stunden": "7"}, mine["uuid"])
    abgleich.run(Book(book.root), send)
    b = Book(book.root)
    assert [e["menge"] for e in leistungen.entries(b) if e["wer"] == "M0001"] == [Decimal("7")]
    checks = uebersicht.check_list(b)
    assert len(checks) == 1 and checks[0]["geaendert"]
    # a second sync does not adopt anything twice
    before = len(daten.imported(b))
    abgleich.run(b, send)
    assert len(daten.imported(Book(book.root))) == before


def test_office_entry_that_changed_owner_is_refused(book, portal, send):
    phone = onboard(book, portal, send)
    office = W(book, leistungen.add_time, YESTERDAY, "M0001", "8", "Fassade", "", "P0001")["id"]
    abgleich.run(Book(book.root), send)
    mine = [e for e in phone.get("/app/api/ich").json()["mein"]["eintraege"] if e["id"] == office][0]
    post(phone, "zeit", {**mine["data"], "stunden": "6"}, mine["uuid"])
    # meanwhile the office deletes it and records the day for someone else: the freed id is reused
    W(book, leistungen.delete_entry, office)
    assert W(book, leistungen.add_time, YESTERDAY, "M0002", "8", "Fassade", "", "P0001")["id"] == office
    out = abgleich.run(Book(book.root), send)
    assert out["abgelehnt"] and "nicht (mehr) dir" in out["abgelehnt"][0]["reason"]
    assert [e["wer"] for e in leistungen.entries(Book(book.root))] == ["M0002"]


# ---------- spelling and style ----------

def test_text_correction_with_guards(book, portal, send, monkeypatch):
    from aeradex_team import korrektur, uebersicht
    seen = []

    def fake(b, texts):
        seen.append(dict(texts))
        out = {}
        for k, v in texts.items():
            if "farbe" in v:
                out[k] = "Fassadenfarbe 3 Kessel"            # a changed number: must be refused
            elif "gestricen" in v:
                out[k] = "Küche gestrichen, 2. Anstrich"
            else:
                out[k] = v
        return out
    monkeypatch.setattr(korrektur, "ask", fake)
    phone = onboard(book, portal, send)
    a = post(phone, "zeit", {"datum": YESTERDAY, "stunden": "4", "kunde": "K0001", "text": "kuche gestricen 2. anstrich"})
    b_ = post(phone, "material", {"datum": YESTERDAY, "kunde": "K0001", "produkt": "P002", "menge": "2", "text": "farbe 2 kessel"})
    abgleich.run(Book(book.root), send)
    assert len(seen) == 1 and set(seen[0]) == {a, b_}                       # one call for the whole sync
    b = Book(book.root)
    rows = {daten.imported(b)[u]["id"]: u for u in (a, b_)}
    texts = {rows[e["id"]]: e["text"] for e in leistungen.entries(b) if e["id"] in rows}
    assert texts[a] == "Küche gestrichen, 2. Anstrich" and texts[b_] == "farbe 2 kessel"
    assert daten.imported(b)[a]["original_text"] == "kuche gestricen 2. anstrich"
    item = next(i for i in uebersicht.check_list(b) if i["text"].startswith("Küche"))
    assert item["original"] == "kuche gestricen 2. anstrich"
    assert booked_item(phone, a)["text"] == "Küche gestrichen, 2. Anstrich"   # the phone shows the corrected text


def test_fixed_service_hides_the_choice_and_is_filled_in(book, portal, send):
    """«Always Regiearbeiten»: the app gets no list to choose from, every hour for a customer is booked as it."""
    W(book, leistungen.add_product, "Regiearbeiten", "88", "h")
    W(book, daten.set_fixed_service, "P003")
    phone = onboard(book, portal, send)
    abgleich.run(Book(book.root), send)
    assert phone.get("/app/api/ich").json()["shared"]["leistungen"] == []
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "kunde": "K0001", "text": "Türen lackiert"})
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "1", "kategorie": "Intern", "text": "aufgeräumt"})
    abgleich.run(Book(book.root), send)
    by_text = {e["text"]: e for e in leistungen.entries(Book(book.root))}
    assert by_text["Türen lackiert"]["produkt"] == "P003"
    assert not by_text["aufgeräumt"]["produkt"]                                  # internal hours stay without
    with pytest.raises(BookError):
        W(Book(book.root), daten.set_fixed_service, "P999")
    W(Book(book.root), daten.set_fixed_service, "")
    abgleich.run(Book(book.root), send)
    assert phone.get("/app/api/ich").json()["shared"]["leistungen"]               # the choice is back


def test_text_position_for_material_not_in_the_catalog(book, portal, send):
    """«Anderes (eigener Text)»: booked as the book's «Textposition» at price 0, flagged for the office."""
    from aeradex_team import uebersicht
    phone = onboard(book, portal, send)
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": str(uuidlib.uuid4()), "kind": "material",
                   "data": {"datum": YESTERDAY, "kunde": "K0001", "produkt": "TEXT", "menge": "2"}})
    assert r.status_code == 400 and "Beschreiben" in r.text                     # the text is required
    u = post(phone, "material", {"datum": YESTERDAY, "kunde": "K0001", "produkt": "TEXT", "menge": "2",
                                 "text": "Silikon weiss"})
    abgleich.run(Book(book.root), send)
    b = Book(book.root)
    nr = next(n for n, p in leistungen.materials(b).items() if p["text"] == "Textposition")
    e = next(e for e in leistungen.entries(b) if e["produkt"] == nr)
    assert e["text"] == "Silikon weiss" and e["menge"] == 2 and not e["preis"]
    assert "ohne Preis" in next(i for i in uebersicht.check_list(b) if i["id"] == e["id"])["flags"]
    shared = phone.get("/app/api/ich").json()["shared"]
    assert all(m["text"] != "Textposition" for m in shared["material"])        # the app shows «Anderes» instead
    assert booked_item(phone, u)["data"]["produkt"] == "TEXT"                   # and can change it as such


def test_text_correction_can_be_switched_off(book, portal, send, monkeypatch):
    from aeradex_team import korrektur
    monkeypatch.setattr(korrektur, "ask", lambda b, t: {k: "GEÄNDERT" for k in t})
    W(book, daten.set_text_correction, False)
    phone = onboard(book, portal, send)
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "1", "kunde": "K0001", "text": "wand gestrichen"})
    abgleich.run(Book(book.root), send)
    assert [e["text"] for e in leistungen.entries(Book(book.root))] == ["wand gestrichen"]


def test_text_correction_over_the_claude_api(book, monkeypatch):
    """The texts come before the instruction (instruction right before the JSON made Haiku answer «{}»);
    Sonnet by default. Never calls the real API here."""
    import sys
    import types
    from aeradex_team import korrektur
    seen = {}

    class Messages:
        def create(self, **kw):
            seen.update(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text='{"a": "Wand gestrichen"}')])

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: types.SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(korrektur, "backend", lambda b: "api")
    assert korrektur.real_ask(book, {"a": "wand gestricen"}) == {"a": "Wand gestrichen"}
    prompt = seen["messages"][0]["content"]
    assert prompt.index("wand gestricen") < prompt.index("Antworte NUR")
    assert seen["model"] == "claude-sonnet-5-5"


def test_accept_guards():
    from aeradex_team.korrektur import accept
    assert accept("wand gestricen", "Wand gestrichen") == "Wand gestrichen"
    assert accept("2 kessel farbe", "3 Kessel Farbe") == "2 kessel farbe"          # number changed
    assert accept("decke weiss", "Decke weiss gestrichen und danach sorgfältig gereinigt, Abdeckmaterial entfernt") == "decke weiss"
    assert accept("strasse", "Straße") == "Strasse"                                # never ß
    assert accept("text", None) == "text" and accept("text", "") == "text"


def test_text_correction_over_an_openai_compatible_endpoint(book, monkeypatch):
    """On the VPS the correction runs on Gemini (AERADEX_OPENAI_*): one plain HTTP call, JSON back."""
    import io
    import json as _json
    import urllib.request
    from aeradex_team import korrektur
    monkeypatch.setenv("AERADEX_OPENAI_BASE_URL", "https://example.invalid/v1beta/openai/")
    monkeypatch.setenv("AERADEX_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AERADEX_OPENAI_MODEL", "gemini-test")
    monkeypatch.setenv("AERADEX_CHAT_BACKEND", "openai")
    sent = []

    def fake_urlopen(req, timeout=0):
        sent.append(_json.loads(req.data))
        body = {"choices": [{"message": {"content": '```json\n{"a": "Wand gestrichen"}\n```'}}]}
        return io.BytesIO(_json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(korrektur, "ask", korrektur.real_ask)
    assert korrektur.backend(Book(book.root)) == "openai"
    assert korrektur.polish(Book(book.root), {"a": "wand gestricen"}) == {"a": "Wand gestrichen"}
    assert sent[0]["model"] == "gemini-test" and "wand gestricen" in sent[0]["messages"][0]["content"]


def test_objects_are_required_and_one_description_per_day_is_enough(book, portal, send):
    """The office's way: customers with Objekte need one chosen; two people on one job, one of them describes the day;
    the invoice shows the hours in total and the days' descriptions; a day nobody described is flagged."""
    from aeradex_leistungen import abrechnung
    from aeradex_team import uebersicht
    W(book, leistungen.add_product, "Regiearbeiten", "88", "h")
    W(book, daten.set_fixed_service, "P003")
    W(book, leistungen.set_options, abrechnen_je="projekt", projekt_wort="Objekt", projekt_pflicht="1", rechnung_tage="1")
    anna = onboard(book, portal, send)
    luca = onboard(book, portal, send, "M0002")
    abgleich.run(Book(book.root), send)
    shared = anna.get("/app/api/ich").json()["shared"]
    assert (shared["projekt_wort"], shared["projekt_pflicht"], shared["rechnung_tage"]) == ("Objekt", True, True)

    def refused(phone, kind, data):
        r = phone.post("/app/api/eintraege", headers=H, json={"uuid": str(uuidlib.uuid4()), "kind": kind, "data": data})
        assert r.status_code == 400
        return r.json()["fehler"]
    assert "Objekt wählen" in refused(anna, "zeit", {"datum": YESTERDAY, "stunden": "2", "kunde": "K0001", "text": "x"})
    assert "Objekt wählen" in refused(anna, "material", {"datum": YESTERDAY, "kunde": "K0001", "produkt": "P002", "menge": "1"})
    two_days_ago = (TODAY - timedelta(days=2)).isoformat()
    post(anna, "zeit", {"datum": YESTERDAY, "stunden": "8", "kunde": "K0001", "projekt": "P0001", "text": "Fassade grundiert"})
    post(luca, "zeit", {"datum": YESTERDAY, "stunden": "8", "kunde": "K0001", "projekt": "P0001", "text": ""})
    post(luca, "zeit", {"datum": two_days_ago, "stunden": "3", "kunde": "K0001", "projekt": "P0001", "text": ""})
    out = abgleich.run(Book(book.root), send)
    assert not out["abgelehnt"]
    b = Book(book.root)
    flagged = {i["datum"].isoformat(): i["flags"] for i in uebersicht.check_list(b) if i["art"] == "Zeit"}
    assert "Tag ohne Beschreibung" in flagged[two_days_ago]
    assert "Tag ohne Beschreibung" not in flagged[YESTERDAY]
    items = abrechnung.select(b, "K0001", "P0001")
    pos = abrechnung.preview(b, [e["id"] for e in items])["positionen"]
    assert len(pos) == 1 and pos[0]["menge"] == Decimal("19")
    day = date.fromisoformat(YESTERDAY).strftime("%d.%m.")
    assert pos[0]["text"].startswith("Regiearbeiten, Fassade Huber (") and pos[0]["text"].endswith(f"\n{day} Fassade grundiert")
    assert "Anna" not in pos[0]["text"] and "Luca" not in pos[0]["text"]
    assert_clean(b)


def test_documents_use_the_portals_kennung(book, tmp_path, monkeypatch):
    """An installation with its own Kennung: the book learns it from the portal and encrypts so its phones can read."""
    from aeradex_team_portal.testing import activate
    portal = create_app(Config(tmp_path / "p2", b"s" * 40, auth.sha256(TOKEN), kennung="muster"))
    pc = TestClient(portal, base_url="https://testserver")
    pc.headers["authorization"] = f"Bearer {TOKEN}"

    def send(method, path, body=None):
        return pc.request(method, f"/app/api/sync{path}", json=body).json()
    inv = abgleich.invite(book, "M0001", send)
    phone = activate(portal, inv["benutzer"], inv["code"], kennung="muster")
    month = f"{TODAY.year}-{TODAY.month:02d}"
    api.payroll_run(Book(book.root), month)
    api.payslip_close(Book(book.root), month, "M0001")
    abgleich.run(Book(book.root), send)
    env = phone.get(f"/app/api/dokumente/lohn-{month}").json()["doc"]["envelope"]
    pdf = (book.root / "lohn" / str(TODAY.year) / f"{TODAY.month:02d}" / "M0001.pdf").read_bytes()
    assert krypto.decrypt(phone.private_key, "M0001", f"lohn-{month}", env, "muster") == pdf
    with pytest.raises(Exception):
        krypto.decrypt(phone.private_key, "M0001", f"lohn-{month}", env)        # another Kennung: no key


def test_trade_texts_reach_the_app_and_the_correction(book, portal, send, monkeypatch, tmp_path):
    from aeradex.files import write_yaml
    from aeradex_team import korrektur
    write_yaml(book.root / "team" / "einstellungen.yaml",
               {"branche": "Schreinerei", "text_beispiel": "Türe eingepasst", "arztzeugnis_ab_tag": 3})
    phone = onboard(Book(book.root), portal, send)
    abgleich.run(Book(book.root), send)
    shared = phone.get("/app/api/ich").json()["shared"]
    assert shared["text_beispiel"] == "Türe eingepasst" and shared["arztzeugnis_ab_tag"] == 3
    prompts = []
    monkeypatch.setattr(korrektur, "backend", lambda b: "claude-code")
    monkeypatch.setitem(korrektur.ASK, "claude-code", lambda prompt, cwd: prompts.append(prompt) or '{"a": "Tür"}')
    korrektur.real_ask(Book(book.root), {"a": "tür"})
    assert "(Branche: Schreinerei)" in prompts[0] and "«Türe eingepasst»" in prompts[0]
    assert daten.texts(make_book(tmp_path / "andere", firma="X")) == {
        "branche": "Handwerksbetrieb", "text_beispiel": "", "arztzeugnis_ab_tag": 0}


def test_app_entries_are_authored_under_the_portals_host(book, portal, send, monkeypatch):
    verbindung.save_config("https://www.muster.example", "tok")
    assert verbindung.mail_domain() == "app.muster.example"
    phone = onboard(book, portal, send)
    post(phone, "zeit", {"datum": YESTERDAY, "stunden": "2", "projekt": "P0001", "text": "Abdecken"})
    from aeradex import gitlog
    authors = []
    real = uebernahme._book_it
    monkeypatch.setattr(uebernahme, "_book_it", lambda *a: authors.append(gitlog.AUTHOR.get()) or real(*a))
    abgleich.run(Book(book.root), send)
    assert authors == ["Anna Muster (App) <m0001@app.muster.example>"]

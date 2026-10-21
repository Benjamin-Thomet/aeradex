"""Users in the browser (server mode): invitations up to one's own role, one-time links, password change and
reset, roles and removal by admins, the user log."""
from __future__ import annotations

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from aeradex import gitlog  # noqa: E402
from aeradex.testing import make_book  # noqa: E402
from aeradex.web.app import create_app  # noqa: E402
from aeradex.web.auth import Invites, UserStore, may_grant  # noqa: E402


@pytest.fixture
def server(tmp_path):
    book = make_book(tmp_path, git=True)
    cfg = tmp_path / "cfg"
    store = UserStore(cfg)
    store.add("anna", "geheim-genug-1", "admin", "Anna Admin")
    store.add("bruno", "geheim-genug-2", "buchhaltung", "Bruno Buch")
    store.add("lea", "geheim-genug-3", "lesen", "Lea Lesen")
    app = create_app(book.root, auth_dir=cfg)
    with TestClient(app, base_url="https://dtmg.example.ch") as c:
        c.ui, c.root = app.state.ui, book.root
        yield c


def login(c, name, password):
    c.cookies.clear()
    r = c.post("/login", data={"name": name, "passwort": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    c.csrf = c.get("/").text.split('"X-CSRF": "')[1].split('"')[0]


def post(c, path, data):
    return c.post(path, data=data, headers={"X-CSRF": c.csrf, "HX-Request": "true"})


def link_of(html):
    return html.split('value="')[1].split('"')[0]


def test_roles_rank():
    assert may_grant("buchhaltung", "lesen") and may_grant("buchhaltung", "buchhaltung")
    assert not may_grant("buchhaltung", "admin") and not may_grant("lesen", "buchhaltung")


def test_reader_invites_a_reader_who_joins_with_a_one_time_link(server):
    c = server
    login(c, "lea", "geheim-genug-3")
    assert "Person einladen" in c.get("/benutzer").text
    assert "höchstens" in post(c, "/benutzer/einladen", {"rolle": "buchhaltung"}).text
    r = post(c, "/benutzer/einladen", {"rolle": "lesen", "notiz": "Tom Treuhand"})
    url = link_of(r.text)
    assert url.startswith("https://dtmg.example.ch/einladung/")
    path = url.split("dtmg.example.ch")[1]
    c.cookies.clear()
    page = c.get(path).text
    assert "Lea Lesen lädt dich" in page and 'value="tom"' in page
    r = c.post(path, data={"name": "tom", "anzeige": "Tom Treuhand", "passwort": "kurz", "passwort2": "kurz"})
    assert r.status_code == 400 and "mindestens 10" in r.text
    r = c.post(path, data={"name": "tom", "anzeige": "Tom Treuhand", "passwort": "tom-sein-pw-1",
                           "passwort2": "tom-sein-pw-1"}, follow_redirects=False)
    assert r.status_code == 303
    tom = c.ui.users.get("tom")
    assert tom.rolle == "lesen" and tom.eingeladen_von == "lea" and tom.letzter_login
    assert c.get("/", follow_redirects=False).status_code == 200                     # signed in
    assert c.get(path).status_code == 404                                          # used up
    log = c.ui.userlog.recent()
    assert [e["was"] for e in log[:2]] == ["angenommen", "eingeladen"]


def test_password_change_and_admin_tools(server):
    c = server
    login(c, "bruno", "geheim-genug-2")
    assert "stimmt nicht" in post(c, "/konto", {"aktion": "passwort", "aktuell": "x", "neu": "a" * 12, "neu2": "a" * 12}).text
    r = post(c, "/konto", {"aktion": "passwort", "aktuell": "geheim-genug-2", "neu": "bruno-neu-123", "neu2": "bruno-neu-123"})
    assert r.status_code == 204
    page = c.get("/", follow_redirects=False)
    assert page.status_code == 200                                                 # this session stays (new cookie)
    c.csrf = page.text.split('"X-CSRF": "')[1].split('"')[0]
    ok = post(c, "/konto", {"aktion": "anzeige", "anzeige": "Bruno B."})
    assert ok.status_code == 204 and c.ui.users.get("bruno").anzeige == "Bruno B."
    assert "Nur Admins" in post(c, "/benutzer/rolle", {"name": "lea", "rolle": "admin"}).text
    login(c, "anna", "geheim-genug-1")
    assert post(c, "/benutzer/rolle", {"name": "lea", "rolle": "buchhaltung"}).status_code == 204
    assert c.ui.users.get("lea").rolle == "buchhaltung"
    assert "eigene Rolle" in post(c, "/benutzer/rolle", {"name": "anna", "rolle": "lesen"}).text
    r = post(c, "/benutzer/passwort-link", {"name": "lea"})
    path = link_of(r.text).split("dtmg.example.ch")[1]
    assert post(c, "/benutzer/entfernen", {"name": "bruno"}).status_code == 204
    assert c.ui.users.get("bruno") is None
    c.cookies.clear()
    r = c.post(path, data={"passwort": "lea-neu-12345", "passwort2": "lea-neu-12345"}, follow_redirects=False)
    assert r.status_code == 303 and c.ui.users.check("lea", "lea-neu-12345")
    page = (c.get("/benutzer").text)
    assert "Lea Lesen" in page


def test_invites_store_only_hashes(tmp_path):
    inv = Invites(tmp_path)
    token, _ = inv.create("anna", "lesen")
    assert token not in (tmp_path / "einladungen.yaml").read_text()
    assert inv.find(token)["rolle"] == "lesen" and inv.consume(token) and inv.find(token) is None


def test_bookings_carry_the_author(server):
    c = server
    login(c, "bruno", "geheim-genug-2")
    r = post(c, "/journal/buchen", {"modus": "einfach", "datum": "2026-04-02", "betrag": "10", "text": "Papier",
                                    "soll": "6500", "haben": "1020"})
    assert r.status_code == 204
    assert gitlog.log(c.root, 1)[0]["autor"] == "Bruno Buch"


def test_who_booked_what(server):
    c = server
    login(c, "bruno", "geheim-genug-2")
    post(c, "/journal/buchen", {"modus": "einfach", "datum": "2026-04-02", "betrag": "10", "text": "Papier",
                                "soll": "6500", "haben": "1020"})
    from aeradex import api
    from aeradex.book import Book
    [row] = Book(c.root).rows
    login(c, "anna", "geheim-genug-1")
    r = post(c, "/journal/storno", {"beleg": row.beleg})
    assert r.status_code == 204, r.text
    h = api.booking_history(Book(c.root), row.beleg)
    assert [(e["art"], e["autor"]) for e in h["ereignisse"]] == [("erfasst", "Bruno Buch")]
    storno = next(r.beleg for r in Book(c.root).rows if r.beleg != row.beleg)
    assert api.booking_history(Book(c.root), storno)["ereignisse"][0]["autor"] == "Anna Admin"
    page = c.get("/journal?jahr=2026&monat=").text             # the whole year: booking and storno
    assert f'href="/verlauf?beleg={row.beleg}"' in page and ">BB<" in page and ">AA<" in page
    page = c.get(f"/verlauf?beleg={row.beleg}").text
    assert "Erfasst von Bruno Buch" in page
    page = c.get("/verlauf?von=anna").text
    assert "Anna Admin" in page and "Bruno Buch" not in page.split("<tbody>")[-1]

"""The role «verkauf» (server mode): a person who works only on the plugin pages made for them (e.g. phone
acquisition) and sees nothing of the books. Default-deny: every core page, file, chat and event stream is refused,
also the ones added later. Plugin pages say who may use them (`Page.rollen`); plugins learn who is signed in
(`auth.benutzer()`) and may offer endpoints (`Endpunkt`), public ones only by their own token."""
from __future__ import annotations

import types

import pytest

pytest.importorskip("starlette")
from starlette.routing import Mount, Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from aeradex.plugins import Antwort, Endpunkt, Page, hookimpl  # noqa: E402
from aeradex.testing import make_book, plugin  # noqa: E402
from aeradex.web import auth  # noqa: E402
from aeradex.web.app import create_app  # noqa: E402
from aeradex.web.auth import UserStore, may_grant  # noqa: E402

GESEHEN: list = []


def _anrufen_ctx(book, query):
    return {"wer": auth.benutzer().name if auth.benutzer() else "lokal"}


def _anrufen_merken(book, form):
    GESEHEN.append((auth.benutzer().name if auth.benutzer() else None, form.get("x")))
    return {"meldung": "ok"}


def _feed(book, query):
    if query.get("schluessel") != "geheim":
        return Antwort("Nein", status=403)
    return Antwort("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "text/calendar")


def _privat(book, query):
    return Antwort("intern:" + (auth.benutzer().name if auth.benutzer() else "-"))


def _modul():
    m = types.ModuleType("aeradex_verkaufstest")
    m.AERADEX_PLUGIN_API = 1

    @hookimpl
    def aeradex_pages():
        return [Page("anrufen", "Anrufen", "x.html", _anrufen_ctx, {"merken": _anrufen_merken}, bereich="akquise",
                     bereich_titel="Akquise", rollen=("verkauf",)),
                Page("chef", "Chef", "x.html", bereich="akquise", rollen=("admin",)),
                Page("normal", "Normal", "x.html", bereich="akquise")]

    @hookimpl
    def aeradex_endpunkte():
        return [Endpunkt("feed", _feed, oeffentlich=True), Endpunkt("privat", _privat, rollen=("verkauf",))]

    m.aeradex_pages, m.aeradex_endpunkte = aeradex_pages, aeradex_endpunkte
    return m


@pytest.fixture
def server(tmp_path, monkeypatch):
    from aeradex import plugins
    folder = tmp_path / "tpl"
    folder.mkdir()
    (folder / "x.html").write_text('{% extends "base.html" %}{% block main %}<h1>Seite {{ page.slug }}</h1>'
                                   '<p>wer={{ wer }}</p>{% endblock %}', encoding="utf-8")
    monkeypatch.setattr(plugins, "template_dir", lambda name: folder if name == "vt" else None)
    with plugin("vt", _modul()):
        book = make_book(tmp_path, plugins=["vt"])
        cfg = tmp_path / "cfg"
        store = UserStore(cfg)
        store.add("anna", "geheim-genug-1", "admin", "Anna Admin")
        store.add("bruno", "geheim-genug-2", "buchhaltung", "Bruno Buch")
        store.add("vera", "geheim-genug-4", "verkauf", "Vera Verkauf")
        app = create_app(book.root, auth_dir=cfg)
        with TestClient(app, base_url="https://buch.example.ch") as c:
            c.app_, c.root = app, book.root
            yield c


def login(c, name, password, ziel="/"):
    c.cookies.clear()
    r = c.post("/login", data={"name": name, "passwort": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    c.csrf = c.get(ziel).text.split('"X-CSRF": "')[1].split('"')[0]


def test_only_admins_hand_out_the_sales_role():
    assert may_grant("admin", "verkauf")
    assert not may_grant("buchhaltung", "verkauf") and not may_grant("lesen", "verkauf")
    assert not may_grant("verkauf", "lesen") and not may_grant("verkauf", "verkauf")


def test_sales_person_lands_on_their_page_and_sees_nothing_of_the_books(server):
    c = server
    c.cookies.clear()
    c.post("/login", data={"name": "vera", "passwort": "geheim-genug-4"}, follow_redirects=False)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/p/vt/anrufen"
    login(c, "vera", "geheim-genug-4", "/p/vt/anrufen")
    html = c.get("/p/vt/anrufen").text
    assert "Seite anrufen" in html and "wer=vera" in html
    for verraeterisch in ("Beleg hochladen", "Agent fragen", "/einstellungen", "/verlauf", "/datensicherung",
                          "Einkauf", "Lohn", "Buchhaltung", "chat.js", "Leserecht", 'href="/p/vt/normal"',
                          'href="/p/vt/chef"'):
        assert verraeterisch not in html, verraeterisch
    assert 'href="/p/vt/anrufen"' in html and "Mein Konto" in html


def _get_paths(app) -> list[str]:
    paths = []
    for r in app.routes:
        if isinstance(r, Route) and "GET" in (r.methods or set()) and "{" not in r.path:
            paths.append(r.path)
    return paths


def test_every_core_page_is_refused_for_sales(server):
    c = server
    login(c, "vera", "geheim-genug-4", "/p/vt/anrufen")
    erlaubt = {"/konto", "/login", "/login/code", "/healthz", "/manifest.webmanifest", "/sw.js", "/"}
    paths = _get_paths(c.app_) + ["/datei/aeradex.yaml", "/verlauf/HEAD", "/chat/stream/x", "/m/kunden/K0001",
                                  "/p/vt/normal", "/p/vt/chef", "/debitoren/kunden/K0001", "/x/vt/unbekannt"]
    assert len(paths) > 60
    for path in paths:
        if path in erlaubt:
            continue
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 403, f"{path}: {r.status_code}"
    for path in ("/journal/raster", "/chat/send", "/eingang/einlesen", "/benutzer/einladen", "/p/vt/normal/x"):
        r = c.post(path, data={}, headers={"X-CSRF": c.csrf}, follow_redirects=False)
        assert r.status_code == 403, f"POST {path}: {r.status_code}"


def test_sales_person_may_use_their_actions_and_their_account(server):
    c = server
    login(c, "vera", "geheim-genug-4", "/p/vt/anrufen")
    GESEHEN.clear()
    r = c.post("/p/vt/anrufen/merken", data={"x": "1"}, headers={"X-CSRF": c.csrf, "HX-Request": "true"})
    assert r.status_code == 204 and GESEHEN == [("vera", "1")]
    assert c.get("/konto").status_code == 200
    r = c.post("/konto", data={"aktion": "anzeige", "anzeige": "Vera V."}, headers={"X-CSRF": c.csrf, "HX-Request": "true"})
    assert r.status_code == 204
    assert c.get("/x/vt/privat").text == "intern:vera"


def test_page_roles_hold_for_the_other_roles_too(server):
    c = server
    login(c, "bruno", "geheim-genug-2")
    assert c.get("/p/vt/normal").status_code == 200
    assert c.get("/p/vt/anrufen").status_code == 403
    assert c.get("/p/vt/chef").status_code == 403
    assert c.get("/x/vt/privat").status_code == 403
    nav = c.get("/journal").text
    assert 'href="/p/vt/normal"' in nav and 'href="/p/vt/anrufen"' not in nav
    login(c, "anna", "geheim-genug-1")
    for slug in ("normal", "anrufen", "chef"):
        assert c.get(f"/p/vt/{slug}").status_code == 200, slug
    assert c.get("/").status_code == 200


def test_public_endpoint_needs_its_own_key_but_no_login(server):
    c = server
    c.cookies.clear()
    assert c.get("/x/vt/feed?schluessel=geheim").headers["content-type"].startswith("text/calendar")
    assert c.get("/x/vt/feed?schluessel=falsch").status_code == 403
    assert c.get("/x/vt/privat", follow_redirects=False).status_code == 303   # to the login
    assert c.get("/x/vt/nichts", follow_redirects=False).status_code in (303, 404)


def test_sales_role_in_user_admin(server):
    c = server
    login(c, "anna", "geheim-genug-1")
    html = c.get("/benutzer").text
    assert "Verkauf" in html
    r = c.post("/benutzer/einladen", data={"rolle": "verkauf", "notiz": "Marco"},
               headers={"X-CSRF": c.csrf, "HX-Request": "true"})
    assert "/einladung/" in r.text
    login(c, "bruno", "geheim-genug-2")
    r = c.post("/benutzer/einladen", data={"rolle": "verkauf"}, headers={"X-CSRF": c.csrf, "HX-Request": "true"})
    assert "/einladung/" not in r.text


def test_local_mode_is_unchanged(tmp_path, monkeypatch):
    from aeradex import plugins
    folder = tmp_path / "tpl"
    folder.mkdir()
    (folder / "x.html").write_text('{% extends "base.html" %}{% block main %}<p>wer={{ wer }}</p>{% endblock %}',
                                   encoding="utf-8")
    monkeypatch.setattr(plugins, "template_dir", lambda name: folder if name == "vt" else None)
    with plugin("vt", _modul()):
        book = make_book(tmp_path, plugins=["vt"])
        app = create_app(book.root, token="tok")
        with TestClient(app) as c:
            c.get("/?t=tok", follow_redirects=False)
            for slug in ("anrufen", "chef", "normal"):
                assert c.get(f"/p/vt/{slug}").status_code == 200
            assert "wer=lokal" in c.get("/p/vt/anrufen").text
            assert c.get("/x/vt/feed?schluessel=geheim").status_code == 200

import uuid as uuidlib
from datetime import date

import pytest

from aeradex_team_portal import auth
from aeradex_team_portal.app import Config, create_app
from aeradex_team_portal.testing import TOKEN, H, activate, auth_key, client, invite


def U():
    return str(uuidlib.uuid4())


def zeit(**kw):
    return {"datum": date.today().isoformat(), "stunden": "3.5", "projekt": "P0001", "text": "Fassade", **kw}


def test_sync_needs_the_token(app):
    c = client(app)
    assert c.get("/app/api/sync/pending").status_code == 401
    c.headers["authorization"] = "Bearer falsch"
    assert c.get("/app/api/sync/pending").status_code == 401


def test_invite_activate_login(app, pc):
    invite(pc)
    phone = activate(app)
    me = phone.get("/app/api/ich").json()
    assert me["person"] == "M0001" and me["name"] == "Anna Muster" and me["key_version"] == 1
    keys = pc.get("/app/api/sync/pending").json()["keys"]
    assert keys[0]["person"] == "M0001" and keys[0]["key_version"] == 1

    # the code works once
    other = client(app)
    r = other.post("/app/api/aktivieren", headers=H, json={"benutzer": "anna", "code": "ABCD-2345",
                                                            "auth": "0" * 64, "pubkey": "x",
                                                            "wrapped": {"salt": 1, "iv": 1, "ct": 1, "iter": 1}})
    assert r.status_code == 401

    fresh = client(app)
    assert fresh.post("/app/api/login", headers=H, json={"benutzer": "anna", "auth": auth_key("falsch", "anna")}).status_code == 401
    assert fresh.post("/app/api/login", headers=H, json={"benutzer": "Anna", "auth": auth_key("geheim-1234", "anna")}).status_code == 200
    assert fresh.get("/app/api/ich").json()["person"] == "M0001"


def test_login_throttle(app, pc):
    invite(pc)
    activate(app)
    c = client(app)
    for _ in range(5):
        assert c.post("/app/api/login", headers=H, json={"benutzer": "anna", "auth": "0" * 64}).status_code == 401
    r = c.post("/app/api/login", headers=H, json={"benutzer": "anna", "auth": auth_key("geheim-1234", "anna")})
    assert r.status_code == 429


def test_writes_need_the_custom_header(app, pc):
    invite(pc)
    phone = activate(app)
    r = phone.post("/app/api/eintraege", json={"uuid": U(), "kind": "zeit", "data": zeit()})
    assert r.status_code == 403


def test_entry_lifecycle(app, pc):
    invite(pc)
    phone = activate(app)
    uid = U()
    assert phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit()}).status_code == 200
    # resending is harmless, editing while open bumps the version
    phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit()})
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit(stunden="4")})
    assert r.json()["eintrag"]["version"] == 2

    pending = pc.get("/app/api/sync/pending").json()["entries"]
    assert [(e["uuid"], e["version"], e["data"]["stunden"]) for e in pending] == [(uid, 2, "4")]
    # claimed: the phone can no longer change it
    r = phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit(stunden="5")})
    assert r.status_code == 400
    assert phone.delete(f"/app/api/eintraege/{uid}", headers=H).status_code == 400

    pc.post("/app/api/sync/ack", json={"results": [{"uuid": uid, "version": 2, "status": "uebernommen", "aeradex_id": "L-2026-0001"}]})
    mine = phone.get("/app/api/ich").json()["eintraege"]
    assert mine[0]["status"] == "uebernommen" and mine[0]["aeradex_id"] == "L-2026-0001"
    assert pc.get("/app/api/sync/pending").json()["entries"] == []


def test_refused_entry_can_be_corrected(app, pc):
    invite(pc)
    phone = activate(app)
    uid = U()
    phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit(projekt="P0099")})
    pc.get("/app/api/sync/pending")
    pc.post("/app/api/sync/ack", json={"results": [{"uuid": uid, "version": 1, "status": "abgelehnt", "reason": "Projekt P0099 nicht gefunden"}]})
    assert phone.get("/app/api/ich").json()["eintraege"][0]["reason"] == "Projekt P0099 nicht gefunden"
    phone.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit(projekt="P0001")})
    again = pc.get("/app/api/sync/pending").json()["entries"]
    assert again[0]["uuid"] == uid and again[0]["version"] == 2 and again[0]["data"]["projekt"] == "P0001"


def test_validation(app, pc):
    invite(pc)
    phone = activate(app)
    bad = [("zeit", zeit(stunden="25")), ("zeit", zeit(projekt="", kategorie="")), ("zeit", zeit(text="")),
           ("material", {"datum": date.today().isoformat(), "projekt": "", "produkt": "P010", "menge": "2"}),
           ("abwesenheit", {"art": "Party", "von": "2026-10-01"}),
           ("abwesenheit", {"art": "Ferien", "von": "2026-10-05", "bis": "2026-10-01"}), ("lohn", {})]
    for kind, data in bad:
        assert phone.post("/app/api/eintraege", headers=H, json={"uuid": U(), "kind": kind, "data": data}).status_code == 400, (kind, data)
    assert phone.post("/app/api/eintraege", headers=H, json={"uuid": "nope", "kind": "zeit", "data": zeit()}).status_code == 400


def test_people_see_only_their_own(app, pc):
    pc.put("/app/api/sync/accounts", json={
        "accounts": [{"person": "M0001", "benutzer": "anna", "name": "Anna", "aktiv": True},
                     {"person": "M0002", "benutzer": "luca", "name": "Luca", "aktiv": True}],
        "invites": [{"person": "M0001", "code_sha256": __import__("hashlib").sha256(b"AAAA2222").hexdigest(), "expires": "2999-01-01"},
                    {"person": "M0002", "code_sha256": __import__("hashlib").sha256(b"BBBB3333").hexdigest(), "expires": "2999-01-01"}]})
    mel = activate(app, "anna", "AAAA2222")
    sha = activate(app, "luca", "BBBB3333")
    uid = U()
    mel.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit()})
    assert sha.get("/app/api/ich").json()["eintraege"] == []
    assert sha.delete(f"/app/api/eintraege/{uid}", headers=H).status_code == 403
    assert sha.post("/app/api/eintraege", headers=H, json={"uuid": uid, "kind": "zeit", "data": zeit()}).status_code == 403

    pc.put("/app/api/sync/snapshot", json={"stand": "x", "shared": {"projekte": [{"nummer": "P0001", "name": "Fassade"}]},
                                           "people": {"M0001": {"name": "Anna", "konto": {"x": 1}},
                                                      "M0002": {"name": "Luca", "konto": {"x": 2}}}})
    assert mel.get("/app/api/ich").json()["mein"]["konto"] == {"x": 1}
    assert sha.get("/app/api/ich").json()["mein"]["konto"] == {"x": 2}
    assert sha.get("/app/api/ich").json()["shared"]["projekte"][0]["name"] == "Fassade"

    keys = {k["person"]: k for k in pc.get("/app/api/sync/pending").json()["keys"]}
    env = {"title": "Lohn", "kind": "lohn", "period": "2026-10", "sha256": "a", "epk": "e", "iv": "i", "ct": "c"}
    assert pc.put("/app/api/sync/docs/M0001/lohn-2026-10", json={**env, "key_version": 99}).status_code == 400
    assert pc.put("/app/api/sync/docs/M0001/lohn-2026-10", json={**env, "key_version": keys["M0001"]["key_version"]}).status_code == 200
    assert [d["doc_id"] for d in mel.get("/app/api/dokumente").json()["docs"]] == ["lohn-2026-10"]
    assert sha.get("/app/api/dokumente").json()["docs"] == []
    assert sha.get("/app/api/dokumente/lohn-2026-10").status_code == 404


def test_reset_and_deactivation_end_sessions(app, pc):
    invite(pc)
    phone = activate(app)
    pc.put("/app/api/sync/docs/M0001/lohn-2026-10", json={"title": "Lohn", "kind": "lohn", "period": "2026-10", "sha256": "a",
                                                          "key_version": 1, "epk": "e", "iv": "i", "ct": "c"})
    invite(pc, code="NEWC0DE9")
    assert phone.get("/app/api/ich").status_code == 401
    assert pc.get("/app/api/sync/docs").json()["docs"] == []   # old key: documents gone
    phone2 = activate(app, code="NEWC-0DE9")
    assert phone2.get("/app/api/ich").json()["key_version"] == 2
    pc.put("/app/api/sync/accounts", json={"accounts": [], "invites": []})
    assert phone2.get("/app/api/ich").status_code == 401


def test_pages_are_hidden_from_search_engines(app):
    c = client(app)
    r = c.get("/app/")
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"] and "app.js" in r.text
    assert "Disallow: /" in c.get("/app/robots.txt").text
    assert c.get("/app/static/app.js").status_code == 200
    assert c.get("/app/sw.js").headers["service-worker-allowed"] == "/app/"


def test_status_counts_waiting_entries_without_claiming(app, pc):
    invite(pc)
    phone = activate(app)
    assert pc.get("/app/api/sync/status").json() == {"offen": 0}
    phone.post("/app/api/eintraege", headers=H, json={"uuid": U(), "kind": "zeit", "data": zeit()})
    assert pc.get("/app/api/sync/status").json() == {"offen": 1}
    assert pc.get("/app/api/sync/status").json() == {"offen": 1}        # nothing claimed
    assert client(app).get("/app/api/sync/status").status_code == 401


def test_kennung_names_cookie_header_and_page(tmp_path):
    """An installation with its own Kennung keeps its logins, phone storage and documents when it updates."""
    cfg = Config(tmp_path / "d", b"x" * 40, auth.sha256(TOKEN), kennung="muster", titel="Team · Muster AG")
    app = create_app(cfg)
    pc = client(app)
    pc.headers["authorization"] = f"Bearer {TOKEN}"
    invite(pc)
    with pytest.raises(AssertionError, match="Ungültige Anfrage"):    # the header of another Kennung
        activate(app)
    phone = activate(app, kennung="muster")
    assert "muster_s" in phone.cookies and phone.get("/app/api/ich").status_code == 200
    page = phone.get("/app/").text
    assert 'data-kennung="muster"' in page and "<title>Team · Muster AG</title>" in page and "__" not in page
    sw = phone.get("/app/sw.js").text
    assert '"muster-shell-v10"' in sw and '"/app/static/app.js"' in sw and "__" not in sw
    manifest = phone.get("/app/manifest.webmanifest").json()
    assert manifest["name"] == "Team · Muster AG" and manifest["short_name"] == "Team"
    assert pc.get("/app/api/sync/docs").json()["kennung"] == "muster"
    kurz = Config(tmp_path / "k", b"x" * 40, "", titel="Team · Muster AG", kurz="MA Team")
    assert client(create_app(kurz)).get("/app/manifest.webmanifest").json()["short_name"] == "MA Team"
    with pytest.raises(SystemExit):
        Config(tmp_path / "d", b"x" * 40, "", kennung="Nicht gut!")


def test_branding_folder_overrides_logo_icons_and_css(tmp_path):
    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "logo.png").write_bytes(b"\x89PNG logo")
    (brand / "branding.css").write_text(":root { --head: #000; }")
    (brand / "Schrift.woff2").write_bytes(b"font")
    (brand / ".env").write_text("SECRET=1")
    c = client(create_app(Config(tmp_path / "d", b"x" * 40, auth.sha256(TOKEN), branding=brand)))
    assert c.get("/app/static/logo.png").content == b"\x89PNG logo"
    assert "--head: #000" in c.get("/app/static/branding.css").text
    assert c.get("/app/static/icon-192.png").status_code == 200             # not branded: the neutral one
    assert c.get("/app/branding/Schrift.woff2").content == b"font"
    assert c.get("/app/branding/.env").status_code == 404
    assert c.get("/app/static/..%2Fapp.py").status_code == 404
    assert '<img src="/app/static/logo.png"' in c.get("/app/").text
    assert "/app/branding/Schrift.woff2" in c.get("/app/sw.js").text
    plain = client(create_app(Config(tmp_path / "e", b"x" * 40, auth.sha256(TOKEN))))
    assert plain.get("/app/static/logo.png").status_code == 404
    assert 'class="titel">Team<' in plain.get("/app/").text

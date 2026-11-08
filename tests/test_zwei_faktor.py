"""Second factor in server mode (`aeradex serve --2fa admin`): admins set up an authenticator app on their first
login and then give a code after the password; recovery codes work once. Without the switch nothing changes."""
from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from aeradex.testing import make_book  # noqa: E402
from aeradex.web import totp  # noqa: E402
from aeradex.web.app import create_app  # noqa: E402
from aeradex.web.auth import UserStore  # noqa: E402


def test_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # "12345678901234567890"
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    assert totp.verify(secret, "287 082", now=59) == 1
    assert totp.verify(secret, "287082", now=59, last_used=1) is None     # the same code works only once
    assert totp.verify(secret, "000000", now=59) is None


@pytest.fixture
def server(tmp_path):
    book = make_book(tmp_path, git=True)
    cfg = tmp_path / "cfg"
    store = UserStore(cfg)
    store.add("anna", "geheim-genug-1", "admin", "Anna Admin")
    store.add("bruno", "geheim-genug-2", "buchhaltung", "Bruno Buch")
    app = create_app(book.root, auth_dir=cfg, zwei_faktor="admin")
    with TestClient(app, base_url="https://buch.example.ch") as c:
        c.store = store
        yield c


def _password(c, name, pw):
    c.cookies.clear()
    return c.post("/login", data={"name": name, "passwort": pw}, follow_redirects=False)


def _secret_from(html: str) -> str:
    return re.search(r'data-geheimnis="([A-Z2-7]+)"', html).group(1)


def test_admin_sets_up_the_app_then_needs_a_code(server):
    c = server
    r = _password(c, "anna", "geheim-genug-1")
    assert r.status_code == 303 and r.headers["location"].startswith("/login/code")
    assert c.get("/journal", follow_redirects=False).status_code == 303          # not signed in yet
    page = c.get("/login/code").text
    assert "<svg" in page and "Authenticator" in page
    secret = _secret_from(page)
    assert c.post("/login/code", data={"code": "123456"}, follow_redirects=False).status_code == 401
    r = c.post("/login/code", data={"code": totp.code_at(secret, totp.counter())}, follow_redirects=False)
    assert r.status_code == 200 and "Ersatzcodes" in r.text
    codes = re.findall(r"<code>([0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})</code>", r.text)
    assert len(codes) == 10
    assert c.get("/journal").status_code == 200
    assert "Zwei-Faktor" in c.get("/konto").text

    # next login: password, then a code from the app (a fresh time step) or a recovery code once
    r = _password(c, "anna", "geheim-genug-1")
    assert r.headers["location"].startswith("/login/code")
    assert "<svg" not in c.get("/login/code").text
    later = time.time() + 30
    import aeradex.web.totp as t
    orig = t.counter
    t.counter = lambda now=None: orig(later)
    try:
        r = c.post("/login/code", data={"code": totp.code_at(secret, orig(later))}, follow_redirects=False)
    finally:
        t.counter = orig
    assert r.status_code == 303 and c.get("/journal").status_code == 200
    _password(c, "anna", "geheim-genug-1")
    assert c.post("/login/code", data={"code": codes[0]}, follow_redirects=False).status_code == 303
    _password(c, "anna", "geheim-genug-1")
    assert c.post("/login/code", data={"code": codes[0]}, follow_redirects=False).status_code == 401


def test_a_session_without_the_second_factor_does_not_count(server):
    c = server
    from aeradex.web.auth import SESSION_COOKIE
    ui = c.app.state.ui
    c.cookies.set(SESSION_COOKIE, ui.sessions.issue("anna", ui.users.version("anna")))
    assert c.get("/journal", follow_redirects=False).status_code == 303
    c.cookies.set(SESSION_COOKIE, ui.sessions.issue("bruno", ui.users.version("bruno")))
    assert c.get("/journal").status_code == 200


def test_pending_cookie_is_no_session(server):
    c = server
    _password(c, "anna", "geheim-genug-1")
    from aeradex.web.auth import SESSION_COOKIE
    assert SESSION_COOKIE not in c.cookies
    assert c.get("/", follow_redirects=False).status_code == 303


def test_bookkeeper_signs_in_without_code(server):
    c = server
    r = _password(c, "bruno", "geheim-genug-2")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/journal").status_code == 200


def test_wrong_codes_are_throttled(server):
    c = server
    _password(c, "anna", "geheim-genug-1")
    c.get("/login/code")
    for _ in range(5):
        c.post("/login/code", data={"code": "000000"})
    assert "Zu viele" in c.post("/login/code", data={"code": "000000"}).text


def test_without_the_switch_admins_sign_in_as_before(tmp_path):
    book = make_book(tmp_path, git=True)
    cfg = tmp_path / "cfg"
    UserStore(cfg).add("anna", "geheim-genug-1", "admin")
    with TestClient(create_app(book.root, auth_dir=cfg), base_url="https://x.example.ch") as c:
        r = c.post("/login", data={"name": "anna", "passwort": "geheim-genug-1"}, follow_redirects=False)
        assert r.headers["location"] == "/" and c.get("/journal").status_code == 200


def test_cli_resets_the_second_factor(server):
    c = server
    c.store.set_totp("anna", totp.new_secret(), [totp.recovery_hash("aaaa-bbbb-cccc")])
    assert c.store.totp_info("anna")
    c.store.clear_totp("anna")
    assert not c.store.totp_info("anna")

"""The company list of the local UI: start without a book, create, add, switch, remove; never in server mode."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from aeradex import api, firmen  # noqa: E402
from aeradex.book import Book, BookError  # noqa: E402
from aeradex.testing import make_book  # noqa: E402
from aeradex.web.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _base(tmp_path, monkeypatch):
    monkeypatch.setenv("AERADEX_FIRMEN_ORDNER", str(tmp_path / "firmen"))


def _client(root):
    app = create_app(root, token="tok")
    c = TestClient(app)
    c.__enter__()
    c.get("/?t=tok", follow_redirects=False)
    c.csrf = app.state.ui.csrf
    c.ui = app.state.ui
    return c


def post(c, url, data=None, files=None):
    return c.post(url, data=data or {}, files=files, headers={"X-CSRF": c.csrf, "HX-Request": "true"})


def test_list_create_add_remove(tmp_path):
    assert api.companies()["firmen"] == []
    result = api.company_create("Alpha & Co GmbH", "GmbH", 2026, plz="8000", ort="Zürich")
    assert Path(result["buch"]) == tmp_path / "firmen" / "alpha-co-gmbh"
    other = make_book(tmp_path / "beta")
    api.company_add(str(other.root))
    listed = api.companies()["firmen"]
    assert [f["firma"] for f in listed] == [other.settings.firma, "Alpha & Co GmbH"]   # last opened first
    assert listed[1]["ort"] == "Zürich"
    with pytest.raises(BookError):
        api.company_add(str(tmp_path))                        # no aeradex.yaml
    with pytest.raises(BookError):
        api.company_create("Alpha & Co GmbH", ordner=str(other.root))   # folder not empty
    api.company_remove(str(other.root))
    assert other.root.exists() and [f["firma"] for f in api.companies()["firmen"]] == ["Alpha & Co GmbH"]
    # a second company with the same name gets its own folder
    assert firmen.default_folder("Alpha & Co GmbH").name == "alpha-co-gmbh-2"


def test_missing_folder_is_shown_not_dropped(tmp_path):
    book = make_book(tmp_path / "weg")
    firmen.merken(book.root)
    import shutil
    shutil.rmtree(book.root)
    [entry] = firmen.liste()
    assert entry["fehlt"] and entry["ordner"] == str(book.root)


def test_ui_without_book_starts_with_the_list_and_creates_one(tmp_path):
    c = _client(None)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/firmen"
    assert post(c, "/journal/buchen").headers["hx-redirect"] == "/firmen"
    page = c.get("/firmen").text
    assert "Noch keine Firma" in page and "Zurück zu" not in page
    r = post(c, "/firmen/neu", {"firma": "Gamma AG", "rechtsform": "AG", "jahr": "2026"})
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/einstellungen"
    assert c.ui.root == (tmp_path / "firmen" / "gamma-ag").resolve()
    assert "Gamma AG" in c.get("/").text
    assert Book(c.ui.root).settings.get("rechtsform") == "AG"
    c.__exit__(None, None, None)


def test_switch_between_books(tmp_path):
    a, b = make_book(tmp_path / "a"), make_book(tmp_path / "b")
    Book(b.root)
    import yaml
    data = yaml.safe_load((b.root / "aeradex.yaml").read_text())
    data["firma"] = "Zweite GmbH"
    (b.root / "aeradex.yaml").write_text(yaml.safe_dump(data, allow_unicode=True))
    firmen.merken(a.root)
    firmen.merken(b.root)
    c = _client(a.root)
    assert "Firma wechseln" in c.get("/").text
    page = c.get("/firmen").text
    assert "Zweite GmbH" in page and "geöffnet" in page
    r = post(c, "/firmen/entfernen", {"ordner": str(a.root)})
    assert "lässt sich nicht entfernen" in r.text                    # the open one stays
    r = post(c, "/firmen/oeffnen", {"ordner": str(b.root)})
    assert r.headers["hx-redirect"] == "/" and c.ui.root == b.root.resolve()
    assert "Zweite GmbH" in c.get("/").text
    assert "Zweite GmbH" == firmen.liste()[0]["firma"]
    assert "kein aeradex-Buch" in post(c, "/firmen/hinzufuegen", {"ordner": str(tmp_path)}).text
    c.__exit__(None, None, None)


def test_import_from_aeradex_file(tmp_path):
    book = make_book(tmp_path / "quelle")
    api.book_export(book, str(tmp_path / "x.aeradex"))
    c = _client(None)
    r = post(c, "/firmen/importieren", files={"datei": ("x.aeradex", (tmp_path / "x.aeradex").read_bytes())})
    assert r.status_code == 204, r.text
    assert c.ui.root is not None and c.ui.root.parent == (tmp_path / "firmen").resolve()
    assert firmen.bekannt(c.ui.root)
    c.__exit__(None, None, None)


def test_server_mode_has_no_company_list(tmp_path):
    from aeradex.web.auth import UserStore
    book = make_book(tmp_path / "s")
    UserStore(tmp_path / "cfg").add("anna", "geheim-geheim-1", "admin")
    app = create_app(book.root, auth_dir=tmp_path / "cfg")
    with TestClient(app) as c:
        c.post("/login", data={"name": "anna", "passwort": "geheim-geheim-1"})
        assert c.get("/firmen").status_code == 404
        assert "Firma wechseln" not in c.get("/").text
    with pytest.raises(ValueError):
        create_app(None, auth_dir=tmp_path / "cfg")

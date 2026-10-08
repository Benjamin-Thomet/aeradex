"""Plugin marketplace: catalog, maintainer review with fingerprint, code watch, install."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from aeradex import check, marktplatz, plugins
from aeradex.book import Book, BookError
from aeradex.testing import make_book, plugin


PLUGIN_CODE = '''"""Testplugin für den Katalog."""
from aeradex.plugins import Finding, hookimpl

AERADEX_PLUGIN_API = 1
__version__ = "1.0.0"


@hookimpl
def aeradex_check(book, rows):
    return []
'''


@pytest.fixture
def katalog(tmp_path, monkeypatch):
    pkg = tmp_path / "src" / "kat_testplugin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(PLUGIN_CODE)
    monkeypatch.syspath_prepend(str(tmp_path / "src"))
    module = importlib.import_module("kat_testplugin")
    path = tmp_path / "katalog.json"
    path.write_text(json.dumps({"version": 1, "plugins": [
        {"name": "testplug", "paket": "aeradex-testplug", "version": "1.0.0", "beschreibung": "x", "hooks": ["check"],
         "status": "ungeprüft"}]}))
    monkeypatch.setenv("AERADEX_PLUGIN_KATALOG", str(path))
    with plugin("testplug", module):
        yield path, pkg
    sys.modules.pop("kat_testplugin", None)


def test_review_records_fingerprint_and_watches_the_code(tmp_path, katalog):
    path, pkg = katalog
    book = make_book(tmp_path / "b", plugins=["testplug"])
    e = marktplatz.entries(book)[0]
    assert not e["geprueft"] and e["rechte"] == ["eigene Prüfregeln"]
    reviewed = marktplatz.review("testplug", "Anna Muster", tests=False)
    assert reviewed["status"] == "geprüft" and reviewed["pruefung"]["sha256"] == marktplatz.tree_hash(pkg)
    assert json.loads(path.read_text())["plugins"][0]["pruefung"]["von"] == "Anna Muster"
    e = marktplatz.entries(book)[0]
    assert e["geprueft"] and e["code"] == "unverändert"
    assert not [i for i in check.run(Book(book.root)) if "nicht der geprüfte" in i.message]
    (pkg / "__init__.py").write_text(PLUGIN_CODE + "\n# nachträglich geändert\n")
    assert marktplatz.entries(book)[0]["code"] == "verändert"
    assert any(i.level == "warnung" and "nicht der geprüfte" in i.message for i in check.run(Book(book.root)))
    marktplatz.revoke("testplug")
    assert marktplatz.entries(book)[0]["status"] == "ungeprüft"


def test_install_needs_review_or_explicit_consent(katalog, monkeypatch):
    calls = []
    monkeypatch.setattr(marktplatz.subprocess, "run",
                        lambda args, **kw: calls.append(args) or type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})())
    with pytest.raises(BookError, match="nicht geprüft"):
        marktplatz.install("testplug")
    marktplatz.install("testplug", ungeprueft_ok=True)
    assert calls[0][-1] == "aeradex-testplug==1.0.0" and calls[0][1:4] == ["-m", "pip", "install"]
    with pytest.raises(BookError, match="nicht im Katalog"):
        marktplatz.install("gibtsnicht", True)


def test_catalog_over_https_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AERADEX_PLUGIN_KATALOG", "https://example.org/katalog.json")
    data = json.dumps({"version": 1, "plugins": [{"name": "a", "paket": "aeradex-a"}]}).encode()
    assert marktplatz.load(fetch=lambda url: data)["plugins"][0]["name"] == "a"

    def offline(url):
        raise OSError("offline")
    assert marktplatz.load(fetch=offline)["plugins"][0]["name"] == "a"               # from the cache
    monkeypatch.setenv("AERADEX_PLUGIN_KATALOG", "http://example.org/k.json")
    with pytest.raises(BookError, match="https"):
        marktplatz.load()


def test_bundled_catalog_lists_the_examples_unreviewed():
    names = {e["name"]: e for e in marktplatz.load()["plugins"]}
    assert set(names) >= {"anlagen", "revolut"}
    assert all(e["status"] == "ungeprüft" for e in names.values())


def test_settings_page_shows_catalog_and_installs_locally(tmp_path, katalog, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    book = make_book(tmp_path / "b")
    installed = []
    monkeypatch.setattr(marktplatz, "install", lambda name, ok=False: installed.append((name, ok)) or "")
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/einstellungen").text
        assert "Katalog" in page and "ungeprüft" in page
        r = c.post("/einstellungen/plugins/installieren", headers=h, data={"name": "testplug", "ungeprueft_ok": "1"})
        assert r.status_code == 204, r.text
        assert installed == [("testplug", True)]
        assert "Jetzt neu starten" in c.get("/einstellungen").text


@pytest.mark.parametrize("argv0", ["/usr/bin/aeradex", "/repo/src/aeradex/__main__.py"])
def test_restart_runs_aeradex_as_module(argv0, monkeypatch):
    """The restart after an install must work for `aeradex ui` and `python -m aeradex ui` alike."""
    import os
    import threading

    from aeradex.web import app

    calls = []
    monkeypatch.setattr(sys, "argv", [argv0, "--buch", "muster", "ui", "--port", "8790"])
    monkeypatch.setattr(os, "execv", lambda exe, args: calls.append((exe, args)))
    monkeypatch.setattr(threading, "Timer", lambda delay, fn: type("T", (), {"start": staticmethod(fn)}))
    monkeypatch.delenv("AERADEX_UI_TOKEN", raising=False)
    app.restart_later(type("UI", (), {"token": "tok"})())
    assert calls == [(sys.executable, [sys.executable, "-m", "aeradex", "--buch", "muster", "ui", "--port", "8790"])]
    assert os.environ.pop("AERADEX_UI_TOKEN") == "tok"
    os.environ.pop("AERADEX_UI_RESTART", None)

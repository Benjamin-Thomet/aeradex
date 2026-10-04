"""Plugin system: discovery, per-book activation, every hook, and the guarantees around them."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

import plugin_sample
from batzen import api, bank, cli, plugins, tools
from batzen.book import Book, BookError
from batzen.testing import assert_clean, make_book, plugin, problems


@pytest.fixture
def sample():
    with plugin("spenden", plugin_sample):
        yield


def test_installed_is_not_enabled(tmp_path, sample):
    book = make_book(tmp_path)
    assert "spenden" in plugins.installed()
    assert plugins.sources(book) == {}                       # behaviour only when enabled
    with pytest.raises(BookError, match="Unbekanntes Kontoauszugsformat"):
        plugins.bank_format_for(book, "a.csv", b"Datum;Betrag;Text\n")
    assert "testplan" in plugins.kontenplaene()             # data from every installed plugin


def test_enable_and_owned_rows_are_checked(tmp_path, sample):
    book = make_book(tmp_path, plugins=["spenden"])
    out = api.write(book, "Spende", plugin_sample.record, "Anna", "250.00", "2026-03-05")
    assert out["ok"] and out["ergebnis"] == "0001"
    assert_clean(book)
    # a changed row no longer matches its document
    path = book.root / "journal" / "2026" / "2026-03.md"
    path.write_text(path.read_text().replace("250.00", "260.00"))
    assert any("Spende 0001" in p for p in problems(book))
    path.write_text(path.read_text().replace("260.00", "250.00"))
    # a document without its row is caught too
    (book.root / "spenden" / "0002.yaml").write_text("von: Ben\nbetrag: '10.00'\ndatum: '2026-03-06'\n")
    assert any("erwartet 1 Zeile(n), gefunden 0" in p for p in problems(book))


def test_check_rule_and_refused_disable(tmp_path, sample):
    book = make_book(tmp_path, plugins=["spenden"])
    api.write(book, "Spende", plugin_sample.record, "Gross AG", "20000.00", "2026-03-05")
    assert any("Grossspende" in str(i) for i in __import__("batzen.check").check.run(Book(book.root)))
    with pytest.raises(BookError, match="besitzt noch Journalzeilen"):
        api.plugin_disable(Book(book.root), "spenden")


def test_missing_plugin_makes_book_invalid(tmp_path, sample):
    book = make_book(tmp_path, plugins=["spenden"])
    api.write(book, "Spende", plugin_sample.record, "Anna", "250.00", "2026-03-05")
    plugins.unregister("spenden")
    found = problems(Book(book.root), ("fehler",))
    assert any("nicht installiert" in p for p in found)
    with pytest.raises(BookError, match="zuerst beheben"):
        api.post_entry(Book(book.root), "2026-03-06", "6500", "1020", "10", "x")
    plugins.register("spenden", plugin_sample)


def test_api_version_mismatch_is_refused(tmp_path):
    old = types.ModuleType("old_plugin")
    old.BATZEN_PLUGIN_API = 99
    with plugin("alt", old):
        assert "Plugin-API 99" in plugins.installed()["alt"].fehler
        with pytest.raises(BookError, match="kann nicht laufen"):
            make_book(tmp_path, plugins=["alt"])


def test_bank_format_from_plugin(tmp_path, sample):
    book = make_book(tmp_path, plugins=["spenden"], eroeffnung={"1020": 1000, "2800": -1000})
    src = tmp_path / "auszug.csv"
    src.write_bytes(b"Datum;Betrag;Text\n2026-03-02;-45.50;Kontogebuehr\n2026-03-03;120.00;Einzahlung\n")
    out = api.bank_import(book, str(src))["import_"]
    assert out["neu"] == 2 and out["auszuege"][0]["format"] == "testcsv"
    assert list((book.root / "bank" / "auszuege" / "2026").glob("*.csv"))
    assert [t["Status"] for t in bank.transactions(Book(book.root))] == ["offen", "offen"]
    with pytest.raises(BookError, match="Unbekanntes Kontoauszugsformat"):
        bad = tmp_path / "x.txt"
        bad.write_bytes(b"hello")
        api.bank_import(Book(book.root), str(bad))


def test_chart_template_with_system_accounts(tmp_path, sample):
    book = make_book(tmp_path, kontenplan="testplan")
    assert book.settings.konto("ertrag") == "3600"
    assert "6500" in book.accounts


def test_agent_tools_and_instructions(tmp_path, sample, monkeypatch):
    book = make_book(tmp_path, plugins=["spenden"])
    names = [fn.__name__ for fn in tools.shared_for(book.root)]
    assert "spende_erfassen" in names and "propose_booking" in names
    assert "Spenden" in tools.instructions_for(book.root)
    other = make_book(tmp_path / "zwei")
    assert "spende_erfassen" not in [fn.__name__ for fn in tools.shared_for(other.root)]
    monkeypatch.setenv("BATZEN_BUCH", str(book.root))
    result = plugin_sample.spende_erfassen("Anna", "50.00", "2026-04-01")
    assert result["ok"]
    assert_clean(Book(book.root))


def test_cli_plugins_and_plugin_command(tmp_path, sample, capsys):
    book = make_book(tmp_path)
    root = str(book.root)
    assert cli.main(["--buch", root, "spende", "Anna", "20", "--datum", "2026-05-01"]) == 2
    assert "nicht eingeschaltet" in capsys.readouterr().err
    assert cli.main(["--buch", root, "plugins", "ein", "spenden"]) == 0
    assert cli.main(["--buch", root, "spende", "Anna", "20", "--datum", "2026-05-01"]) == 0
    capsys.readouterr()
    assert cli.main(["--buch", root, "plugins"]) == 0
    listing = capsys.readouterr().out
    assert "● spenden" in listing and "sources" in listing
    assert_clean(Book(book.root))


def test_settings_panel_switches_plugins(tmp_path, sample):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from batzen.web.app import create_app
    book = make_book(tmp_path)          # no git: the pre-commit hook runs in a process without test plugins
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        headers = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/einstellungen").text
        assert 'id="plugins"' in page and "spenden" in page
        r = c.post("/einstellungen/plugins/ein", data={"name": "spenden"}, headers=headers)
        assert r.status_code == 204, r.text
        assert "spenden" in Book(book.root).settings.get("plugins")
        api.write(Book(book.root), "Spende", plugin_sample.record, "Anna", "25.00", "2026-03-05")
        assert "/spenden/0001" in c.get("/journal?jahr=2026&monat=3").text       # link from Source.link
        r = c.post("/einstellungen/plugins/aus", data={"name": "spenden"}, headers=headers)
        assert "besitzt noch Journalzeilen" in r.text

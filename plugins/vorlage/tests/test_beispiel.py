from aeradex import api, check, plugins
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book

import aeradex_beispiel


def test_installed():
    assert plugins.installed()["beispiel"].ok


def test_rule_tool_and_command(tmp_path, monkeypatch):
    book = make_book(tmp_path, plugins=["beispiel"])
    api.post_entry(book, "2026-03-02", "6500", "1020", "10", "abc")
    hints = [str(i) for i in check.run(Book(book.root)) if i.level == "hinweis"]
    assert any("sehr kurz" in h for h in hints)
    assert_clean(Book(book.root))                     # hints are fine, errors/warnings are not
    monkeypatch.setenv("AERADEX_BUCH", str(book.root))
    assert aeradex_beispiel.beispiel_kurze_texte() == {"belege": ["26-001"]}

"""Helpers for testing batzen plugins (and batzen itself) on throwaway books.

    from batzen.testing import make_book, assert_clean

    def test_my_plugin(tmp_path):
        book = make_book(tmp_path, plugins=["myplugin"])
        ...
        assert_clean(book)

A plugin that is not installed as a package can be registered for the test
with ``with plugin("name", module): …``. Such a plugin exists only in the test
process: use books without git (the default here), because the pre-commit hook
runs `batzen check` in a separate process that only sees installed plugins.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import ModuleType

from . import api, check, plugins
from .book import Book


def make_book(path: Path, firma: str = "Test GmbH", jahr: int = 2026, plugins: tuple | list = (),
              kontenplan: str | None = None, eroeffnung: dict | None = None, git: bool = False, **settings) -> Book:
    """A fresh book under `path`/buch. `eroeffnung` sets opening balances, e.g.
    {"1020": 20000, "2800": -20000}; `plugins` are switched on. The chart follows `rechtsform` unless
    `kontenplan` names one."""
    root = Path(path) / "buch"
    api.init_book(root, firma, jahr, kontenplan=kontenplan, git=git, **settings)
    book = Book(root)
    if eroeffnung:
        for nr, value in eroeffnung.items():
            book.accounts[str(nr)].eroeffnung = api.Decimal(str(value))
        book.save_accounts()
    for name in plugins:
        api.plugin_enable(Book(root), name)
    return Book(root)


def problems(book: Book, levels: tuple[str, ...] = ("fehler", "warnung")) -> list[str]:
    return [str(i) for i in check.run(Book(book.root)) if i.level in levels]


def assert_clean(book: Book) -> None:
    """The book passes `batzen check` without errors or warnings."""
    found = problems(book)
    assert not found, "batzen check:\n" + "\n".join(found)


@contextlib.contextmanager
def plugin(name: str, module: ModuleType):
    """Make `module` available as plugin `name` for the duration of a test."""
    plugins.register(name, module)
    try:
        yield
    finally:
        plugins.unregister(name)

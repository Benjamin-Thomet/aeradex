from batzen import api, plugins, statements
from batzen.book import Book
from batzen.testing import assert_clean, make_book


def test_template_is_offered():
    assert "verein" in plugins.kontenplaene()


def test_association_year(tmp_path):
    book = make_book(tmp_path, firma="Turnverein Muster", kontenplan="verein",
                     eroeffnung={"1020": 5000, "2800": -5000})
    assert book.settings.konto("ertrag") == "3000"
    api.post_entry(book, "2026-03-01", "1020", "3000", "1200", "Mitgliederbeiträge 2026")
    api.post_entry(Book(book.root), "2026-04-01", "6000", "1020", "800", "Hallenmiete")
    assert_clean(Book(book.root))
    st = statements.year_end_statement(Book(book.root), 2026)
    assert st["jahresergebnis"] == 400 and st["differenz"] == 0

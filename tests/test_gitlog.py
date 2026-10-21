"""An API commit must only include its own files in the audit trail."""
import subprocess

import pytest

from aeradex import gitlog


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("AERADEX_NO_COMMIT", raising=False)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.org")
    (tmp_path / "booking.txt").write_text("old")
    (tmp_path / "notes.txt").write_text("old")
    gitlog.commit(tmp_path, "Initial")
    (tmp_path / "notes.txt").write_text("staged notes")
    git(tmp_path, "add", "notes.txt")
    (tmp_path / "notes.txt").write_text("unstaged notes")
    return tmp_path


def test_commit_preserves_unrelated_index_and_worktree(repo):
    booking = repo / "booking.txt"
    booking.write_text("new")
    assert gitlog.commit(repo, "Booking", [booking])
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "booking.txt"
    assert git(repo, "show", ":notes.txt") == "staged notes"
    assert (repo / "notes.txt").read_text() == "unstaged notes"


@pytest.mark.parametrize("paths", [[], ["booking.txt"], ["missing.txt"]])
def test_noop_does_not_commit_other_staged_files(repo, paths):
    before = git(repo, "rev-parse", "HEAD")
    assert gitlog.commit(repo, "No changes", [repo / p for p in paths]) is None
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "diff", "--cached", "--name-only") == "notes.txt"


def test_commit_tracks_deleted_files(repo):
    booking = repo / "booking.txt"
    booking.unlink()
    assert gitlog.commit(repo, "Delete booking", [booking])
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "booking.txt"
    assert git(repo, "diff", "--cached", "--name-only") == "notes.txt"


def test_commit_tracks_already_staged_deletion(repo):
    booking = repo / "booking.txt"
    git(repo, "rm", "booking.txt")
    assert gitlog.commit(repo, "Delete booking", [booking])
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "booking.txt"
    assert git(repo, "diff", "--cached", "--name-only") == "notes.txt"


def test_beleg_index_is_extended_incrementally(tmp_path, monkeypatch):
    import json
    from aeradex import api
    from aeradex.book import Book
    from aeradex.testing import make_book
    monkeypatch.delenv("AERADEX_NO_COMMIT", raising=False)
    book = make_book(tmp_path, git=True)
    api.post_entry(book, "2026-03-02", "6500", "1020", "10", "Papier")
    first = gitlog.beleg_index(book.root)
    assert [e["art"] for e in first["26-001"]] == ["erfasst"]
    api.amend_entry(Book(book.root), "26-001", "2026-03-02", "Papier",
                    [{"soll": "6500", "haben": "1020", "betrag": "12", "text": "Papier"}])
    api.post_entry(Book(book.root), "2026-03-03", "6500", "1020", "5", "Stifte")
    cached = json.loads((book.root / ".aeradex/lokal/belegindex.json").read_text())
    assert cached["head"] != git(book.root, "rev-parse", "--short", "HEAD")      # stale until asked
    again = gitlog.beleg_index(book.root)
    (book.root / ".aeradex/lokal/belegindex.json").unlink()
    assert again == gitlog.beleg_index(book.root)                                 # incremental = rebuilt
    assert "26-002" in again and [e["art"] for e in again["26-001"]] == ["erfasst", "geändert"]

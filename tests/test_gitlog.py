"""An API commit must only include its own files in the audit trail."""
import subprocess

import pytest

from batzen import gitlog


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("BATZEN_NO_COMMIT", raising=False)
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

"""Git as the audit trail. Every write the engine makes is one commit.

A book does not have to be a git repository — then writes simply are not
committed — but `batzen init` makes it one and installs a pre-commit hook that
runs `batzen check`, so a broken book cannot be committed by anyone, human or
agent.
"""
from __future__ import annotations

import contextvars
import os
import subprocess
import sys
from pathlib import Path

# Who a commit is attributed to: None = the git user (a person at the CLI or UI);
# the MCP server and the chat agent set an agent identity so the audit trail
# shows which changes a person made and which an agent made.
AUTHOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("batzen_author", default=None)
AGENT_AUTHOR = "batzen Agent <agent@batzen.local>"


def agent_author(tool: str) -> str:
    return f"batzen Agent ({tool}) <agent@batzen.local>"

HOOK = """#!/bin/sh
# Installed by batzen: refuse a commit that leaves the book invalid.
exec "{python}" -m batzen --buch "$(git rev-parse --show-toplevel)" check --quiet
"""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=check)


def is_repo(root: Path) -> bool:
    return (root / ".git").exists()


def init_repo(root: Path) -> None:
    if not is_repo(root):
        _git(root, "init", "-q")
    attributes = root / ".gitattributes"
    if not attributes.exists():
        attributes.write_text(GITATTRIBUTES, encoding="utf-8")
    install_hook(root)


def install_hook(root: Path) -> None:
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK.format(python=sys.executable), encoding="utf-8")
    hook.chmod(0o755)


def commit(root: Path, message: str, paths: list[Path] | None = None) -> str | None:
    """Stage `paths` (or everything) and commit. Returns the short hash, or None
    when the book is not a repo, commits are disabled, or nothing changed."""
    if os.environ.get("BATZEN_NO_COMMIT") or not is_repo(root):
        return None
    if paths:
        rel = sorted({str(Path(p).resolve().relative_to(root)) for p in paths
                      if Path(p).resolve().is_relative_to(root)})
        tracked = set(_git(root, "ls-files", "--", *rel, check=False).stdout.splitlines())
        # A path that is gone and was never committed (a receipt moved out of an
        # untracked inbox) has nothing to stage; git would reject it.
        rel = [r for r in rel if (root / r).exists() or r in tracked]
        if rel:
            _git(root, "add", "-A", "--", *rel)
    else:
        _git(root, "add", "-A")
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    author = AUTHOR.get() or os.environ.get("BATZEN_AUTHOR")
    extra = [f"--author={author}"] if author else []
    result = _git(root, "commit", "-q", *extra, "-m", message, check=False)
    if result.returncode != 0:
        raise RuntimeError("git commit abgelehnt:\n" + (result.stdout + result.stderr).strip())
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def log(root: Path, limit: int = 20) -> list[dict]:
    if not is_repo(root):
        return []
    out = _git(root, "log", f"-{limit}", "--pretty=format:%h\x1f%ad\x1f%an\x1f%s", "--date=iso",
               check=False).stdout
    rows = []
    for line in out.splitlines():
        h, d, a, s = line.split("\x1f", 3)
        rows.append({"commit": h, "datum": d, "autor": a, "nachricht": s})
    return rows


BINARY = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".woff2")
GITATTRIBUTES = "".join(f"*{ext} binary\n" for ext in BINARY)


def show(root: Path, commit: str) -> dict:
    """One commit: message, author, date and the unified diff per file."""
    if not is_repo(root) or not commit.isalnum():
        return {"commit": commit, "dateien": []}
    head = _git(root, "show", "-s", "--format=%h\x1f%H\x1f%ad\x1f%an\x1f%s", "--date=iso", commit,
                check=False).stdout.strip()
    if not head:
        return {"commit": commit, "dateien": []}
    short, full, when, author, subject = head.split("\x1f", 4)
    diff = _git(root, "show", "--format=", "--no-color", "--unified=2", "--no-textconv", commit, check=False).stdout
    files, current = [], None
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            current = {"pfad": line.split(" b/", 1)[-1], "zeilen": []}
            files.append(current)
        elif current is not None and current["pfad"].lower().endswith(BINARY):
            continue
        elif current is not None and not line.startswith(("index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ")):
            kind = "add" if line.startswith("+") else "del" if line.startswith("-") else "hunk" if line.startswith("@@") else "ctx"
            current["zeilen"].append({"art": kind, "text": line[1:] if kind in ("add", "del", "ctx") else line})
    return {"commit": short, "hash": full, "datum": when, "autor": author, "nachricht": subject, "dateien": files}

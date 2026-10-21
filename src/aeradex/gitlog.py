"""Git as the audit trail. Every write the engine makes is one commit.

A book does not have to be a git repository — then writes simply are not
committed — but `aeradex init` makes it one and installs a pre-commit hook that
runs `aeradex check`, so a broken book cannot be committed by anyone, human or
agent.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Who a commit is attributed to: None = the git user (a person at the CLI or UI);
# the MCP server and the chat agent set an agent identity so the audit trail
# shows which changes a person made and which an agent made.
AUTHOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("aeradex_author", default=None)
AGENT_AUTHOR = "aeradex Agent <agent@aeradex.local>"


def agent_author(tool: str) -> str:
    return f"aeradex Agent ({tool}) <agent@aeradex.local>"

HOOK = """#!/bin/sh
# Installed by aeradex: refuse a commit that leaves the book invalid.
exec "{python}" -m aeradex --buch "$(git rev-parse --show-toplevel)" check --quiet
"""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if args and args[0] == "commit":
        # The pre-commit checker is a child of the process holding the book lock.
        env["AERADEX_INTERNAL_CHECK_ROOT"] = str(root)
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=check, env=env)


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


def commit(root: Path, message: str, paths: list[Path] | None = None, verified: bool = False) -> str | None:
    """Stage `paths` (or everything) and commit. Returns the short hash, or None
    when the book is not a repo, commits are disabled, or nothing changed."""
    from .storage import defer_commit
    if defer_commit(root, message, paths):
        return None
    if os.environ.get("AERADEX_NO_COMMIT") or not is_repo(root):
        return None
    rel = None
    if paths is not None:
        rel = sorted({str(Path(p).resolve().relative_to(root)) for p in paths
                      if Path(p).resolve().is_relative_to(root)})
        if not rel:
            return None
        tracked = set(_git(root, "ls-files", "--", *rel, check=False).stdout.splitlines())
        deleted = set(_git(root, "diff", "--cached", "--name-only", "--diff-filter=D", "--", *rel,
                           check=False).stdout.splitlines())
        # A path that is gone and was never committed (a receipt moved out of an
        # untracked inbox) has nothing to stage; git would reject it.
        rel = [r for r in rel if (root / r).exists() or r in tracked or r in deleted]
        if not rel:
            return None
        to_stage = [r for r in rel if (root / r).exists() or r in tracked]
        if to_stage:
            _git(root, "add", "-A", "--", *to_stage)
    else:
        # The [n]/[k] globs keep git (2.55+) from treating the excludes as naming an ignored file, which
        # it rejects ("paths are ignored by one of your .gitignore files") whenever write.lock exists.
        _git(root, "add", "-A", "--", ".", ":(exclude).aeradex/transactio[n]/", ":(exclude).aeradex/write.loc[k]",
             ":(exclude).aeradex/loka[l]/", ":(exclude)**/.aeradex-tmp-*")
    scope = ["--", *rel] if rel is not None else []
    if _git(root, "diff", "--cached", "--quiet", *scope, check=False).returncode == 0:
        return None
    author = AUTHOR.get() or os.environ.get("AERADEX_AUTHOR")
    extra = [f"--author={author}"] if author else []
    # --only leaves unrelated staged work in the index for its own commit.
    only = ["--only", "--", *rel] if rel is not None else []
    # `verified`: aeradex has just run the same check in-process on exactly this state, so the pre-commit hook
    # (a second Python process, ~1 s) is skipped. Commits made by hand outside aeradex still go through the hook.
    hook = ["--no-verify"] if verified else []
    result = _git(root, "commit", "-q", *hook, *extra, "-m", message, *only, check=False)
    if result.returncode != 0:
        raise RuntimeError("git commit abgelehnt:\n" + (result.stdout + result.stderr).strip())
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def log(root: Path, limit: int = 20, autor: str = "", suche: str = "", commits: list[str] | None = None) -> list[dict]:
    """The latest commits; `autor` matches name or e-mail (a server user is `<name>@aeradex`), `suche` the message,
    `commits` limits to these commits (e.g. the ones that touched a Beleg)."""
    if not is_repo(root):
        return []
    args = ["log", f"-{limit}", "--pretty=format:%h\x1f%ad\x1f%an\x1f%s", "--date=iso"]
    if autor:
        args.append(f"--author={autor}")
    if suche:
        args += ["-i", "--fixed-strings", f"--grep={suche}"]
    if commits is not None:
        if not commits:
            return []
        args += ["--no-walk", *commits]
    out = _git(root, *args, check=False).stdout
    rows = []
    for line in out.splitlines():
        h, d, a, s = line.split("\x1f", 3)
        rows.append({"commit": h, "datum": d, "autor": a, "nachricht": s})
    return rows


# ---------- who booked what ----------

_INDEX = ".aeradex/lokal/belegindex.json"
_DATE_CELL = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _journal_belege(lines: list[str], sign: str) -> set[str]:
    found = set()
    for line in lines:
        if not line.startswith(sign + "|"):
            continue
        cells = [c.strip() for c in line[1:].strip().strip("|").split("|")]
        if len(cells) > 1 and _DATE_CELL.match(cells[0]) and cells[1]:
            found.add(cells[1])
    return found


def _scan(root: Path, rng: str, index: dict) -> None:
    out = _git(root, "log", "--reverse", "--no-color", "-p", "--unified=0", "--date=iso",
               "--format=\x1e%h\x1f%ad\x1f%an\x1f%ae\x1f%s", *([rng] if rng else []), "--", "journal/",
               check=False).stdout
    for chunk in out.split("\x1e")[1:]:
        head, _, diff = chunk.partition("\n")
        h, d, name, mail, subject = (head.split("\x1f", 4) + [""] * 5)[:5]
        lines = diff.splitlines()
        added, removed = _journal_belege(lines, "+"), _journal_belege(lines, "-")
        for beleg in sorted(added | removed):
            events = index.setdefault(beleg, [])
            if beleg in added and not events:
                art = "erfasst"
            elif beleg in added:
                art = "geändert"
            else:
                art = "entfernt"
            events.append({"commit": h, "datum": d, "autor": name, "email": mail, "art": art, "nachricht": subject})


def beleg_index(root: Path) -> dict[str, list[dict]]:
    """Per Beleg number the commits that added, changed or removed its journal rows, oldest first — who booked
    what, from the audit trail itself. Cached per HEAD in `.aeradex/lokal/` and extended by the new commits only."""
    if not is_repo(root):
        return {}
    head = _git(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
    if not head:
        return {}
    path = Path(root) / _INDEX
    cached = {}
    try:
        cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        cached = {}
    if cached.get("head") == head:
        return cached.get("index", {})
    index, old = cached.get("index", {}), cached.get("head")
    if old and _git(root, "merge-base", "--is-ancestor", old, head, check=False).returncode == 0:
        _scan(root, f"{old}..{head}", index)
    else:
        index = {}
        _scan(root, "", index)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"head": head, "index": index}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return index


def is_agent(author: str, email: str = "") -> bool:
    return "agent@aeradex" in (email or "") or (author or "").startswith("aeradex Agent")


def short_name(author: str, email: str = "") -> str:
    """'Delia Thomet' → 'DT', an agent → 'Agent'."""
    if is_agent(author, email):
        return "Agent"
    parts = [p for p in re.split(r"[\s.\-_]+", author or "") if p and p[0].isalpha()]
    return ("".join(p[0] for p in parts[:2]).upper() or (author or "?")[:2]).strip()


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

"""The secretary's memory: what she knows about her boss and what you talked about in earlier calls.

A plain Markdown file per book in `.aeradex/lokal/anruf/gedaechtnis.md` (git-ignored, never part of the books):
lasting notes («Marco mag es kurz») and one line per call. The newest part goes into the agent's prompt at the
start of every call, so she can pick up where you left off.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

PROMPT_CHARS = 3000          # how much of the memory goes into a call
MAX_CALL_LINES = 40          # older call summaries are dropped
_lock = threading.Lock()

HEADER = "# Gedächtnis\n\n## Was ich mir merke\n\n## Letzte Gespräche\n"


def path(root: Path) -> Path:
    p = Path(root) / ".aeradex" / "lokal" / "anruf" / "gedaechtnis.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read(root: Path) -> tuple[list[str], list[str]]:
    p = path(root)
    text = p.read_text(encoding="utf-8") if p.exists() else HEADER
    notes, calls, section = [], [], None
    for line in text.splitlines():
        if line.startswith("## Was ich mir merke"):
            section = notes
        elif line.startswith("## Letzte Gespräche"):
            section = calls
        elif line.startswith("- ") and section is not None:
            section.append(line[2:].strip())
    return notes, calls


def _write(root: Path, notes: list[str], calls: list[str]) -> None:
    body = ("# Gedächtnis\n\n## Was ich mir merke\n\n" + "".join(f"- {n}\n" for n in notes)
            + "\n## Letzte Gespräche\n\n" + "".join(f"- {c}\n" for c in calls[-MAX_CALL_LINES:]))
    tmp = path(root).with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path(root))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:400]


def remember_note(root: Path, text: str) -> None:
    with _lock:
        notes, calls = _read(root)
        text = _clean(text)
        if text and text not in notes:
            notes.append(text)
            _write(root, notes, calls)


def add_call(root: Path, summary: str) -> None:
    with _lock:
        notes, calls = _read(root)
        summary = _clean(summary)
        if summary:
            calls.append(f"{time.strftime('%d.%m.%Y %H:%M')}: {summary}")
            _write(root, notes, calls)


def for_prompt(root: Path) -> str:
    """The lasting notes and the most recent calls, newest last, within PROMPT_CHARS."""
    notes, calls = _read(root)
    if not notes and not calls:
        return ""
    out = ["Was du dir gemerkt hast:"] + [f"- {n}" for n in notes] if notes else []
    recent: list[str] = []
    budget = PROMPT_CHARS - sum(len(x) for x in out)
    for c in reversed(calls):
        if budget - len(c) < 0:
            break
        recent.insert(0, f"- {c}")
        budget -= len(c)
    if recent:
        out += ["Eure letzten Gespräche (ältestes zuerst):"] + recent
    return "\n".join(out)


def entries(root: Path) -> tuple[list[str], list[str]]:
    """(lasting notes, call summaries oldest first), for the Nora page."""
    return _read(root)


def save_notes(root: Path, lines: list[str]) -> None:
    """Replace the lasting notes (edited on the Nora page); call summaries stay."""
    with _lock:
        _, calls = _read(root)
        notes = []
        for line in lines:
            line = _clean(line.lstrip("-• ").strip())
            if line and line not in notes:
                notes.append(line)
        _write(root, notes, calls)


def clear_calls(root: Path) -> None:
    with _lock:
        notes, _ = _read(root)
        _write(root, notes, [])


# ---------- the tool ----------

def remember(notiz: str) -> dict:
    """Etwas für spätere Gespräche merken: Vorlieben deines Chefs, wiederkehrende Abmachungen, wichtige Hinweise
    zu Kunden («Marco will Offerten immer mit 30 Tagen Gültigkeit», «Bei Beispiel Bau immer Peter Beispiel anschreiben»).
    Keine Beträge oder Buchungen, die stehen in der Buchhaltung. Nur merken, was er gesagt hat oder klar wünscht.

    Args:
        notiz: ein kurzer Satz.
    """
    from aeradex.book import BookError
    from aeradex.tools import book
    try:
        remember_note(book().root, notiz)
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    return {"ok": True, "gemerkt": _clean(notiz)}

"""Spelling and style of the texts employees type on their phones, before they reach the book (and invoices).

One short call per sync to the book's own agent (Claude Code or Codex with the owner's login, opencode, the Claude
API, or an OpenAI-compatible model such as Google Gemini on the server), without tools. Every answer passes guards: the numbers and measures must stay the same, the length
must stay close, the answer must be clean JSON; otherwise the original text is kept. No agent, no change: the
sync never waits for or fails on the correction.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from aeradex.book import Book

log = logging.getLogger("aeradex.team")
TIMEOUT = 90
# Sonnet: Haiku sometimes changed the meaning («abklept» → «abgeklopft» instead of «abgeklebt»)
API_MODEL = os.environ.get("AERADEX_TEAM_TEXT_MODEL", "claude-sonnet-5-5")

PROMPT = """Du korrigierst kurze Texte aus der Zeiterfassung eines Betriebs in der Schweiz (Branche: {branche}).
Sie stehen später auf Kundenrechnungen.

Für jeden Text:
- Rechtschreibung, Grammatik und Zeichensetzung korrigieren; Schweizer Rechtschreibung, kein ß (immer ss).
- Stil: knapp, sachlich, im Stil eines Leistungsrapports (z.B. «{beispiel}»), keine ganzen
  Sätze nötig, keine Ich-Form, keine Füllwörter. Grossschreibung am Anfang.
- Bedeutung, Fachbegriffe, Zahlen, Masse, Mengen, Räume und Namen NICHT ändern, nichts hinzufügen, nichts weglassen.
- Ist ein Text schon in Ordnung, gib ihn unverändert zurück.
"""
# The texts first, the instruction last: with the instruction right before the JSON, Haiku often answered «{}»
ANSWER = ("\n\nAntworte NUR mit einem JSON-Objekt: dieselben Schlüssel, als Wert jeweils der korrigierte Text. "
          "Ohne Erklärung, ohne Markdown.")


def enabled(book: Book) -> bool:
    from . import daten
    return daten.text_correction(book)


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:[.,]\d+)?", text))


def accept(original: str, proposal) -> str:
    """The corrected text, or the original when the proposal does not pass the guards."""
    if not isinstance(proposal, str):
        return original
    new = " ".join(proposal.replace("ß", "ss").split())
    if not new or len(new) > 300:
        return original
    if _numbers(original.replace(",", ".")) != _numbers(new.replace(",", ".")):
        return original                     # a changed number is never a spelling fix
    if len(new) > len(original) * 1.6 + 12 or len(new) < len(original) * 0.5:
        return original                     # rewritten, not corrected
    return new


def _json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _ask_claude(prompt: str, cwd: str) -> str:
    out = subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--tools", "", "--model", "sonnet",
                          "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""],
                         capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd, stdin=subprocess.DEVNULL)
    return str((_json(out.stdout) or {}).get("result") or "")


def _ask_codex(prompt: str, cwd: str) -> str:
    last = Path(cwd) / "antwort.txt"
    subprocess.run(["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", str(last), prompt],
                   capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd, stdin=subprocess.DEVNULL)
    return last.read_text(encoding="utf-8") if last.exists() else ""


def _ask_opencode(prompt: str, cwd: str) -> str:
    out = subprocess.run(["opencode", "run", prompt], capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd,
                         stdin=subprocess.DEVNULL)
    return out.stdout


def _ask_api(prompt: str, cwd: str) -> str:
    import anthropic
    msg = anthropic.Anthropic().messages.create(model=API_MODEL, max_tokens=2000,
                                                messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def _ask_openai(prompt: str, cwd: str) -> str:
    import urllib.request
    from aeradex.web import chat
    cfg = chat.openai_settings()
    if cfg is None:
        return ""
    req = urllib.request.Request(f"{cfg['base']}/chat/completions", method="POST",
                                 data=json.dumps({"model": cfg["model"],
                                                  "messages": [{"role": "user", "content": prompt}]}).encode(),
                                 headers={"authorization": f"Bearer {cfg['key']}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        reply = json.loads(resp.read())
    return str(((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


ASK = {"claude-code": _ask_claude, "codex": _ask_codex, "opencode": _ask_opencode, "api": _ask_api,
       "openai": _ask_openai}


def backend(book: Book) -> str | None:
    try:
        from aeradex.web import chat
    except ImportError:
        return None
    kind = chat.backend(book.settings.get("agent_backend"))
    if kind == "claude-code" and not shutil.which("claude"):
        return None
    return kind if kind in ASK else None


def ask(book: Book, texts: dict[str, str]) -> dict[str, str]:
    """The raw answer of the book's agent: {key: corrected text}. Empty when there is no agent or it fails."""
    kind = backend(book)
    if not kind or not texts:
        return {}
    from . import daten
    t = daten.texts(book)
    prompt = PROMPT.format(branche=t["branche"], beispiel=t["text_beispiel"] or "Leitungen verlegt, Anschlüsse geprüft") \
        + "\nTexte (JSON, Schlüssel → Text):\n" + json.dumps(texts, ensure_ascii=False, indent=0) + ANSWER
    with tempfile.TemporaryDirectory(prefix="aeradex-text-") as tmp:   # no book context, no project files
        try:
            raw = ASK[kind](prompt, tmp)
            answer = _json(raw)
            if not answer:
                log.warning("Textkorrektur (%s): Antwort nicht lesbar, Texte bleiben unverändert: %r", kind, raw[:300])
            return answer
        except Exception as exc:                                       # an SDK error must not stop the sync
            log.warning("Textkorrektur (%s) fehlgeschlagen, Texte bleiben unverändert: %s: %s",
                        kind, type(exc).__name__, str(exc)[:300])
            return {}


def polish(book: Book, texts: dict[str, str]) -> dict[str, str]:
    """{key: text} → {key: corrected text} for the texts that changed and passed the guards."""
    wanted = {k: v for k, v in texts.items() if v and re.search(r"[A-Za-zÄÖÜäöü]{3}", v)}
    if not wanted or not enabled(book):
        return {}
    answer = ask(book, wanted)
    out = {}
    for key, original in wanted.items():
        new = accept(original, answer.get(key))
        if new != original:
            out[key] = new
    return out

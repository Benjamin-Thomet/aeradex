"""Pending actions: what the remote agent may only *ask* for.

Issuing an invoice or approving proposals books into the journal. From a call
or Telegram the agent never does that itself: it files a pending action, the person
gets a card with buttons, and the button runs the action deterministically
(no LLM) under the person's name. Each action runs at most once and expires.

The store lives in `.aeradex/lokal/anruf/` (git-ignored in every book).
"""
from __future__ import annotations

import contextvars
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from aeradex import api, gitlog
from aeradex.book import Book, BookError

TTL_SECONDS = 24 * 3600


class Channel(Protocol):
    """Where results go: the call page or the person's Telegram chat."""

    def send_text(self, text: str) -> None: ...

    def send_document(self, path: Path, caption: str = "") -> None: ...

    def send_card(self, text: str, buttons: list[tuple[str, str]]) -> None: ...


# Set by the agent for the duration of a turn (tools run in the agent's thread).
CHANNEL: contextvars.ContextVar[Channel | None] = contextvars.ContextVar("aeradex_anruf_channel", default=None)


def store(root: Path) -> Path:
    path = Path(root) / ".aeradex" / "lokal" / "anruf" / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create(root: Path, kind: str, payload: dict, summary: str) -> str:
    action_id = uuid.uuid4().hex[:12]
    data = {"id": action_id, "art": kind, "payload": payload, "zusammenfassung": summary, "erstellt": time.time()}
    tmp = store(root) / f"{action_id}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(store(root) / f"{action_id}.json")
    return action_id


def take(root: Path, action_id: str) -> dict:
    """Claim an action exactly once: the file is renamed away before anything runs."""
    if not action_id.isalnum():
        raise BookError("Ungültige Aktion")
    path = store(root) / f"{action_id}.json"
    claimed = store(root) / f"{action_id}.{os.getpid()}.{uuid.uuid4().hex[:6]}.claimed"
    try:
        path.replace(claimed)
    except FileNotFoundError:
        raise BookError("Diese Aktion wurde schon ausgeführt, verworfen oder ist abgelaufen.") from None
    data = json.loads(claimed.read_text(encoding="utf-8"))
    claimed.unlink()
    if time.time() - float(data.get("erstellt", 0)) > TTL_SECONDS:
        raise BookError("Diese Aktion ist abgelaufen (älter als 24 Stunden). Bitte neu anfragen.")
    return data


BUTTONS = {"rechnung": ("✅ Ausstellen", "✖ Verwerfen"), "freigabe": ("✅ Freigeben", "✖ Später")}


def buttons(action: dict) -> list[tuple[str, str]]:
    yes, no = BUTTONS.get(action["art"], ("✅ OK", "✖ Verwerfen"))
    return [(yes, f"ok:{action['id']}"), (no, f"no:{action['id']}")]


def show_card(root: Path, action_id: str, channel: Channel) -> None:
    """Show a pending action as a card with its buttons."""
    path = store(root) / f"{action_id}.json"
    if action_id.isalnum() and path.exists():
        action = json.loads(path.read_text(encoding="utf-8"))
        channel.send_card(action["zusammenfassung"], buttons(action))


def pending(root: Path) -> list[dict]:
    out = []
    for path in sorted(store(root).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("erstellt", 0)) <= TTL_SECONDS:
            out.append(data)
        else:
            path.unlink(missing_ok=True)
    return out


# ---------- what a confirmed action does ----------

@dataclass
class Outcome:
    text: str
    pdf: Path | None = None


def _run_invoice(book: Book, p: dict) -> Outcome:
    res = api.invoice_create(book, p["kunde"], p["positionen"], p.get("datum") or None, p.get("text", ""),
                             leistung_von=p.get("leistung_von") or None, leistung_bis=p.get("leistung_bis") or None)
    r = res["rechnung"]
    return Outcome(f"Rechnung {r['nummer']} an {r['an']['name']} über {r['waehrung']} {r['total']} ausgestellt.",
                   book.root / res["pdf"])


def _run_approve(book: Book, p: dict) -> Outcome:
    res = api.approve(book, p["ids"])
    return Outcome(res.get("meldung") or f"Freigegeben: {', '.join(p['ids'])}")


RUNNERS: dict[str, Callable[[Book, dict], Outcome]] = {"rechnung": _run_invoice, "freigabe": _run_approve}


def confirm(root: Path, action_id: str, author: str | None = None) -> Outcome:
    """Run a pending action as the person who tapped the button."""
    data = take(root, action_id)
    runner = RUNNERS.get(data["art"])
    if runner is None:
        raise BookError(f"Unbekannte Aktion: {data['art']}")
    token = gitlog.AUTHOR.set(author or None)        # None = the book's git user (the person at this machine)
    try:
        return runner(Book(root), data["payload"])
    finally:
        gitlog.AUTHOR.reset(token)


def discard(root: Path, action_id: str) -> str:
    """Drop a pending action. Proposals behind a «freigabe» simply stay open."""
    data = take(root, action_id)
    return f"Verworfen: {data['zusammenfassung'].splitlines()[0]}"


# ---------- tools the remote agent gets instead of the booking ones ----------

def _channel() -> Channel | None:
    return CHANNEL.get()


def request_invoice(kunde: str, positionen: list[dict], datum: str = "", text: str = "",
                    leistung_von: str = "", leistung_bis: str = "") -> dict:
    """Rechnung zur Freigabe vorlegen. Stellt NICHT aus: der Mensch bekommt eine Karte mit den vom System
    gerechneten Beträgen und tippt «Ausstellen» oder «Verwerfen». Vorher Kunde, Positionen und Total mit ihm klären.

    Args:
        kunde: Kundennummer, z.B. "K0001" (customers() zeigt sie).
        positionen: z.B. [{"text": "Malerarbeiten", "menge": 8, "einheit": "h", "preis": "95.00"}] (konto optional).
        datum: Rechnungsdatum JJJJ-MM-TT; leer = heute.
        text: Einleitungstext auf der Rechnung.
        leistung_von: Leistungsdatum bzw. Beginn des Leistungszeitraums JJJJ-MM-TT, falls abweichend.
        leistung_bis: Ende des Leistungszeitraums JJJJ-MM-TT.
    """
    from aeradex.tools import book
    try:
        b = book()
        preview = api.invoice_check(b, kunde, positionen, datum or None)
    except (BookError, ValueError, KeyError) as exc:
        return {"ok": False, "fehler": str(exc)}
    lines = [f"🧾 Rechnung an {preview['an'].get('name', kunde)} ({preview['kunde']}), {preview['datum']}"]
    for p in preview["positionen"]:
        lines.append(f"• {p.get('text', '')}: {p.get('menge')} {p.get('einheit') or ''} × {p.get('preis')} = {p.get('betrag')}")
    for m in preview.get("mwst") or []:
        lines.append(f"MWST {m.get('code', '')} {m.get('satz', '')}%: {m.get('steuer')}")
    lines.append(f"Total {preview['waehrung']} {preview['total']}")
    summary = "\n".join(lines)
    payload = {"kunde": preview["kunde"], "positionen": positionen, "datum": preview["datum"], "text": text,
               "leistung_von": leistung_von, "leistung_bis": leistung_bis}
    action_id = create(b.root, "rechnung", payload, summary)
    if _channel():
        show_card(b.root, action_id, _channel())
    return {"ok": True, "aktion": action_id, "total": preview["total"], "waehrung": preview["waehrung"],
            "hinweis": "Dem Menschen als Karte zur Freigabe vorgelegt. Noch nicht ausgestellt."}


def request_approval(ids: list[str]) -> dict:
    """Buchungsvorschläge dem Menschen zur Freigabe vorlegen (Karte mit «Freigeben»). Bucht NICHT selbst.

    Args:
        ids: Vorschlags-IDs aus list_proposals(), z.B. ["V-003"].
    """
    from aeradex.tools import book
    try:
        b = book()
        known = {str(r.get("ID")): r for r in api.proposals(b)}
    except (BookError, ValueError) as exc:
        return {"ok": False, "fehler": str(exc)}
    missing = [i for i in ids if i not in known]
    if missing or not ids:
        return {"ok": False, "fehler": f"Unbekannte Vorschläge: {', '.join(missing) or '(keine angegeben)'}"}
    lines = ["📒 Buchungsvorschläge freigeben?"]
    for i in ids:
        r = known[i]
        lines.append(f"• {i} {r.get('Datum', '')} {r.get('Text', '')}: {r.get('Soll', '')}/{r.get('Haben', '')} {r.get('Betrag', '')}")
    summary = "\n".join(lines)
    action_id = create(b.root, "freigabe", {"ids": list(ids)}, summary)
    if _channel():
        show_card(b.root, action_id, _channel())
    return {"ok": True, "aktion": action_id, "hinweis": "Dem Menschen als Karte zur Freigabe vorgelegt."}


DOCUMENT_FOLDERS = ("rechnungen", "offerten", "leistungen", "kreditoren", "belege", "eingang", "inbox", "lohn", "berichte",
                    "mahnungen", "abschluss")


def send_document(pfad: str, beschriftung: str = "") -> dict:
    """Ein PDF oder Bild aus dem Buch dem Menschen zeigen, z.B. eine Offerte, Rechnung oder einen Beleg (Anrufseite bzw. Telegram-Chat).

    Args:
        pfad: Pfad relativ zum Buch, z.B. "rechnungen/2026/R-2026-0007.pdf" (Tools nennen ihn unter «dateien» bzw. «pdf»).
        beschriftung: kurze Bildunterschrift.
    """
    from aeradex.tools import book
    try:
        b = book()
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    path = (b.root / pfad).resolve()
    if not path.is_relative_to(b.root.resolve()) or path.relative_to(b.root.resolve()).parts[:1] not in [(f,) for f in DOCUMENT_FOLDERS]:
        return {"ok": False, "fehler": f"Nur Dokumente aus {', '.join(DOCUMENT_FOLDERS)}"}
    if not path.is_file():
        return {"ok": False, "fehler": f"Datei nicht gefunden: {pfad}"}
    ch = _channel()
    if ch is None:
        if os.environ.get("AERADEX_ANRUF"):     # MCP server of a Claude Code call: the agent shows it (agent.py)
            return {"ok": True, "zeigen": b.rel(path), "beschriftung": beschriftung}
        return {"ok": False, "fehler": "Kein Kanal verbunden"}
    ch.send_document(path, beschriftung)
    return {"ok": True, "gesendet": b.rel(path)}


def pending_overview() -> dict:
    """Was auf deine Freigabe wartet: offene Karten, Buchungsvorschläge und Belegentwürfe."""
    from aeradex.tools import book
    try:
        b = book()
        return {"aktionen": [{"id": a["id"], "art": a["art"], "zusammenfassung": a["zusammenfassung"]} for a in pending(b.root)],
                "vorschlaege": api.proposals(b), "entwuerfe": api.bill_drafts(b)}
    except (BookError, ValueError) as exc:
        return {"ok": False, "fehler": str(exc)}


from .gedaechtnis import remember  # noqa: E402  (the secretary's notes, see gedaechtnis.py)

REMOTE_TOOLS = [request_invoice, request_approval, send_document, pending_overview, remember]

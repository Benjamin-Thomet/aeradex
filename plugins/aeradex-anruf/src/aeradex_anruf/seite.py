"""The Telegram-Agent page in aeradex (Einstellungen › Agent & KI): status, call button, cards, memory, settings."""
from __future__ import annotations

import re
import secrets
import time

from aeradex.book import Book, BookError
from aeradex.plugins import Page

from . import actions, config, dienst, gedaechtnis
from .config import ENV_FILE, Config, read_env_file, write_env_value

_voice_names: dict[str, str] = {}


def _voice_name(cfg: Config) -> str:
    """The voice's name at ElevenLabs (cached; the page must not wait for the network)."""
    vid = cfg.elevenlabs_voice_id
    if not vid or not cfg.elevenlabs_key:
        return vid or "Standard"
    if vid not in _voice_names:
        try:
            from .elevenlabs import _client
            with _client(cfg) as c:
                c.timeout = 3.0
                _voice_names[vid] = (c.get(f"/v1/voices/{vid}").json().get("name") or vid).split(" - ")[0]
        except Exception:
            return vid
    return _voice_names[vid]


def _context(book: Book, query: dict) -> dict:
    cfg = Config.load(config.ENV_FILE)
    notes, calls = gedaechtnis.entries(book.root)
    st = dienst.status(book.root)
    if st:
        mins = int((time.time() - st["seit"]) // 60)
        st["seit_text"] = f"seit {mins // 60} h {mins % 60} min" if mins >= 60 else f"seit {mins} min"
    return {
        "name": cfg.assistant_name, "chef": cfg.boss_name, "dienst": st,
        "karten": actions.pending(book.root),
        "notizen": notes, "gespraeche": list(reversed(calls)),
        "einstellungen": [
            ("Name", cfg.assistant_name), ("Chef", cfg.boss_name or "–"),
            ("Stimme", _voice_name(cfg)),
            ("Agent", {"claude-code": "Claude Code (dein Abo)", "api": "Claude API",
                       "openai": "OpenAI-kompatibel (Gemini, Einstellungen › Agent)"}.get(cfg.backend, cfg.backend)),
            ("Modell im Gespräch", cfg.voice_model), ("Für schwere Aufgaben", cfg.expert_model or "–"),
            ("Werkzeuge", "alle, wie im MCP-Chat" if cfg.tools_mode != "entwuerfe" else "nur Entwürfe, Ausstellen per Karte"),
            ("Telegram", "eingerichtet" if cfg.bot_token else "nicht eingerichtet"),
            ("Konfiguration", str(ENV_FILE)),
        ],
        "fehlt": cfg.problems_call(),
        "zugang": _access(cfg),
    }


def _masked(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 8 else ("gesetzt" if value else "")


def _access(cfg: Config) -> dict:
    """What the form shows: secrets only as their last characters."""
    return {"telegram_token": _masked(cfg.bot_token), "telegram_ids": " ".join(str(u) for u in sorted(cfg.allowed_users)),
            "elevenlabs_key": _masked(cfg.elevenlabs_key), "voice_id": cfg.elevenlabs_voice_id,
            "name": cfg.assistant_name, "chef": cfg.boss_name, "backend": cfg.backend, "datei": str(config.ENV_FILE)}


def _zugaenge(book: Book, form: dict) -> dict:
    """Store the bot's access in its env file (outside the book, chmod 600). Empty secret fields keep the old value.
    A bot running as a service (`telegram serve --dienst`) notices the change and restarts with it."""
    token = str(form.get("telegram_token") or "").strip()
    if token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        raise BookError("Telegram-Token: so wie von @BotFather, z.B. 123456789:AA…")
    ids = str(form.get("telegram_ids") or "").replace(",", " ").split()
    if any(not i.isdigit() for i in ids):
        raise BookError("Telegram-IDs: nur Zahlen (z.B. von @userinfobot), mehrere mit Leerzeichen")
    key = str(form.get("elevenlabs_key") or "").strip()
    if key and (any(c.isspace() for c in key) or "=" in key):
        raise BookError("ElevenLabs-Schlüssel ohne Leerzeichen und =")
    backend = str(form.get("backend") or "")
    if backend not in ("claude-code", "api", "openai"):
        raise BookError("Agent: claude-code, api oder openai")
    values = {"TELEGRAM_ALLOWED_USER_IDS": " ".join(ids), "ELEVENLABS_VOICE_ID": str(form.get("voice_id") or "").strip(),
              "AERADEX_ANRUF_NAME": str(form.get("name") or "").strip(), "AERADEX_ANRUF_CHEF": str(form.get("chef") or "").strip(),
              "AERADEX_ANRUF_BACKEND": backend}
    if token:
        values["TELEGRAM_BOT_TOKEN"] = token
    if key:
        values["ELEVENLABS_API_KEY"] = key
    path = config.ENV_FILE                 # read now, not at import (tests and services point it elsewhere)
    if len(read_env_file(path).get("LLM_SHARED_SECRET", "")) < 24:
        values["LLM_SHARED_SECRET"] = secrets.token_hex(32)
    for k, v in values.items():
        if v or k in ("TELEGRAM_ALLOWED_USER_IDS", "ELEVENLABS_VOICE_ID"):
            write_env_value(k, v, path)
    return {"meldung": "Zugänge gespeichert. Läuft der Bot als Dienst, startet er gleich neu."}


def _notizen(book: Book, form: dict) -> dict:
    gedaechtnis.save_notes(book.root, str(form.get("notizen") or "").splitlines())
    return {"meldung": "Gedächtnis gespeichert"}


def _verlauf_leeren(book: Book, form: dict) -> dict:
    gedaechtnis.clear_calls(book.root)
    return {"meldung": "Gesprächsnotizen gelöscht"}


def _karte(book: Book, form: dict) -> dict:
    verb, _, action_id = str(form.get("data") or "").partition(":")
    if verb == "ok":
        outcome = actions.confirm(book.root, action_id, None)       # as the person at aeradex (git user)
        return {"meldung": outcome.text}
    if verb == "no":
        return {"meldung": actions.discard(book.root, action_id)}
    raise BookError("Unbekannte Aktion")


def _anrufen(book: Book, form: dict) -> dict:
    """«Anrufen»: a one-time ticket from the running service, then on to its call page. Works when aeradex runs on a
    server too (with PUBLIC_URL the call page is reached there); the page key itself never reaches the browser."""
    from urllib.parse import parse_qs, urlsplit
    st = dienst.status(book.root)
    if not st:
        raise BookError("Der Dienst läuft nicht")
    link = urlsplit(st.get("anrufseite") or "")
    key = (parse_qs(link.query).get("key") or [""])[0]
    from .elevenlabs import httpx
    try:
        r = httpx.post(f"http://127.0.0.1:{int(st['port'])}/anruf/ticket", headers={"X-Aeradex-Key": key}, timeout=5)
        r.raise_for_status()
        ticket = r.json()["ticket"]
    except (httpx.HTTPError, KeyError, ValueError):
        raise BookError("Der Dienst antwortet nicht. Kurz warten und nochmals versuchen.") from None
    base = (Config.load(config.ENV_FILE).public_url or f"{link.scheme}://{link.netloc}").rstrip("/")
    return {"weiter": f"{base}/anruf?ticket={ticket}"}


def pages() -> list[Page]:
    return [Page("sekretaerin", "Telegram-Agent", "sekretaerin.html", _context,
                 {"notizen": _notizen, "verlauf_leeren": _verlauf_leeren, "karte": _karte, "zugaenge": _zugaenge,
                  "anrufen": _anrufen}, bereich="einstellungen:agent")]

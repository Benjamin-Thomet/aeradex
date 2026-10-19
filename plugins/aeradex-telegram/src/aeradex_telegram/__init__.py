"""Telegram für aeradex-anruf: mit dem Agenten chatten, Sprachnachrichten schicken und aus Telegram anrufen.

Baut auf dem Plugin anruf auf (Agent, Karten, ElevenLabs, Gateway); dieses Plugin bringt nur den Bot und die
Telegram-Mini-App. Ausstellen, Buchen und Freigeben bestätigst du mit einem Knopf im Chat.
"""
from __future__ import annotations

import threading

from aeradex.plugins import Command, hookimpl

AERADEX_PLUGIN_API = 1
__version__ = "0.1.0"


def _setup(parser):
    parser.add_argument("aktion", choices=["serve", "status"], nargs="?", default="status",
                        help="serve: Bot und Gateway starten · status: Konfiguration prüfen")
    parser.add_argument("--dienst", action="store_true",
                        help="als Dienst (systemd): auf fehlende Zugänge warten und bei geänderten neu starten")


def _problems(book, cfg) -> list[str]:
    out = cfg.problems_telegram()
    if "anruf" not in (book.settings.get("plugins") or []):
        out.append("Plugin anruf ist für dieses Buch nicht eingeschaltet (aeradex plugins ein anruf)")
    return out


def _run(book, args):
    from aeradex_anruf.config import ENV_FILE, Config
    cfg = Config.load()
    if args.aktion == "status":
        return {"konfiguration": str(ENV_FILE), "fehlt": _problems(book, cfg), "anrufen_bereit": cfg.voice_ready(),
                "erlaubte_benutzer": sorted(cfg.allowed_users), "agent": cfg.backend}
    problems = _problems(book, cfg)
    if problems and not args.dienst:
        return {"ok": False, "fehler": "Konfiguration unvollständig (" + str(ENV_FILE) + "): " + "; ".join(problems)}
    if args.dienst:
        cfg = _wait_until_ready(book)
        _restart_on_change(ENV_FILE)
    return serve(book.root, cfg)


def _stamp(path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _wait_until_ready(book, every: float = 30):
    """As a service: the access may still be missing (entered later in aeradex). Wait instead of crash-looping."""
    import time

    from aeradex_anruf.config import Config
    said = None
    while True:
        cfg = Config.load()
        problems = _problems(book, cfg)
        if not problems:
            return cfg
        if problems != said:
            print("Telegram-Bot wartet auf: " + "; ".join(problems), flush=True)
            said = problems
        time.sleep(every)


def _restart_on_change(path, every: float = 15) -> None:
    """Changed access (aeradex › Sekretärin › Zugänge): end the process, systemd starts it with the new values."""
    import os
    import time
    first = _stamp(path)

    def watch():
        while True:
            time.sleep(every)
            if _stamp(path) != first:
                print("Zugänge geändert: Telegram-Bot startet neu", flush=True)
                os._exit(3)
    threading.Thread(target=watch, name="zugaenge-watch", daemon=True).start()


def serve(root, cfg) -> dict:
    import logging

    import uvicorn
    from aeradex_anruf import setup_logging

    import secrets

    from aeradex_anruf.anruf import Feed

    from .bot import TelegramBot, create_app
    setup_logging()
    tg = TelegramBot(root, cfg)
    threading.Thread(target=tg.poll_forever, name="telegram-poll", daemon=True).start()
    if not cfg.voice_ready():
        logging.getLogger("aeradex.telegram").info(
            "Anrufen nicht eingerichtet (PUBLIC_URL/ELEVENLABS_*): nur Chat und Sprachnachrichten.")
    key = secrets.token_urlsafe(24)            # the browser call page runs in the same process
    link = f"http://127.0.0.1:{cfg.port}/anruf?key={key}"
    print(f"\n  Telegram-Bot läuft. Anrufseite im Browser: {link}\n", flush=True)
    from aeradex_anruf import dienst
    dienst.announce(root, link, telegram=True, port=cfg.port)
    try:
        uvicorn.run(create_app(root, cfg, tg, feed=Feed(), page_key=key), host=cfg.host, port=cfg.port,
                    log_level="warning", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    finally:
        tg.stop.set()
    return {"ok": True}


# How the settings show this plugin (Einstellungen › Funktionen).
AERADEX_TITEL = "Telegram"
AERADEX_TEXT = "Chat, Sprachnachrichten und Anrufen aus Telegram"
AERADEX_TEIL_VON = "anruf"


@hookimpl
def aeradex_commands():
    return [Command("telegram", "Telegram-Bot und Anrufen aus Telegram (braucht das Plugin anruf)", _run, _setup)]

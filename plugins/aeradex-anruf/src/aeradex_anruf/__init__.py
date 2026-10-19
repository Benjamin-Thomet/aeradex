"""Anrufen: mit dem aeradex-Agenten sprechen wie mit einem Mitarbeiter (ElevenLabs-Stimme, Claude als Agent).

ElevenLabs macht nur die Sprache und fragt für jede Antwort den Gateway dieses Plugins (Custom LLM); dort
arbeitet Claude mit den aeradex-Werkzeugen, standardmässig über Claude Code mit deinem eigenen Login. Was bucht
oder ausstellt, legt der Agent nur als Karte vor; du bestätigst sie auf der Anrufseite.
"""
from __future__ import annotations

import logging
import os
import threading

from aeradex.plugins import Command, hookimpl

AERADEX_PLUGIN_API = 1
__version__ = "0.1.0"


# How the settings show this plugin (Einstellungen › Funktionen).
AERADEX_TITEL = "Telegram-Agent"
AERADEX_TEXT = "Der Agent per Telegram und Telefon: Fragen stellen, Belege schicken, buchen lassen"


@hookimpl
def aeradex_tools():
    # Only for the MCP server of a call/chat agent (started with AERADEX_ANRUF=1); the UI and other MCP
    # clients keep their own tools and never see cards nobody would look at.
    if os.environ.get("AERADEX_ANRUF"):
        from .actions import REMOTE_TOOLS
        return list(REMOTE_TOOLS)
    return []


def _setup(parser):
    parser.add_argument("aktion", choices=["serve", "elevenlabs-setup", "status"], nargs="?", default="status",
                        help="serve: Anrufseite und Gateway starten · elevenlabs-setup: Sprach-Agent anlegen/aktualisieren · "
                             "status: Konfiguration prüfen")
    parser.add_argument("--begruessung", default="{{begruessung}}",
                        help="erster Satz im Anruf (elevenlabs-setup); Standard: je nach Tageszeit mit deinem Vornamen")
    parser.add_argument("--kein-browser", action="store_true", help="die Anrufseite nicht automatisch öffnen")


def _run(book, args):
    from .config import ENV_FILE, Config
    cfg = Config.load()
    if args.aktion == "status":
        return {"konfiguration": str(ENV_FILE), "fehlt": cfg.problems_call(), "agent": cfg.backend,
                "modell_im_anruf": cfg.voice_model, "public_url": cfg.public_url, "gateway": f"http://{cfg.host}:{cfg.port}"}
    if args.aktion == "elevenlabs-setup":
        from . import elevenlabs
        return elevenlabs.setup_agent(cfg, args.begruessung)
    problems = cfg.problems_call()
    if problems:
        return {"ok": False, "fehler": "Konfiguration unvollständig (" + str(ENV_FILE) + "): " + "; ".join(problems)}
    return serve(book.root, cfg, open_browser=not args.kein_browser)


def serve(root, cfg, open_browser: bool = True) -> dict:
    """The call page on this machine (with an access key in the link) plus the gateway for ElevenLabs."""
    import secrets
    import webbrowser

    import uvicorn

    from .anruf import Feed
    from .gateway import create_app
    setup_logging()
    key = secrets.token_urlsafe(24)
    link = f"http://127.0.0.1:{cfg.port}/anruf?key={key}"
    print(f"\n  Anrufseite: {link}\n  (nur auf diesem Rechner öffnen; der Link ist der Zugang)"
          f"\n  Agent: {'Claude Code (dein Login)' if cfg.backend == 'claude-code' else 'Claude API'}\n", flush=True)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(link)).start()
    from . import dienst
    dienst.announce(root, link, telegram=False, port=cfg.port)
    uvicorn.run(create_app(root, cfg, feed=Feed(), page_key=key), host=cfg.host, port=cfg.port, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return {"ok": True}


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):    # their request log can carry tokens in the URL
        logging.getLogger(noisy).setLevel(logging.WARNING)


@hookimpl
def aeradex_pages():
    from .seite import pages
    return pages()


@hookimpl
def aeradex_commands():
    return [Command("anruf", "Mit dem Agenten sprechen (ElevenLabs-Stimme, Claude als Agent), im Browser", _run, _setup)]

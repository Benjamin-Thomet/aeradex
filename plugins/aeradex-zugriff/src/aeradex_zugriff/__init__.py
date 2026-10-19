"""Zugriff: who may reach this book's tools from their own PC (Claude Code/Codex over ssh), managed in aeradex.

For a book on a server (`aeradex serve`): each person's key is locked to a forced command (`aeradex-ssh`) that can
start the aeradex tools (MCP) and put a receipt into inbox/, nothing else. See `deploy/README.md`.
"""
from __future__ import annotations

from importlib import resources

from aeradex.book import Book, BookError
from aeradex.plugins import Command, Page, hookimpl

from . import schluessel

AERADEX_PLUGIN_API = 1
__version__ = "0.1.0"


def ssh_script() -> str:
    """The forced command, to install as root: `aeradex zugriff skript | sudo install -m 755 /dev/stdin …`."""
    return resources.files(__package__).joinpath("aeradex-ssh").read_text(encoding="utf-8")


def _context(book: Book, query: dict) -> dict:
    return {"aktiv": schluessel.keys_file() is not None, "schluessel": schluessel.entries(), "s": schluessel.server()}


def _neu(book: Book, f: dict) -> dict:
    res = schluessel.add(f.get("wer") or "", f.get("name") or "", f.get("schluessel") or "")
    return {"meldung": f"Zugang für {res['wer']} eingetragen ({res['fingerprint'][:20]}…)"}


def _weg(book: Book, f: dict) -> dict:
    schluessel.remove(f.get("fingerprint") or "")
    return {"meldung": "Zugang entfernt"}


@hookimpl
def aeradex_pages():
    return [Page("schluessel", "Zugriff (Claude Code)", "zugriff.html", _context, {"neu": _neu, "weg": _weg},
                 bereich="einstellungen:buch")]


def _run(book, args):
    if args.zugriff_cmd == "skript":
        print(ssh_script(), end="")
        return None
    if args.zugriff_cmd == "liste":
        if schluessel.keys_file() is None:
            raise BookError("AERADEX_ZUGRIFF_KEYS ist nicht gesetzt (nur auf dem Server)")
        return schluessel.entries()
    if args.zugriff_cmd == "neu":
        return schluessel.add(args.wer, args.name, args.schluessel)
    if args.zugriff_cmd == "weg":
        return schluessel.remove(args.fingerprint)
    return None


def _setup(parser):
    sub = parser.add_subparsers(dest="zugriff_cmd", required=True)
    sub.add_parser("skript", help="Das SSH-Skript (Forced command) ausgeben, zum Installieren als root")
    sub.add_parser("liste", help="Eingetragene Schlüssel")
    s = sub.add_parser("neu", help="Schlüssel eintragen")
    s.add_argument("wer", help="Kürzel, z.B. anna")
    s.add_argument("name", help="Name im Verlauf")
    s.add_argument("schluessel", help="öffentlicher Schlüssel «ssh-ed25519 AAAA… kommentar»")
    s = sub.add_parser("weg", help="Schlüssel entfernen")
    s.add_argument("fingerprint")


@hookimpl
def aeradex_commands():
    return [Command("zugriff", "Zugriff für Claude Code per SSH (auf dem Server)", _run, _setup, needs_book=False)]

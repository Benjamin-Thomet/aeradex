"""Beispiel-Plugin: eine Prüfregel, ein Agenten-Werkzeug und ein Befehl.

Die erste Zeile dieses Docstrings erscheint in `allkvitt plugins`.
Jeder Hook ist optional — ein Plugin implementiert nur, was es braucht.
"""
from __future__ import annotations

from allkvitt import api
from allkvitt.plugins import Command, Finding, hookimpl

# Pflicht: die Version der Plugin-API, gegen die dieses Plugin geschrieben ist.
ALLKVITT_PLUGIN_API = 1
__version__ = "0.1.0"


# ---- Prüfregel: läuft bei jedem `allkvitt check` und vor/nach jedem Schreibvorgang ----
# Stufen: "fehler" (blockiert jedes Schreiben), "warnung", "hinweis". Sparsam mit "fehler".

@hookimpl
def allkvitt_check(book, rows):
    return [Finding("hinweis", r.where, f"Beleg {r.beleg}: Buchungstext ist sehr kurz")
            for r in rows if not r.quelle and len(r.text.strip()) < 4]


# ---- Agenten-Werkzeug: erscheint im MCP-Server und im Chat der Oberfläche ----
# Name, Docstring und Args-Beschreibung sind das, was der Agent sieht — präzise schreiben.
# `call` holt das Buch (aus $ALLKVITT_BUCH bzw. dem Chat) und macht aus Fehlern {"ok": False, "fehler": …}.

def beispiel_kurze_texte() -> dict:
    """Buchungen mit sehr kurzem Text finden (Kandidaten für einen besseren Buchungstext)."""
    from allkvitt.tools import call

    def find(book):
        return {"belege": sorted({r.beleg for r in book.rows if not r.quelle and len(r.text.strip()) < 4})}
    return call(find)


@hookimpl
def allkvitt_tools():
    return [beispiel_kurze_texte]


@hookimpl
def allkvitt_instructions():
    return "- beispiel_kurze_texte: zeigt Belege mit zu kurzem Buchungstext."


# ---- Befehl: allkvitt beispiel-kurz ----
# Schreiben nur über api.write(book, "Commit-Nachricht", funktion, …): das Buch wird vorher und
# nachher geprüft und die Änderung in git festgehalten — oder abgelehnt.

def _run(book, args):
    return {"belege": sorted({r.beleg for r in book.rows if not r.quelle and len(r.text.strip()) < args.min})}


def _setup(parser):
    parser.add_argument("--min", type=int, default=4, help="Mindestlänge des Texts")


@hookimpl
def allkvitt_commands():
    return [Command("beispiel-kurz", "Belege mit zu kurzem Text anzeigen", _run, _setup)]


# ---- Weitere Hooks (siehe docs/plugins.md) ----
# allkvitt_kontenplaene()   -> {"name": Path("vorlage.yaml")}       Kontenplan-Vorlagen (Daten)
# allkvitt_qst_tarife()     -> Path("ordner")                        Quellensteuer-Tabellen (Daten)
# allkvitt_bank_formats()   -> [BankFormat(...)]                     Kontoauszugsformate (Beispiel: allkvitt-revolut)
# allkvitt_sources()        -> [Source(prefix, label, rows, link)]   Dokumente, denen Journalzeilen gehören
#                                                                  (Beispiel: tests/plugin_sample.py im allkvitt-Repo)

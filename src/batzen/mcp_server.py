"""MCP server: the batzen operations as tools for Claude Code, opencode, agy …

    claude mcp add batzen -- batzen --buch /pfad/zum/buch mcp

Each call opens the book fresh, so edits made in between (by a person, by git,
by another agent) are always seen. With `agent_modus: vorschlag` in
batzen.yaml (the default) the agent cannot post free journal entries itself:
it proposes them, and a person approves with `batzen approve`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import api
from .book import Book, BookError, find_root
from .files import FormatError

INSTRUCTIONS = """batzen ist eine Schweizer Buchhaltung (OR/KMU-Kontenrahmen, CHF) als Textdateien in git.
Regeln:
- Rechne nie selbst Summen, Salden, Löhne oder MWST — frage die Tools (balance, ledger, report, payroll_run).
- Vor dem Buchen: accounts() nach dem passenden Konto durchsuchen. Soll = wohin das Geld/der Wert fliesst
  (Aufwand, Aktiven nehmen zu), Haben = woher (Ertrag, Bank bei Zahlung, Passiven nehmen zu).
- Im agent_modus 'vorschlag' nur propose_booking verwenden; ein Mensch gibt frei.
- Gebuchtes wird nie gelöscht: Korrekturen per reverse_entry (Storno) bzw. Rechnungen per void/credit.
- Nach jeder Änderung ist das Buch geprüft und in git committet; bei Fehlern die Meldung lesen und korrigieren.
- Beträge als Text mit Punkt: "1234.50". Datum als JJJJ-MM-TT.
"""

server = MCPServer("batzen", instructions=INSTRUCTIONS)


def _book() -> Book:
    env = os.environ.get("BATZEN_BUCH")
    return Book(find_root(Path(env) if env else None))


def _call(fn, *args, **kwargs) -> Any:
    try:
        return fn(_book(), *args, **kwargs)
    except (BookError, FormatError, ValueError) as exc:
        return {"ok": False, "fehler": str(exc)}


def _direct(fn, *args, **kwargs) -> Any:
    """Free journal postings: refused in proposal mode."""
    try:
        book = _book()
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    if (book.settings.get("agent_modus") or "vorschlag") != "direkt":
        return {"ok": False, "fehler": "agent_modus ist 'vorschlag': bitte propose_booking verwenden. "
                "Ein Mensch gibt mit `batzen approve` frei (oder setzt agent_modus: direkt in batzen.yaml)."}
    try:
        return fn(book, *args, **kwargs)
    except (BookError, FormatError, ValueError) as exc:
        return {"ok": False, "fehler": str(exc)}


# ---------- read ----------

@server.tool()
def status() -> dict:
    """Überblick: Firma, Jahre, flüssige Mittel, laufendes Ergebnis, offene Rechnungen, Vorschläge, Inbox, Prüfstatus."""
    return _call(api.status)


@server.tool()
def check() -> dict:
    """Prüft das ganze Buch (Journal, Rechnungen, Löhne, Sperre). Fehler blockieren Commits."""
    return _call(api.run_check)


@server.tool()
def accounts(suche: str = "") -> list | dict:
    """Kontenplan, optional gefiltert nach Nummer oder Name (z.B. 'bank', 'miete', '65')."""
    return _call(api.accounts, suche)


@server.tool()
def balance(jahr: int | None = None, periode: str = "jahr") -> dict:
    """Saldenliste. periode: jahr, q1–q4, h1, h2, 01–12 oder JJJJ-MM-TT..JJJJ-MM-TT."""
    return _call(api.balances, jahr, periode)


@server.tool()
def ledger(konto: str, jahr: int | None = None) -> dict:
    """Kontoblatt eines Kontos mit Gegenkonto und laufendem Saldo."""
    return _call(api.ledger, konto, jahr)


@server.tool()
def journal(jahr: int | None = None, monat: int | None = None, beleg: str = "", konto: str = "",
            suche: str = "", limit: int = 100) -> list | dict:
    """Journalzeilen, gefiltert. Neueste zuletzt."""
    return _call(api.journal_rows, jahr, monat, beleg, konto, suche, limit)


@server.tool()
def report(jahr: int | None = None, pdf: bool = False) -> dict:
    """Jahresrechnung: Bilanz, Erfolgsrechnung (OR 959a/b), Gewinnverwendung; optional als PDF."""
    return _call(api.statement, jahr, None, pdf)


@server.tool()
def history(limit: int = 20) -> list | dict:
    """Änderungsverlauf (git log) des Buchs."""
    return _call(api.history, limit)


# ---------- journal ----------

@server.tool()
def propose_booking(datum: str, soll: str, haben: str, betrag: str, text: str, begruendung: str,
                    beleg_datei: str = "") -> dict:
    """Buchung vorschlagen (wird geprüft, aber erst nach Freigabe durch einen Menschen gebucht).
    begruendung: kurz, warum diese Konten (wird dem Menschen gezeigt).
    beleg_datei: Quittung, z.B. inbox/quittung.pdf — wird bei der Freigabe automatisch nach belege/ abgelegt."""
    return _call(api.propose, datum, soll, haben, betrag, text, begruendung, beleg_datei)


@server.tool()
def list_proposals() -> list | dict:
    """Offene Buchungsvorschläge."""
    return _call(api.proposals)


@server.tool()
def book_entry(datum: str, soll: str, haben: str, betrag: str, text: str, beleg_datei: str = "") -> dict:
    """Buchung direkt erfassen (nur agent_modus: direkt). beleg_datei: Pfad zur Quittung (z.B. inbox/x.pdf)."""
    return _direct(api.post_entry, datum, soll, haben, betrag, text, "", beleg_datei or None)


@server.tool()
def book_split(datum: str, text: str, zeilen: list[dict], beleg_datei: str = "") -> dict:
    """Sammelbuchung (nur agent_modus: direkt): zeilen = [{"soll": "6500", "betrag": "40.00"}, {"haben": "1020", "betrag": "40.00"}]."""
    return _direct(api.post_split, datum, text, zeilen, "", beleg_datei or None)


@server.tool()
def approve_proposals(ids: list[str]) -> dict:
    """Vorschläge buchen (nur agent_modus: direkt; sonst gibt ein Mensch frei)."""
    return _direct(api.approve, ids)


@server.tool()
def reverse_entry(beleg: str, datum: str = "", text: str = "") -> dict:
    """Storno eines manuellen Belegs per Gegenbuchung (nur agent_modus: direkt)."""
    return _direct(api.reverse_entry, beleg, datum or None, text)


# ---------- debitoren ----------

@server.tool()
def customers() -> list | dict:
    """Kundenliste."""
    return _call(api.customer_list)


@server.tool()
def add_customer(name: str, firma: str = "", strasse: str = "", nr: str = "", plz: str = "", ort: str = "",
                 land: str = "CH", email: str = "", rechnung_an: str = "firma") -> dict:
    """Kunde anlegen. rechnung_an: 'firma' oder 'person'."""
    return _call(api.customer_add, name=name, firma=firma, strasse=strasse, nr=nr, plz=plz, ort=ort,
                 land=land, email=email, rechnung_an=rechnung_an)


@server.tool()
def invoices(status: str = "") -> list | dict:
    """Rechnungen mit offenem Betrag. status: offen, teilbezahlt, bezahlt, storniert."""
    return _call(api.invoice_list, status)


@server.tool()
def create_invoice(kunde: str, positionen: list[dict], datum: str = "", text: str = "") -> dict:
    """QR-Rechnung ausstellen und verbuchen. positionen = [{"text": "Beratung", "menge": 10,
    "einheit": "h", "preis": "150.00", "konto": "3400"}] (konto optional). Erzeugt PDF mit QR-Einzahlungsschein."""
    return _call(api.invoice_create, kunde, positionen, datum or None, text)


@server.tool()
def match_payment(betrag: str, text: str = "") -> dict:
    """Findet die offene Rechnung zu einem Zahlungseingang (Referenz, Rechnungsnummer, Betrag)."""
    return _call(api.invoice_match, betrag, text)


@server.tool()
def pay_invoice(nummer: str, betrag: str = "", datum: str = "", konto: str = "") -> dict:
    """Zahlungseingang auf Rechnung buchen (Bank an Debitoren). Ohne betrag: ganzer offener Betrag."""
    return _call(api.invoice_pay, nummer, betrag or None, datum or None, konto or None)


@server.tool()
def credit_invoice(nummer: str, betrag: str = "", datum: str = "", grund: str = "") -> dict:
    """Gutschrift/Erlösminderung auf Rechnung (z.B. Skonto, Kulanz)."""
    return _call(api.invoice_credit, nummer, betrag or None, datum or None, None, grund)


@server.tool()
def void_invoice(nummer: str, grund: str) -> dict:
    """Unbezahlte Rechnung stornieren (Buchung wird entfernt, Rechnung bleibt als 'storniert')."""
    return _call(api.invoice_void, nummer, grund)


@server.tool()
def receivables(stichtag: str = "") -> dict:
    """Offene Debitoren per Stichtag mit Altersstruktur und Abgleich zum Debitorenkonto."""
    return _call(api.receivables, stichtag or None)


# ---------- lohn ----------

@server.tool()
def employees() -> list | dict:
    """Mitarbeitende."""
    return _call(api.employee_list)


@server.tool()
def payroll_run(monat: str, mitarbeiter: str = "", eingaben: dict | None = None) -> dict:
    """Lohnabrechnungen eines Monats (JJJJ-MM) als Entwurf berechnen. eingaben (nur mit mitarbeiter):
    stunden, bvg, kinderzulagen, korrektur, korrektur_text, qst_satzbestimmend, qst_gesamtpensum."""
    return _call(api.payroll_run, monat, mitarbeiter or None, eingaben)


@server.tool()
def payslip(monat: str, mitarbeiter: str) -> dict:
    """Eine Lohnabrechnung anzeigen."""
    return _call(api.payslip_show, monat, mitarbeiter)


@server.tool()
def close_payslip(monat: str, mitarbeiter: str) -> dict:
    """Lohnabrechnung abschliessen: friert Werte ein, verbucht sie, erzeugt PDF."""
    return _call(api.payslip_close, monat, mitarbeiter)


@server.tool()
def lohnausweis(jahr: int, mitarbeiter: str) -> dict:
    """Lohnausweis (Formular 11) aus den abgeschlossenen Abrechnungen des Jahres."""
    return _call(api.lohnausweis_create, jahr, mitarbeiter)


def serve() -> None:
    server.run("stdio")

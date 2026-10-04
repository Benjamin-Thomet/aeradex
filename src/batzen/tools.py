"""Agent tools — one registry for the MCP server and the UI's chat panel, so
both always offer the same operations with the same rules.

Every tool opens the book fresh (edits made in between are always seen) and
returns plain data. In `agent_modus: vorschlag` (the default) free journal
postings are refused: the agent proposes, a person approves.
"""
from __future__ import annotations

import base64
import contextvars
import os
from pathlib import Path
from typing import Any

from . import api
from .book import Book, BookError, find_root
from .files import FormatError

# The book a tool call works on: set by the chat panel; the MCP server falls
# back to $BATZEN_BUCH or the working directory.
BOOK_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar("batzen_book_root", default=None)

INSTRUCTIONS = """batzen ist eine Schweizer Buchhaltung (OR/KMU-Kontenrahmen, CHF) als Textdateien in git.
Regeln:
- Rechne nie selbst Summen, Salden, Löhne oder MWST — frage die Tools (balance, ledger, report, payroll_run).
- Vor dem Buchen: accounts() nach dem passenden Konto durchsuchen und journal() auf Duplikate prüfen.
  Soll = wohin der Wert fliesst (Aufwand, Aktiven nehmen zu), Haben = woher (Ertrag, Bank bei Zahlung, Passiven nehmen zu).
- Im agent_modus 'vorschlag' nur propose_booking verwenden (mit beleg_datei, wenn eine Quittung vorliegt); ein Mensch gibt frei.
- Gebuchtes wird nie gelöscht: Korrekturen per reverse_entry (Storno) bzw. Rechnungen per void/credit.
- Nach jeder Änderung ist das Buch geprüft und in git committet; bei Fehlern die Meldung lesen und korrigieren.
- Beträge als Text mit Punkt: "1234.50". Datum als JJJJ-MM-TT.
- MWST: status() nennt die mwst_methode. Bei "effektiv" und einem Beleg mit ausgewiesener MWST den Code mitgeben
  (V81/I81 Vorsteuer, U81 Umsatz …) und den BRUTTO-Betrag buchen; die Steuer wird automatisch abgespalten.
  Bei "saldo" oder "keine" ohne Vorsteuer-Code buchen.
"""


def book() -> Book:
    root = BOOK_ROOT.get()
    if root is None:
        env = os.environ.get("BATZEN_BUCH")
        root = find_root(Path(env) if env else None)
    return Book(root)


def _call(fn, *args, **kwargs) -> Any:
    try:
        return fn(book(), *args, **kwargs)
    except (BookError, FormatError, ValueError, KeyError) as exc:
        return {"ok": False, "fehler": str(exc)}


def _direct(fn, *args, **kwargs) -> Any:
    """Free journal postings: refused in proposal mode."""
    try:
        b = book()
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    if (b.settings.get("agent_modus") or "vorschlag") != "direkt":
        return {"ok": False, "fehler": "agent_modus ist 'vorschlag': bitte propose_booking verwenden. "
                "Ein Mensch gibt in batzen (Prüfen) oder mit `batzen approve` frei."}
    try:
        return fn(b, *args, **kwargs)
    except (BookError, FormatError, ValueError, KeyError) as exc:
        return {"ok": False, "fehler": str(exc)}


# ---------- read ----------

def status() -> dict:
    """Überblick: Firma, Jahre, flüssige Mittel, laufendes Ergebnis, offene Rechnungen, Vorschläge, Inbox, Prüfstatus."""
    return _call(api.status)


def check() -> dict:
    """Prüft das ganze Buch (Journal, Rechnungen, Löhne, Sperre). Fehler blockieren Commits."""
    return _call(api.run_check)


def accounts(suche: str = "") -> list | dict:
    """Kontenplan, optional gefiltert nach Nummer oder Name (z.B. 'bank', 'miete', '65').

    Args:
        suche: Teil der Kontonummer oder des Kontonamens; leer = alle Konten.
    """
    return _call(api.accounts, suche)


def balance(jahr: int | None = None, periode: str = "jahr") -> dict:
    """Saldenliste.

    Args:
        jahr: Geschäftsjahr; leer = aktuelles.
        periode: jahr, q1–q4, h1, h2, 01–12 oder JJJJ-MM-TT..JJJJ-MM-TT.
    """
    return _call(api.balances, jahr, periode)


def ledger(konto: str, jahr: int | None = None) -> dict:
    """Kontoblatt eines Kontos mit Gegenkonto und laufendem Saldo.

    Args:
        konto: Kontonummer, z.B. "1020".
        jahr: Geschäftsjahr; leer = aktuelles.
    """
    return _call(api.ledger, konto, jahr)


def journal(jahr: int | None = None, monat: int | None = None, beleg: str = "", konto: str = "",
            suche: str = "", limit: int = 100) -> list | dict:
    """Journalzeilen, gefiltert. Neueste zuletzt.

    Args:
        jahr: Geschäftsjahr.
        monat: 1–12.
        beleg: genaue Belegnummer.
        konto: Zeilen, die dieses Konto im Soll oder Haben haben.
        suche: Text, der im Buchungstext vorkommt.
        limit: höchstens so viele Zeilen.
    """
    return _call(api.journal_rows, jahr, monat, beleg, konto, suche, limit)


def report(jahr: int | None = None) -> dict:
    """Jahresrechnung: Bilanz, Erfolgsrechnung (OR 959a/b), Gewinnverwendung.

    Args:
        jahr: Geschäftsjahr; leer = aktuelles.
    """
    return _call(api.statement, jahr)


def history(limit: int = 20) -> list | dict:
    """Änderungsverlauf (git log) des Buchs.

    Args:
        limit: Anzahl Einträge.
    """
    return _call(api.history, limit)


def list_inbox() -> list | dict:
    """Dateien in inbox/, die noch nicht verbucht sind (Quittungen, Rechnungen, Auszüge)."""
    try:
        b = book()
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    folder = b.root / "inbox"
    return [{"datei": f"inbox/{p.name}", "groesse_kb": round(p.stat().st_size / 1024, 1)}
            for p in sorted(folder.iterdir()) if p.is_file() and not p.name.startswith(".")] if folder.exists() else []


# ---------- journal ----------

def propose_booking(datum: str, soll: str, haben: str, betrag: str, text: str, begruendung: str,
                    beleg_datei: str = "", mwst: str = "") -> dict:
    """Buchung vorschlagen (wird geprüft, aber erst nach Freigabe durch einen Menschen gebucht).

    Args:
        datum: JJJJ-MM-TT.
        soll: Soll-Konto (wohin der Wert fliesst), z.B. "6500".
        haben: Haben-Konto (woher), z.B. "1020".
        betrag: positiver Betrag als Text, z.B. "45.80".
        text: Buchungstext, z.B. "Druckerpapier, Papeterie Muster".
        begruendung: kurz, warum diese Konten (wird dem Menschen gezeigt).
        beleg_datei: Quittung, z.B. "inbox/quittung.pdf" — wird bei der Freigabe nach belege/ abgelegt.
        mwst: MWST-Code, wenn das Buch MWST-pflichtig ist und der Beleg MWST ausweist: V81/V26/V38 (Vorsteuer
            Material/Dienstleistungen), I81/I26/I38 (Vorsteuer Investitionen/übriger Aufwand), U81/U26/U38 (Umsatz).
            Dann ist betrag BRUTTO (inkl. MWST); die Steuer wird automatisch abgespalten.
    """
    return _call(api.propose, datum, soll, haben, betrag, text, begruendung, beleg_datei, mwst)


def list_proposals() -> list | dict:
    """Offene Buchungsvorschläge."""
    return _call(api.proposals)


def book_entry(datum: str, soll: str, haben: str, betrag: str, text: str, beleg_datei: str = "",
               mwst: str = "") -> dict:
    """Buchung direkt erfassen (nur agent_modus: direkt).

    Args:
        datum: JJJJ-MM-TT.
        soll: Soll-Konto.
        haben: Haben-Konto.
        betrag: positiver Betrag als Text.
        text: Buchungstext.
        beleg_datei: Pfad zur Quittung, z.B. "inbox/x.pdf".
        mwst: MWST-Code (siehe propose_booking); betrag dann brutto.
    """
    return _direct(api.post_entry, datum, soll, haben, betrag, text, "", beleg_datei or None, mwst)


def book_split(datum: str, text: str, zeilen: list[dict], beleg_datei: str = "") -> dict:
    """Sammelbuchung (nur agent_modus: direkt).

    Args:
        datum: JJJJ-MM-TT.
        text: Buchungstext.
        zeilen: z.B. [{"soll": "6500", "betrag": "40.00"}, {"haben": "1020", "betrag": "40.00"}]; der Beleg muss aufgehen.
        beleg_datei: Pfad zur Quittung.
    """
    return _direct(api.post_split, datum, text, zeilen, "", beleg_datei or None)


def approve_proposals(ids: list[str]) -> dict:
    """Vorschläge buchen (nur agent_modus: direkt; sonst gibt ein Mensch frei).

    Args:
        ids: z.B. ["V-001"].
    """
    return _direct(api.approve, ids)


def reverse_entry(beleg: str, datum: str = "", text: str = "") -> dict:
    """Storno eines manuellen Belegs per Gegenbuchung (nur agent_modus: direkt).

    Args:
        beleg: Belegnummer, z.B. "26-004".
        datum: Datum der Gegenbuchung; leer = heute.
        text: optionaler Text.
    """
    return _direct(api.reverse_entry, beleg, datum or None, text)


def mwst_report(periode: str) -> dict:
    """MWST-Abrechnung einer Periode nach ESTV-Ziffern (Umsatz, Steuer, Vorsteuer, Zahllast).

    Args:
        periode: z.B. "2026-Q1" (effektive Methode) oder "2026-S1" (Saldosteuersatz).
    """
    return _call(api.mwst_report, periode)


# ---------- debitoren ----------

def customers() -> list | dict:
    """Kundenliste."""
    return _call(api.customer_list)


def add_customer(name: str, firma: str = "", strasse: str = "", nr: str = "", plz: str = "", ort: str = "",
                 land: str = "CH", email: str = "", rechnung_an: str = "firma") -> dict:
    """Kunde anlegen.

    Args:
        name: Kontaktperson oder Privatperson.
        firma: Firmenname (leer bei Privatpersonen).
        strasse: Strasse.
        nr: Hausnummer.
        plz: Postleitzahl.
        ort: Ort.
        land: Ländercode, Standard CH.
        email: E-Mail.
        rechnung_an: "firma" oder "person".
    """
    return _call(api.customer_add, name=name, firma=firma, strasse=strasse, nr=nr, plz=plz, ort=ort,
                 land=land, email=email, rechnung_an=rechnung_an)


def invoices(status: str = "") -> list | dict:
    """Rechnungen mit offenem Betrag.

    Args:
        status: offen, teilbezahlt, bezahlt, storniert; leer = alle.
    """
    return _call(api.invoice_list, status)


def create_invoice(kunde: str, positionen: list[dict], datum: str = "", text: str = "") -> dict:
    """QR-Rechnung ausstellen und verbuchen; erzeugt die PDF mit QR-Einzahlungsschein.

    Args:
        kunde: Kundennummer, z.B. "K0001".
        positionen: z.B. [{"text": "Beratung", "menge": 10, "einheit": "h", "preis": "150.00", "konto": "3400"}] (konto optional).
        datum: Rechnungsdatum JJJJ-MM-TT; leer = heute.
        text: Einleitungstext auf der Rechnung.
    """
    return _call(api.invoice_create, kunde, positionen, datum or None, text)


def match_payment(betrag: str, text: str = "") -> dict:
    """Findet die offene Rechnung zu einem Zahlungseingang (Referenz, Rechnungsnummer, Betrag).

    Args:
        betrag: Betrag des Zahlungseingangs.
        text: Zeile aus dem Bankauszug (Referenz, Mitteilung).
    """
    return _call(api.invoice_match, betrag, text)


def pay_invoice(nummer: str, betrag: str = "", datum: str = "", konto: str = "") -> dict:
    """Zahlungseingang auf Rechnung buchen (Bank an Debitoren).

    Args:
        nummer: Rechnungsnummer, z.B. "R-2026-0001".
        betrag: leer = ganzer offener Betrag.
        datum: Valuta JJJJ-MM-TT; leer = heute.
        konto: Geldkonto; leer = Bank aus den Einstellungen.
    """
    return _call(api.invoice_pay, nummer, betrag or None, datum or None, konto or None)


def credit_invoice(nummer: str, betrag: str = "", datum: str = "", grund: str = "") -> dict:
    """Gutschrift/Erlösminderung auf Rechnung (z.B. Skonto, Kulanz).

    Args:
        nummer: Rechnungsnummer.
        betrag: leer = ganzer offener Betrag.
        datum: JJJJ-MM-TT; leer = heute.
        grund: kurzer Grund.
    """
    return _call(api.invoice_credit, nummer, betrag or None, datum or None, None, grund)


def void_invoice(nummer: str, grund: str) -> dict:
    """Unbezahlte Rechnung stornieren (Buchung wird entfernt, Rechnung bleibt als 'storniert').

    Args:
        nummer: Rechnungsnummer.
        grund: Grund des Stornos.
    """
    return _call(api.invoice_void, nummer, grund)


def receivables(stichtag: str = "") -> dict:
    """Offene Debitoren per Stichtag mit Altersstruktur und Abgleich zum Debitorenkonto.

    Args:
        stichtag: JJJJ-MM-TT; leer = heute.
    """
    return _call(api.receivables, stichtag or None)


# ---------- lohn ----------

def employees() -> list | dict:
    """Mitarbeitende."""
    return _call(api.employee_list)


def payroll_run(monat: str, mitarbeiter: str = "", eingaben: dict | None = None) -> dict:
    """Lohnabrechnungen eines Monats als Entwurf berechnen.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001"; leer = alle aktiven.
        eingaben: nur mit mitarbeiter: stunden, bvg, kinderzulagen, korrektur, korrektur_text, qst_satzbestimmend, qst_gesamtpensum.
    """
    return _call(api.payroll_run, monat, mitarbeiter or None, eingaben)


def payslip(monat: str, mitarbeiter: str) -> dict:
    """Eine Lohnabrechnung anzeigen.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.payslip_show, monat, mitarbeiter)


def close_payslip(monat: str, mitarbeiter: str) -> dict:
    """Lohnabrechnung abschliessen: friert Werte ein, verbucht sie, erzeugt PDF.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.payslip_close, monat, mitarbeiter)


def lohnausweis(jahr: int, mitarbeiter: str) -> dict:
    """Lohnausweis (Formular 11) aus den abgeschlossenen Abrechnungen des Jahres.

    Args:
        jahr: Kalenderjahr.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.lohnausweis_create, jahr, mitarbeiter)


# ---------- chat only: let Claude look at a receipt ----------

def read_inbox_file(datei: str) -> list[dict] | str:
    """Liest eine Datei aus inbox/ oder belege/ (PDF, Bild oder Text), damit du den Inhalt siehst.

    Args:
        datei: Pfad relativ zum Buch, z.B. "inbox/quittung.pdf".
    """
    try:
        b = book()
    except BookError as exc:
        return f"Fehler: {exc}"
    path = (b.root / datei).resolve()
    if not path.is_relative_to(b.root) or path.parts[len(b.root.parts)] not in ("inbox", "belege") or not path.is_file():
        return f"Fehler: {datei} ist keine Datei in inbox/ oder belege/"
    data = path.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        return f"Fehler: {datei} ist grösser als 20 MB"
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                 "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": f"Inhalt von {datei}"}]
    images = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    if suffix in images:
        return [{"type": "image", "source": {"type": "base64", "media_type": images[suffix],
                                              "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": f"Inhalt von {datei}"}]
    return data.decode("utf-8", errors="replace")[:20000]


# Order matters for prompt caching: a stable list keeps the cached prefix valid.
SHARED = [status, check, accounts, balance, ledger, journal, report, history, list_inbox, mwst_report,
          propose_booking, list_proposals, book_entry, book_split, approve_proposals, reverse_entry,
          customers, add_customer, invoices, create_invoice, match_payment, pay_invoice, credit_invoice,
          void_invoice, receivables, employees, payroll_run, payslip, close_payslip, lohnausweis]
CHAT_ONLY = [read_inbox_file]

"""Agent tools — one registry for the MCP server and the UI's chat panel, so
both always offer the same operations with the same rules.

Every tool opens the book fresh (edits made in between are always seen) and
returns plain data. In `agent_modus: vorschlag` (the default) free journal
postings are refused: the agent proposes, a person approves.
"""
from __future__ import annotations

import base64
import contextvars
from decimal import Decimal
import os
from pathlib import Path
from typing import Any

from . import api
from .book import Book, BookError, find_root
from .files import FormatError

# The book a tool call works on: set by the chat panel; the MCP server falls
# back to $AERADEX_BUCH or the working directory.
BOOK_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar("aeradex_book_root", default=None)

INSTRUCTIONS = """aeradex ist eine Schweizer Buchhaltung (OR/KMU-Kontenrahmen, CHF) als Textdateien in git.
Regeln:
- Rechne nie selbst Summen, Salden, Löhne oder MWST — frage die Tools (balance, ledger, report, payroll_run).
- Vor dem Buchen: accounts() nach dem passenden Konto durchsuchen und journal() auf Duplikate prüfen.
  Soll = wohin der Wert fliesst (Aufwand, Aktiven nehmen zu), Haben = woher (Ertrag, Bank bei Zahlung, Passiven nehmen zu).
- Im agent_modus 'vorschlag' nur propose_booking verwenden (mit beleg_datei, wenn eine Quittung vorliegt); ein Mensch gibt frei.
- Gebuchtes wird nie gelöscht: Korrekturen per reverse_entry (Storno) bzw. Rechnungen per void/credit.
- Nach jeder Änderung ist das Buch geprüft und in git committet; bei Fehlern die Meldung lesen und korrigieren.
- status() liefert revision. Bei Schreibtools mit expected_revision diesen Stand mitgeben; nach Konflikten neu lesen.
- Bei Schreibtools mit idempotency_key einen eindeutigen Schlüssel verwenden und bei Timeout/Wiederholung beibehalten.
- Kunden: vor add_customer mit customers() prüfen, ob es ihn schon gibt; fehlende oder falsche Angaben eines
  bestehenden Kunden mit update_customer ergänzen, nie einen zweiten anlegen. Was der Mensch über den Kunden sagt
  und kein eigenes Feld hat (Zugang/Schlüssel, Wünsche, wie erreichbar …), nicht weglassen, sondern in notizen bzw.
  notiz festhalten (dauerhafte Fakten); wer ihn empfohlen hat → herkunft. Was mit dem Kunden besprochen oder
  vereinbart wurde (Anruf, Besuch, E-Mail), mit Datum → log_customer, mit nachfassen wenn etwas nachzufragen ist.
- Rechnungen: Rabatt nur über rabatt_prozent. Hat der Kunde offene Akontorechnungen (open_akonto) und ist
  nicht klar, ob das die Schlussrechnung dazu ist, zuerst fragen; create_invoice zieht sonst alle ab (akonto="alle").
  Eine Anzahlung verlangen = create_invoice mit art="akonto".
- Beträge als Text mit Punkt: "1234.50". Datum als JJJJ-MM-TT.
- MWST: status() nennt die mwst_methode. Bei "effektiv" und einem Beleg mit ausgewiesener MWST den Code mitgeben
  (V81/I81 Vorsteuer, U81 Umsatz …) und den BRUTTO-Betrag buchen; die Steuer wird automatisch abgespalten.
  Bei "saldo" oder "keine" ohne Vorsteuer-Code buchen.
- Bankbewegungen: bank_transactions() zeigt offene Posten aus importierten Kontoauszügen. Passt eine zu einer
  offenen Rechnung/einem Kreditor: assign_bank_transaction. Sonst propose_bank_booking mit Gegenkonto.
- Lieferantenrechnungen (PDF/Foto mit QR-Zahlteil): scan_qr_bill → bekannter Lieferant: add_supplier_bill
  (Konto leer = hinterlegtes Konto). Neuer Lieferant: add_supplier ohne konto, dann dem Menschen das Aufwandkonto
  vorschlagen; er erfasst die Rechnung unter Kreditoren (vorausgefüllt). Nicht als freie Buchung (propose_booking)
  erfassen — sonst fehlen IBAN und Referenz für den Zahlungslauf.
- Belegeingang (bill_drafts): hochgeladene Belege — Lieferantenrechnungen, Quittungen, extern erstellte eigene
  Rechnungen —, schon ausgelesen. Ist ein Entwurf «unsicher» oder «agent», kontiere ihn: bill_draft(id) lesen,
  Konto wählen, complete_bill_draft mit Begründung. Neue Belege in der Inbox: create_bill_draft(datei) — nicht
  propose_booking: so werden Quittungen mit der Bank abgeglichen und nichts doppelt gebucht. Gebucht wird von
  einem Menschen.
- Kontoauszüge, die nicht camt.053 sind: CSV/Excel → bank_file_preview, dann propose_bank_format (nur das
  Format beschreiben, ein Mensch bestätigt). Kreditkartenabrechnung (PDF) → card_statement_text, dann
  propose_card_statement mit allen Transaktionen; Beträge nur abschreiben, nie umrechnen oder ergänzen.
- Mitarbeitende: vor add_employee mit employees() prüfen. Aus einem Arbeitsvertrag alles übernehmen, was er
  hergibt (Adresse, Geburtsdatum, AHV-Nr., Eintritt, Lohnart, Lohn, Pensum, Wochenstunden, Ferienentschädigung,
  BVG, Aufenthalt, IBAN); was kein Feld hat (Funktion, Probezeit, Kündigungsfrist, Ferienanspruch, 13. Monatslohn,
  Spesenregelung) in notizen. Den Vertrag mit file_employee_document bei der Person ablegen. Fehlt etwas Nötiges
  (Geschlecht, AHV-Nr., Aufenthalt, Quellensteuer-Tarif), nicht raten, sondern am Schluss danach fragen.
- Fremdwährung: Beleg in EUR/USD … → waehrung mitgeben und den Betrag in der Fremdwährung; umgerechnet wird
  zum BAZG-Tageskurs (exchange_rate). Konten mit Fremdwährung (accounts() zeigt waehrung) gehen nur so.
  Die Bewertung per Stichtag (revaluation_preview) bucht ein Mensch im Abschluss.
"""


def book() -> Book:
    root = BOOK_ROOT.get()
    if root is None:
        env = os.environ.get("AERADEX_BUCH")
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
                "Ein Mensch gibt in aeradex (Prüfen) oder mit `aeradex approve` frei."}
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


def _in_book(result, key: str):
    """A file the tool wrote into the book, named relative to it: the agent may run elsewhere (MCP over ssh) and
    the web UI serves book files under /datei/<path>."""
    if isinstance(result, dict) and isinstance(result.get(key), str):
        try:
            result[key] = book().rel(Path(result[key]))
        except (BookError, ValueError):
            pass
    return result


def closing_documents(jahr: int | None = None, teile: list[str] | None = None,
                      formate: list[str] | None = None) -> dict:
    """Abschlussunterlagen eines Jahres als ZIP in berichte/: Jahresrechnung, Saldenliste, Journal,
    Kontoblätter, Belegordner (Belege mit ihrer Belegnummer gestempelt, Lückenverzeichnis), MWST,
    offene Posten, Lohn, Kontenplan — PDF und CSV, mit Prüfsummen (manifest.json).

    Args:
        jahr: Geschäftsjahr; leer = aktuelles.
        teile: Auswahl, z.B. ["journal", "belege"]; leer = alle, die für das Buch zutreffen.
        formate: z.B. ["pdf"]; leer = alle (pdf, csv, Originalbelege).
    """
    return _in_book(_call(api.dossier, jahr, teile, formate), "datei")


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
                    beleg_datei: str = "", mwst: str = "", waehrung: str = "",
                    expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
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
        waehrung: nur bei Fremdwährung (Beleg in EUR/USD … oder Konto mit Fremdwährung, siehe list_accounts):
            ISO-Code; betrag ist dann in dieser Währung, umgerechnet wird zum BAZG-Tageskurs des Datums.
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _call(api.propose, datum, soll, haben, betrag, text, begruendung, beleg_datei, mwst, waehrung=waehrung,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


def list_proposals() -> list | dict:
    """Offene Buchungsvorschläge."""
    return _call(api.proposals)


def book_entry(datum: str, soll: str, haben: str, betrag: str, text: str, beleg_datei: str = "",
               mwst: str = "", waehrung: str = "",
               expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Buchung direkt erfassen (nur agent_modus: direkt).

    Args:
        datum: JJJJ-MM-TT.
        soll: Soll-Konto.
        haben: Haben-Konto.
        betrag: positiver Betrag als Text.
        text: Buchungstext.
        beleg_datei: Pfad zur Quittung, z.B. "inbox/x.pdf".
        mwst: MWST-Code (siehe propose_booking); betrag dann brutto.
        waehrung: Fremdwährung (siehe propose_booking).
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _direct(api.post_entry, datum, soll, haben, betrag, text, "", beleg_datei or None, mwst, waehrung,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


def exchange_rate(waehrung: str, datum: str = "") -> dict:
    """Offizieller BAZG-Tageskurs (CHF je 1 Einheit), den auch die ESTV verwendet.

    Args:
        waehrung: ISO-Code, z.B. "EUR".
        datum: JJJJ-MM-TT (leer = heute; Wochenende = letzter publizierter Tag).
    """
    return _call(api.fx_rate, waehrung, datum or None)


def revaluation_preview(stichtag: str) -> dict:
    """Fremdwährungskonten per Stichtag zum BAZG-Kurs bewerten — nur Vorschau, bucht nichts.
    Gebucht wird die Bewertung von einem Menschen (Abschluss → Fremdwährungen).

    Args:
        stichtag: JJJJ-MM-TT, meist der 31.12.
    """
    return _call(api.fx_preview, stichtag)


def book_split(datum: str, text: str, zeilen: list[dict], beleg_datei: str = "",
               expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Sammelbuchung (nur agent_modus: direkt).

    Args:
        datum: JJJJ-MM-TT.
        text: Buchungstext.
        zeilen: z.B. [{"soll": "6500", "betrag": "40.00"}, {"haben": "1020", "betrag": "40.00"}]; der Beleg muss aufgehen.
        beleg_datei: Pfad zur Quittung.
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _direct(api.post_split, datum, text, zeilen, "", beleg_datei or None,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


def approve_proposals(ids: list[str],
                      expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Vorschläge buchen (nur agent_modus: direkt; sonst gibt ein Mensch frei).

    Args:
        ids: z.B. ["V-001"].
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _direct(api.approve, ids,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


def reverse_entry(beleg: str, datum: str = "", text: str = "",
                  expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Storno eines manuellen Belegs per Gegenbuchung (nur agent_modus: direkt).

    Args:
        beleg: Belegnummer, z.B. "26-004".
        datum: Datum der Gegenbuchung; leer = heute.
        text: optionaler Text.
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _direct(api.reverse_entry, beleg, datum or None, text,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


def recode_preview(belege: list[str], konto_alt: str = "", konto_neu: str = "", mwst_neu: str = "") -> dict:
    """Vorschau einer Umbuchung (ändert nichts): pro Beleg vorher/nachher oder warum er übersprungen wird.

    Args:
        belege: Belegnummern, z.B. ["26-004", "E-2026-0003"] (Belege aus journal()).
        konto_alt: das zu ersetzende Konto (egal ob im Soll oder Haben), z.B. "6500".
        konto_neu: das neue Konto, z.B. "6570"; leer = Konto bleibt.
        mwst_neu: neuer MWST-Code (z.B. "V81"), "-" = Code entfernen, leer = Code bleibt.
    """
    return _call(api.recode_preview, belege, konto_alt, konto_neu, mwst_neu)


def recode(belege: list[str], konto_alt: str = "", konto_neu: str = "", mwst_neu: str = "",
           expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Mehrere Belege umbuchen: Konto ersetzen (Soll oder Haben) und/oder MWST-Code ändern; Netto und
    Steuer werden aus dem Brutto neu gerechnet. Manuelle Belege und Kreditorenrechnungen; anderes wird mit
    Grund übersprungen. Zuerst recode_preview zeigen (nur agent_modus: direkt).

    Args:
        belege: Belegnummern.
        konto_alt: das zu ersetzende Konto.
        konto_neu: das neue Konto; leer = Konto bleibt.
        mwst_neu: neuer MWST-Code, "-" = Code entfernen, leer = Code bleibt.
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _direct(api.recode, belege, konto_alt, konto_neu, mwst_neu,
                   expected_revision=expected_revision, idempotency_key=idempotency_key)


def accrual_proposals(jahr: int) -> dict:
    """Rechnungsabgrenzungen für den Jahresabschluss: Buchungen mit « · Leistung von–bis» im Text, deren Leistung
    ins Folgejahr reicht oder teils im Jahr lag, mit dem Anteil nach Tagen und der vorgeschlagenen Buchung. Ändert nichts.

    Args:
        jahr: Geschäftsjahr, z.B. 2026.
    """
    return _call(api.accruals, jahr)


def book_accruals(jahr: int, ids: list[str],
                  expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Ausgewählte Abgrenzungen per 31.12. buchen, mit Auflösung am 01.01. des Folgejahres (nur agent_modus: direkt).

    Args:
        jahr: Geschäftsjahr.
        ids: IDs aus accrual_proposals().
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _direct(api.accruals_book, jahr, ids, expected_revision=expected_revision, idempotency_key=idempotency_key)


def closing_checklist(jahr: int) -> dict:
    """Checkliste des Jahresabschlusses: Schritte in Arbeitsreihenfolge (Laufendes, Bewerten, Abschlussbuchungen,
    Kontrolle, Abschliessen) mit Status, Link und wer was abgehakt hat. Ändert nichts — abhaken und buchen tut ein
    Mensch auf der Seite Abschluss.

    Args:
        jahr: Geschäftsjahr, z.B. 2026.
    """
    return _call(api.closing_checklist, jahr)


def booking_history(beleg: str) -> dict:
    """Wer einen Beleg gebucht hat: jede Änderung an seinen Journalzeilen (erfasst, geändert, entfernt) mit Person
    oder Agent, Zeit und Commit — aus dem Verlauf (git). Ändert nichts.

    Args:
        beleg: Belegnummer, z.B. "27-003".
    """
    return _call(api.booking_history, beleg)


def business_years() -> dict:
    """Die Geschäftsjahre: erstes Jahr, welche Jahre Buchungen annehmen (gebucht wird nur in eröffneten, nicht
    gesperrten Jahren — ein Datum ausserhalb wird abgelehnt), welche gesperrt sind und ob das nächste eröffnet werden
    kann. Ändert nichts; eröffnen tut ein Mensch unter Einstellungen › Geschäftsjahre."""
    return _call(api.years_overview)


def opening_balance() -> dict:
    """Die Eröffnungsbilanz des ersten Geschäftsjahres: je Konto die Schlusszahl des Vorjahres aus der bisherigen
    Buchhaltung (Lesart: Passiven und Ertrag positiv), die daraus abgeleitete Eröffnung (Privat und Gewinn im
    Eigenkapital), Summen und Differenz, dazu ein eingelesener, noch nicht gespeicherter Vorschlag. Ändert nichts;
    übernommen wird auf der Seite Eröffnungsbilanz (Einstellungen › Geschäftsjahre)."""
    return _call(api.opening_balance)


def closing_entry(jahr: int, art: str) -> dict:
    """Eine Abschlussbuchung ansehen: angefangene Arbeiten (arbeiten), Materialvorrat (material), Delkredere,
    Ferien-/Überstundenguthaben (ferien) oder Privatanteile (privatanteile) — Saldo ohne diese Buchung, was gebucht
    ist, und Vorschläge (offene Leistungen, Akontos, 5 % der Debitoren, Ferien-/Überstundensalden, Fahrzeuge).
    Ändert nichts; gebucht wird auf Abschluss › Checkliste.

    Args:
        jahr: Geschäftsjahr.
        art: arbeiten, material, delkredere, ferien oder privatanteile.
    """
    return _call(api.closing_entry, jahr, art)


def mwst_report(periode: str) -> dict:
    """MWST-Abrechnung einer Periode nach ESTV-Ziffern (Umsatz, Steuer, Vorsteuer, Zahllast).

    Args:
        periode: z.B. "2026-Q1" (effektive Methode) oder "2026-S1" (Saldosteuersatz).
    """
    return _call(api.mwst_report, periode)


def mwst_details(periode: str) -> dict:
    """MWST-Report: woher die Zahlen einer Abrechnung kommen — jede Buchungszeile mit MWST-Code,
    gruppiert nach ESTV-Ziffer, mit Summe und Abgleich gegen die Ziffer.

    Args:
        periode: z.B. "2026-Q1" oder "2026-S1".
    """
    return _call(api.mwst_details, periode)


def mwst_reconciliation(jahr: int, als_pdf: bool = False) -> dict:
    """MWST-Umsatzabstimmung eines Geschäftsjahres: Umsatz und Steuer laut Buchhaltung gegen die gebuchten
    Abrechnungen (bei vereinnahmten Entgelten mit Überleitung über die offenen Debitoren/Kreditoren),
    Ertrag ohne MWST-Code und die Salden der MWST-Konten. Differenzen → Jahresabstimmung (Berichtigungsabrechnung nach Art. 72 MWSTG).

    Args:
        jahr: Geschäftsjahr, z.B. 2026.
        als_pdf: Abstimmung zusätzlich als PDF unter berichte/ ablegen.
    """
    return _in_book(_call(api.mwst_abstimmung, jahr, als_pdf=als_pdf), "pdf")


# ---------- debitoren ----------

def customers() -> list | dict:
    """Kundenliste."""
    return _call(api.customer_list)


def customer_details(nummer: str) -> dict:
    """Alle Angaben eines Kunden: Firma, Kontaktperson, Adresse, E-Mail, Rechnungsempfänger, Stundensatz, Kategorie,
    Herkunft, feste Notizen; dazu Umsatz, offene Rechnungen, offene Akontos, Zahlungsmoral, die letzten
    Log-Einträge und offene Wiedervorlagen.

    Args:
        nummer: Kundennummer, z.B. "K0004".
    """
    def get(b):
        d = api.customer_dossier(b, nummer)
        return {**d["kunde"], "notizen": d["feste_infos"], "kennzahlen": d["kennzahlen"],
                "zahlungsmoral": d["zahlungsmoral"],
                "offene_rechnungen": [r for r in d["rechnungen"] if r["status"] in ("offen", "teilbezahlt")],
                "akonto_offen": d["akonto_offen"], "log": d["log"][-5:], "wiedervorlagen": d["wiedervorlagen"]}
    return _call(get)


def log_customer(kunde: str, text: str, art: str = "notiz", datum: str = "", nachfassen: str = "") -> dict:
    """Ins Kunden-Log schreiben, was besprochen oder vereinbart wurde (mit Datum und wer). Dauerhafte Fakten
    (Zugang, Wünsche) gehören dagegen mit update_customer(notiz=…) zu den festen Infos.

    Args:
        kunde: Kundennummer, z.B. "K0003".
        text: was besprochen/vereinbart wurde, in ganzen Sätzen.
        art: anruf, besuch, email oder notiz.
        datum: JJJJ-MM-TT, leer = heute.
        nachfassen: JJJJ-MM-TT, an dem nachgefragt werden soll (Wiedervorlage), sonst leer.
    """
    return _call(api.customer_log_add, kunde, text, art, datum or None, nachfassen or None)


def follow_ups(bis: str = "") -> list | dict:
    """Offene Wiedervorlagen aus den Kunden-Logs (was bei welchem Kunden nachzufragen ist), älteste zuerst.

    Args:
        bis: nur die bis zu diesem Datum fälligen (JJJJ-MM-TT), leer = alle offenen.
    """
    return _call(api.customer_followups, bis or None)


def _same_customer(book: Book, name: str, firma: str) -> list[str]:
    """Existing customers with the same company (or, for private persons, the same name)."""
    from . import invoices as inv

    def norm(x) -> str:
        return " ".join(str(x or "").lower().replace(".", " ").split())
    key = norm(firma) or norm(name)
    return [f"{k} {c.get('firma') or c.get('name')}" for k, c in inv.customers(book).items()
            if key and key in (norm(c.get("firma")) or norm(c.get("name")), norm(c.get("name")))]


def add_customer(name: str, firma: str = "", strasse: str = "", nr: str = "", plz: str = "", ort: str = "",
                 land: str = "CH", email: str = "", telefon: str = "", mobil: str = "", rechnung_an: str = "firma",
                 notizen: str = "", kategorie: str = "", herkunft: str = "", anrede: str = "",
                 trotzdem_neu: bool = False) -> dict:
    """Kunde anlegen. Gibt es den Kunden schon (gleiche Firma bzw. gleicher Name), wird nichts angelegt:
    dann update_customer verwenden, um Angaben zu ergänzen oder zu korrigieren.

    Args:
        name: Kontaktperson oder Privatperson.
        firma: Firmenname (leer bei Privatpersonen).
        strasse: Strasse.
        nr: Hausnummer.
        plz: Postleitzahl.
        ort: Ort.
        land: Ländercode, Standard CH.
        email: E-Mail.
        telefon: Telefonnummer (Festnetz oder Geschäft), wie genannt.
        mobil: Mobile-/Natelnummer, wie genannt.
        rechnung_an: "firma" oder "person".
        notizen: alles Weitere, das genannt wurde und kein eigenes Feld hat (Zugang, Wünsche, Erreichbarkeit …),
            in ganzen Sätzen; nichts Genanntes weglassen.
        kategorie: Privat, Verwaltung, Architekt, Firma oder Öffentlich (leer, wenn unklar).
        herkunft: wie der Kontakt zustande kam, z.B. "Empfehlung Hans Muster", "Website".
        anrede: "Herr", "Frau" oder "Familie" für die Kontakt- bzw. Privatperson (Briefanrede); leer, wenn unklar.
        trotzdem_neu: nur wenn der Mensch ausdrücklich einen zweiten Kunden gleichen Namens will (z.B. andere Filiale).
    """
    try:
        same = _same_customer(book(), name, firma)
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    if same and not trotzdem_neu:
        return {"ok": False, "fehler": f"Diesen Kunden gibt es schon: {', '.join(same)}. Angaben mit update_customer "
                                       "ergänzen oder korrigieren statt neu anlegen.", "vorhanden": same}
    return _call(api.customer_add, name=name, firma=firma, strasse=strasse, nr=nr, plz=plz, ort=ort,
                 land=land, email=email, telefon=telefon, mobil=mobil, rechnung_an=rechnung_an,
                 notizen=notizen.strip(), kategorie=kategorie.strip(), herkunft=herkunft.strip(),
                 anrede=anrede.strip())


def update_customer(nummer: str, name: str = "", firma: str = "", strasse: str = "", nr: str = "", plz: str = "",
                    ort: str = "", land: str = "", email: str = "", telefon: str = "", mobil: str = "",
                    rechnung_an: str = "", kategorie: str = "", herkunft: str = "", notiz: str = "",
                    anrede: str = "", leeren: list[str] | None = None) -> dict:
    """Angaben eines bestehenden Kunden ergänzen, korrigieren oder entfernen (nur die mitgegebenen Felder; leer =
    unverändert; was weg soll, in «leeren» nennen). Bereits ausgestellte Rechnungen behalten die Adresse, die beim
    Ausstellen galt; Offerten-Entwürfe mit update_quote(adresse_neu=True) nachführen.

    Args:
        nummer: Kundennummer, z.B. "K0003" (customers() zeigt sie).
        name: Kontaktperson oder Privatperson.
        firma: Firmenname.
        strasse: Strasse.
        nr: Hausnummer.
        plz: Postleitzahl.
        ort: Ort.
        land: Ländercode.
        email: E-Mail.
        telefon: Telefonnummer (Festnetz oder Geschäft).
        mobil: Mobile-/Natelnummer.
        rechnung_an: "firma" oder "person".
        kategorie: Privat, Verwaltung, Architekt, Firma oder Öffentlich.
        herkunft: wie der Kontakt zustande kam, z.B. "Empfehlung Hans Muster".
        anrede: "Herr", "Frau" oder "Familie" (Briefanrede der Kontakt- bzw. Privatperson).
        notiz: wird an die Notizen des Kunden angehängt (bestehende bleiben stehen): weitere Angaben ohne eigenes Feld.
        leeren: Felder, die entfernt werden sollen, z.B. ["name"] für eine Kontaktperson oder ["email"].
    """
    fields = {k: v for k, v in {"name": name, "firma": firma, "strasse": strasse, "nr": nr, "plz": plz, "ort": ort,
                                "land": land, "email": email, "telefon": telefon, "mobil": mobil,
                                "rechnung_an": rechnung_an, "kategorie": kategorie,
                                "herkunft": herkunft, "anrede": anrede}.items() if str(v).strip()}
    clearable = {"name", "firma", "strasse", "nr", "plz", "ort", "email", "telefon", "mobil", "kategorie", "herkunft",
                 "anrede"}
    for key in leeren or []:
        if key not in clearable:
            return {"ok": False, "fehler": f"«{key}» kann nicht geleert werden (möglich: {', '.join(sorted(clearable))})"}
        fields[key] = ""
    notizen = None
    if notiz.strip():
        def current(b):
            from . import invoices as inv
            return inv.customer(b, nummer).get("_notizen", "")
        try:
            before = current(book()).strip()
        except BookError as exc:
            return {"ok": False, "fehler": str(exc)}
        notizen = (before + "\n\n" if before else "") + notiz.strip() + "\n"
    if not fields and notizen is None:
        return {"ok": False, "fehler": "Keine Änderung angegeben"}
    return _call(api.customer_update, nummer, notizen=notizen, **fields)


def invoices(status: str = "") -> list | dict:
    """Rechnungen mit offenem Betrag.

    Args:
        status: offen, teilbezahlt, bezahlt, storniert; leer = alle.
    """
    return _call(api.invoice_list, status)


def create_invoice(kunde: str, positionen: list[dict], datum: str = "", text: str = "",
                   leistung_von: str = "", leistung_bis: str = "", rabatt_prozent: str = "",
                   art: str = "rechnung", akonto: str = "alle", objekt: str = "", offerte_nummer: str = "",
                   offerte_datum: str = "",
                   expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """QR-Rechnung ausstellen und verbuchen; erzeugt die PDF mit QR-Einzahlungsschein.
    Auch Akontorechnungen (art="akonto") und Schlussrechnungen, die offene Akontos abziehen.

    Args:
        kunde: Kundennummer, z.B. "K0001".
        positionen: z.B. [{"text": "Beratung", "menge": 10, "einheit": "h", "preis": "150.00", "konto": "3400"}] (konto optional).
        datum: Rechnungsdatum JJJJ-MM-TT; leer = heute.
        text: Einleitungstext auf der Rechnung.
        leistung_von: Leistungsdatum bzw. Beginn des Leistungszeitraums JJJJ-MM-TT, wenn er vom Rechnungsdatum
            abweicht (MWST-Rechnungen müssen ihn nennen, Art. 26 MWSTG).
        leistung_bis: Ende des Leistungszeitraums JJJJ-MM-TT; leer bei einem einzelnen Tag.
        rabatt_prozent: Rabatt auf die ganze Rechnung in Prozent, z.B. "10"; wird auf 3800 Erlösminderungen
            gebucht. Nie selbst als negative Position rechnen.
        art: "rechnung" oder "akonto" (Akontorechnung: Anzahlung, gebucht auf Erhaltene Anzahlungen).
        akonto: Welche offenen Akontorechnungen dieses Kunden abgezogen werden: "alle", "keine" oder
            Nummern "R-2026-0012,R-2026-0015". Siehe open_akonto.
        objekt: Objekt/Baustelle, an dem gearbeitet wurde, z.B. "MFH Bernstrasse 12, 3600 Thun" (steht auf der
            Rechnung); leer, wenn keins genannt.
        offerte_nummer: Nummer der Offerte, auf die sich die Rechnung stützt (leer, wenn keine).
        offerte_datum: Datum dieser Offerte JJJJ-MM-TT («Ihre Offerte vom» auf der Rechnung).
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    offerte = {"nummer": offerte_nummer, "datum": offerte_datum} if offerte_nummer or offerte_datum else None
    return _call(api.invoice_create, kunde, positionen, datum or None, text, None, "", None,
                 leistung_von or None, leistung_bis or None, rabatt=rabatt_prozent or None,
                 art=art or "rechnung", akonto=None if art == "akonto" else (akonto or "alle"), objekt=objekt,
                 offerte=offerte, expected_revision=expected_revision, idempotency_key=idempotency_key)


def open_akonto(kunde: str = "") -> list | dict:
    """Akontorechnungen, die noch in keiner Schlussrechnung angerechnet sind (bezahlt oder offen).

    Args:
        kunde: Kundennummer; leer = alle Kunden.
    """
    return _call(api.invoice_open_akonto, kunde)


def match_payment(betrag: str, text: str = "") -> dict:
    """Findet die offene Rechnung zu einem Zahlungseingang (Referenz, Rechnungsnummer, Betrag).

    Args:
        betrag: Betrag des Zahlungseingangs.
        text: Zeile aus dem Bankauszug (Referenz, Mitteilung).
    """
    return _call(api.invoice_match, betrag, text)


def pay_invoice(nummer: str, betrag: str = "", datum: str = "", konto: str = "",
                expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """Zahlungseingang auf Rechnung buchen (Bank an Debitoren).

    Args:
        nummer: Rechnungsnummer, z.B. "R-2026-0001".
        betrag: leer = ganzer offener Betrag.
        datum: Valuta JJJJ-MM-TT; leer = heute.
        konto: Geldkonto; leer = Bank aus den Einstellungen.
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _call(api.invoice_pay, nummer, betrag or None, datum or None, konto or None,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


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


# ---------- kreditoren ----------

def suppliers() -> list | dict:
    """Lieferanten mit IBAN, Standard-Aufwandkonto und MWST-Code."""
    return _call(api.supplier_list)


def add_supplier(name: str, strasse: str = "", nr: str = "", plz: str = "", ort: str = "", land: str = "CH",
                 iban: str = "", konto: str = "", mwst: str = "") -> dict:
    """Lieferant anlegen (z.B. mit den Daten aus scan_qr_bill).

    Args:
        name: Name des Lieferanten.
        strasse: Strasse.
        nr: Hausnummer.
        plz: Postleitzahl.
        ort: Ort.
        land: Ländercode.
        iban: IBAN oder QR-IBAN.
        konto: Standard-Aufwandkonto, z.B. "6510" (nur agent_modus direkt; sonst legt es der Mensch fest).
        mwst: Standard-MWST-Code, z.B. "V81".
    """
    if konto:
        try:
            mode = book().settings.get("agent_modus") or "vorschlag"
        except BookError as exc:
            return {"ok": False, "fehler": str(exc)}
        if mode != "direkt":
            return {"ok": False, "fehler": "agent_modus 'vorschlag': das Aufwandkonto eines Lieferanten legt ein Mensch "
                    "fest. Lieferant ohne konto anlegen und dem Menschen das passende Konto vorschlagen."}
    return _call(api.supplier_add, name=name, strasse=strasse, nr=nr, plz=plz, ort=ort, land=land, iban=iban,
                 konto=konto, mwst=mwst)


def bank_rules() -> list | dict:
    """Bankregeln: wiederkehrende Bewegungen (Gegenpartei/Text → Konto), die der Import selbst bucht.
    Neue Regeln legt ein Mensch an (Bank → «Immer so buchen» bzw. `aeradex bank regel`)."""
    return _call(api.bank_rules)


def expenses(offen: bool = True) -> list | dict:
    """Spesenbelege der Mitarbeitenden (privat bezahlt, Rückzahlung über den Lohn).

    Args:
        offen: nur noch nicht ausbezahlte.
    """
    return _call(api.expense_list, "", offen)


def overdue_invoices() -> list | dict:
    """Überfällige Kundenrechnungen mit den bisherigen Mahnungen und dem nächsten Schritt.
    Mahnungen erstellt ein Mensch (Debitoren → Mahnungen bzw. `aeradex mahnung erstellen`)."""
    return _call(api.reminders)


def bill_drafts() -> list | dict:
    """Beleg-Entwürfe im Eingang: hochgeladene Belege, ausgelesen, noch nicht gebucht. Art: kreditor
    (Lieferantenrechnung), quittung (bereits bezahlt), debitor (eigene, extern erstellte Rechnung).
    Status: bereit, unsicher/agent (Konto fehlt), konflikt (Name ≠ Inhaber der IBAN), unvollstaendig."""
    return _call(api.bill_drafts)


def bill_draft(id: str) -> dict:
    """Einen Kreditoren-Entwurf mit allen erkannten Feldern (und deren Quelle) und dem erkannten Text der Rechnung.

    Args:
        id: z.B. "ENT-0001".
    """
    from . import erfassung

    def get(b):
        meta = erfassung.draft(b, id)
        cache = erfassung.text_cache(b, meta["id"])
        out = {k: v for k, v in meta.items() if not k.startswith("_")}
        out["text"] = cache.read_text(encoding="utf-8")[:6000] if cache.exists() else ""
        return api.jsonable(out)
    return _call(get)


def create_bill_draft(datei: str, art: str = "") -> dict:
    """Einen Beleg aus der Inbox einlesen (QR-Zahlteil, Text, OCR) und als Entwurf anlegen. Bucht nichts.
    Die Art wird erkannt; bekannte Lieferanten bringen ihr Konto mit, Quittungen werden mit der Bank abgeglichen.

    Args:
        datei: z.B. "inbox/rechnung.pdf".
        art: leer = erkennen; sonst kreditor, quittung oder debitor (oder eine Art eines Plugins, z.B. regie).
    Plugins übernehmen ihre Dokumente selbst (z.B. gescannte Rapporte): dann steht «url» statt «entwurf» im Ergebnis.
    """
    return _call(api.inbox_take, datei, art)


def complete_bill_draft(id: str, konto: str, begruendung: str, mwst: str = "", betrag: str = "", datum: str = "",
                        faellig: str = "", rechnungsnr: str = "", name: str = "", iban: str = "", waehrung: str = "",
                        aufteilung: list[dict] | None = None, art: str = "", zahlkonto: str = "",
                        mitarbeiter: str = "", leistung_von: str = "", leistung_bis: str = "") -> dict:
    """Einen Beleg-Entwurf kontieren und fehlende/falsch erkannte Felder korrigieren. Bucht nichts —
    ein Mensch prüft den Entwurf und bucht. Auch im agent_modus 'vorschlag' erlaubt.

    Args:
        id: Entwurf, z.B. "ENT-0001".
        konto: Aufwandkonto (bei Anschaffungen Aktivkonto), aus accounts().
        begruendung: ein Satz, warum dieses Konto (wird dem Menschen gezeigt).
        mwst: Vorsteuer-Code (V81 Material/DL, I81 Investitionen/übriger Aufwand …), nur bei effektiver Methode
            und wenn die Rechnung MWST ausweist; sonst leer lassen.
        betrag: Bruttobetrag "123.45", nur wenn falsch oder fehlend.
        datum: Rechnungsdatum JJJJ-MM-TT, nur wenn falsch oder fehlend.
        faellig: Fälligkeit JJJJ-MM-TT, nur wenn falsch oder fehlend.
        rechnungsnr: Rechnungsnummer des Lieferanten, nur wenn falsch oder fehlend.
        name: Name des Lieferanten, nur wenn falsch oder fehlend.
        iban: IBAN des Lieferanten, nur wenn falsch oder fehlend.
        waehrung: Währung der Rechnung (EUR, USD …), wenn nicht CHF; umgerechnet wird zum BAZG-Kurs des Rechnungsdatums.
        aufteilung: nur wenn Positionen auf verschiedene Konten gehören: [{"konto": "6500", "betrag": "80.00",
            "mwst": "I81", "text": "Papier"}, …]; Beträge brutto in der Rechnungswährung, Summe = Rechnungsbetrag.
            `konto` ist dann das Konto der ersten Position.
        art: nur wenn die erkannte Art falsch ist: kreditor (offene Lieferantenrechnung), quittung (schon bezahlt),
            debitor (eigene Rechnung an einen Kunden).
        zahlkonto: nur bei Quittungen, wenn das erkannte Zahlkonto falsch ist (z.B. "1000" Kasse bei Barzahlung,
            ein Kreditkartenkonto, oder das Konto gegenüber der Person, die privat bezahlt hat).
        mitarbeiter: nur bei Quittungen, die eine Mitarbeiterin/ein Mitarbeiter privat bezahlt hat (z.B. "M0001"):
            dann wird sie als Spesenbeleg erfasst und mit dem nächsten Lohn zurückbezahlt.
        leistung_von: Leistungsdatum bzw. Beginn des Leistungszeitraums JJJJ-MM-TT, wenn der Beleg es nennt
            (Abo, Miete, Versicherung, Lizenz, Lieferdatum) — für die Abgrenzung im Jahresabschluss.
        leistung_bis: Ende des Leistungszeitraums JJJJ-MM-TT; leer bei einem einzelnen Tag.
    """
    fields = {k: v for k, v in (("betrag", betrag), ("datum", datum), ("faellig", faellig),
                                ("rechnungsnr", rechnungsnr), ("name", name), ("iban", iban),
                                ("leistung_von", leistung_von), ("leistung_bis", leistung_bis),
                                ("waehrung", (waehrung or "").upper())) if v}
    return _call(api.bill_draft_update, id, "Agent", konto, mwst or None, begruendung, positionen=aufteilung,
                 art=art, zahlkonto=zahlkonto, mitarbeiter=mitarbeiter, **fields)


def scan_qr_bill(datei: str) -> dict:
    """Liest den Swiss-QR-Zahlteil einer Rechnung (PDF oder Foto, z.B. "inbox/rechnung.pdf"): IBAN, Betrag,
    Referenz, Lieferant. 'lieferant' ist die Nummer eines bekannten Lieferanten mit derselben IBAN, sonst leer.

    Args:
        datei: Pfad relativ zum Buch.
    """
    return _call(api.qr_scan, datei)


def add_supplier_bill(lieferant: str, betrag: str, datum: str = "", faellig: str = "", rechnungsnr: str = "",
                      referenz: str = "", referenz_typ: str = "", mitteilung: str = "", datei: str = "",
                      konto: str = "", mwst: str = "", waehrung: str = "",
                      aufteilung: list[dict] | None = None) -> dict:
    """Lieferantenrechnung erfassen und verbuchen (Aufwand an Kreditoren). Im agent_modus 'vorschlag' nur mit
    dem beim Lieferanten hinterlegten Aufwandkonto (konto leer lassen); sonst propose_booking verwenden.

    Args:
        lieferant: Lieferantennummer, z.B. "L0001".
        betrag: Bruttobetrag laut Rechnung.
        datum: Rechnungsdatum JJJJ-MM-TT.
        faellig: Fälligkeit JJJJ-MM-TT (leer = +30 Tage).
        rechnungsnr: Rechnungsnummer des Lieferanten.
        referenz: QR- oder SCOR-Referenz von der QR-Rechnung.
        referenz_typ: QRR, SCOR oder NON.
        mitteilung: Zahlungsmitteilung, falls keine Referenz.
        datei: die Rechnung, z.B. "inbox/rechnung.pdf" (wird nach belege/ verschoben).
        konto: abweichendes Aufwandkonto (nur agent_modus direkt).
        mwst: abweichender MWST-Code (sonst der des Lieferanten).
        waehrung: Währung der Rechnung, wenn nicht CHF (EUR, USD …); betrag ist dann in dieser Währung,
            gebucht wird zum BAZG-Kurs des Rechnungsdatums.
        aufteilung: Positionen auf verschiedene Konten (nur agent_modus direkt):
            [{"konto": "6500", "betrag": "80.00", "mwst": "I81", "text": "Papier"}, …], Summe = betrag.
    """
    try:
        b = book()
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    if aufteilung and (b.settings.get("agent_modus") or "vorschlag") != "direkt":
        return {"ok": False, "fehler": "agent_modus 'vorschlag': Aufteilungen über einen Kreditoren-Entwurf "
                "(create_bill_draft, complete_bill_draft mit aufteilung) — ein Mensch prüft und erfasst."}
    if konto and (b.settings.get("agent_modus") or "vorschlag") != "direkt":
        from . import kreditoren as kred
        try:
            stored = kred.supplier(b, lieferant).get("konto")
        except BookError as exc:
            return {"ok": False, "fehler": str(exc)}
        if str(konto) != str(stored or ""):
            return {"ok": False, "fehler": "agent_modus 'vorschlag': nur das beim Lieferanten hinterlegte Konto. "
                    "Konto leer lassen oder den Menschen das Konto beim Lieferanten hinterlegen lassen."}
    fields = {k: v for k, v in {"datum": datum, "faellig": faellig, "rechnungsnr": rechnungsnr, "referenz": referenz,
                                "referenz_typ": referenz_typ, "mitteilung": mitteilung, "datei": datei,
                                "konto": konto, "waehrung": waehrung}.items() if v}
    if mwst:
        fields["mwst"] = mwst
    if aufteilung:
        fields["positionen"] = aufteilung
    return _call(api.bill_add, lieferant, betrag, **fields)


def supplier_bills(status: str = "") -> list | dict:
    """Lieferantenrechnungen mit offenem Betrag.

    Args:
        status: offen, angewiesen, bezahlt, storniert; leer = alle.
    """
    return _call(api.bill_list, status)


def create_payment_run(nummern: list[str], ausfuehrung: str) -> dict:
    """Zahlungsdatei (pain.001) für das E-Banking erstellen. Bewegt kein Geld: der Mensch lädt sie hoch.

    Args:
        nummern: z.B. ["E-2026-0001"].
        ausfuehrung: gewünschtes Ausführungsdatum JJJJ-MM-TT.
    """
    return _call(api.payment_run, nummern, ausfuehrung)


# ---------- bank ----------

def bank_transactions(status: str = "offen") -> list | dict:
    """Bankbewegungen aus importierten Kontoauszügen (camt.053). Betrag positiv = Gutschrift, negativ = Belastung.

    Args:
        status: offen (Standard), gebucht, abgeglichen, ignoriert; leer = alle.
    """
    return _call(api.bank_list, status)


def bank_suggestions(ids: list[str] | None = None) -> dict:
    """Was die offenen Bankbewegungen wahrscheinlich sind, je Bewegung beste Zuordnung zuerst: offene Rechnung,
    offener Kreditor, Quittungs-Entwurf, bestehende Buchung oder Konto aus früheren Buchungen derselben Gegenpartei.
    `sicher` heisst: Betrag und Name passen eindeutig. Zuerst hier schauen, dann zuordnen (assign_bank_transaction)
    oder eine Buchung vorschlagen (propose_bank_booking); übernehmen per Klick tut der Mensch.

    Args:
        ids: nur diese Bewegungen; leer = alle offenen.
    """
    return _call(api.bank_suggestions, ids or None)


def assign_bank_transaction(id: str, nummer: str) -> dict:
    """Offene Bankbewegung mit einer offenen Kundenrechnung (R-…) oder Lieferantenrechnung (E-…) begleichen.

    Args:
        id: ID der Bankbewegung, z.B. "B1a2b3c4d5e".
        nummer: "R-2026-0001" oder "E-2026-0003".
    """
    return _call(api.bank_assign, id, nummer)


def propose_bank_booking(id: str, konto: str, text: str, begruendung: str, mwst: str = "") -> dict:
    """Buchung für eine offene Bankbewegung vorschlagen (Gegenkonto wählen, z.B. 6940 Bankspesen). Datum, Betrag und
    Bankkonto kommen aus der Bewegung; nach der Freigabe gilt die Bewegung als gebucht.

    Args:
        id: ID der Bankbewegung.
        konto: Gegenkonto (Aufwand, Ertrag, …).
        text: Buchungstext.
        begruendung: kurz, warum dieses Konto.
        mwst: MWST-Code, falls die Bewegung MWST enthält (Betrag ist brutto).
    """
    try:
        b = book()
        from . import bank as bk
        tx = bk.find(b, id)
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    amount = Decimal(tx["Betrag"])
    soll, haben = (tx["Konto"], konto) if amount > 0 else (konto, tx["Konto"])
    return _call(api.propose, tx["Datum"], soll, haben, str(abs(amount)), text, begruendung, "", mwst, id)


def suggest_bank_accounts(ids: list[str] | None = None) -> dict:
    """Lässt Jev (TypeSafe, falls im Buch eingeschaltet) Gegenkonten für offene Bankbewegungen vorschlagen:
    sichere Fälle werden Vorschläge (Buchhaltung › Vorschläge), unsichere bekommen nur einen Hinweis. Schnell und günstig —
    vor eigenen Vorschlägen aufrufen und die Hinweise als zweite Meinung nutzen.

    Args:
        ids: nur diese Bewegungen; leer = alle offenen.
    """
    return _call(api.bank_suggest, ids or None)


def book_bank_transaction(id: str, konto: str, text: str = "", mwst: str = "") -> dict:
    """Offene Bankbewegung direkt gegen ein Konto buchen (nur agent_modus: direkt).

    Args:
        id: ID der Bankbewegung.
        konto: Gegenkonto.
        text: Buchungstext.
        mwst: MWST-Code (Betrag brutto).
    """
    return _direct(api.bank_book, id, konto, text, mwst)


def transfer_bank_transaction(id: str, konto: str, text: str = "") -> dict:
    """Offene Bankbewegung als Übertrag zwischen eigenen Bankkonten buchen (Bank an Bank, nur agent_modus:
    direkt); die Gegenbewegung auf dem anderen Konto wird mit demselben Beleg abgeglichen.

    Args:
        id: ID der Bankbewegung.
        konto: das andere eigene Bankkonto, z.B. "1022".
        text: Buchungstext; leer = «Übertrag … → …».
    """
    return _direct(api.bank_transfer, id, konto, text)


# ---------- lohn ----------

def bank_file_preview(datei: str) -> dict:
    """Die ersten Zeilen eines Kontoauszugs (CSV/Excel), um sein Format zu beschreiben, und die schon
    beschriebenen Formate.

    Args:
        datei: z.B. "inbox/ubs-export.csv".
    """
    return _call(api.bank_file_preview, datei)


def propose_bank_format(datei: str, name: str, kopfzeile: list[str], datum: str, datumsformat: str,
                        betrag: str = "", belastung: str = "", gutschrift: str = "", vorzeichen: int = 1,
                        dezimal: str = ".", text: list[str] | None = None, gegenpartei: str = "",
                        referenz: str = "", saldo: str = "", id: str = "", waehrung: str = "",
                        trennzeichen: str = "", konto: str = "") -> dict:
    """Das Format eines CSV/Excel-Kontoauszugs beschreiben. aeradex liest die Datei damit und meldet, was es
    gelesen hat (pruefung.ok, Beispiele, Saldo-Prüfung Zeile für Zeile). Bucht und importiert nichts; ein
    Mensch bestätigt das Format, danach liest aeradex jede Datei dieser Bank selbst. Erneut aufrufen
    (gleicher name) überschreibt die Beschreibung.

    Args:
        datei: die Datei, z.B. "inbox/ubs-export.csv".
        name: Bank und Export, z.B. "UBS Kontoauszug CSV".
        kopfzeile: alle Spaltennamen der Kopfzeile, genau wie in der Datei, in dieser Reihenfolge.
        datum: Spalte mit dem Buchungsdatum.
        datumsformat: strftime-Format, z.B. "%d.%m.%Y" oder "%Y-%m-%d".
        betrag: Spalte mit dem Betrag mit Vorzeichen — oder leer und dafür belastung/gutschrift.
        belastung: Spalte mit Belastungen (Geld hinaus), wenn getrennt.
        gutschrift: Spalte mit Gutschriften (Geld herein), wenn getrennt.
        vorzeichen: -1, wenn in der Datei Belastungen positiv sind (bei `betrag`), sonst 1.
        dezimal: "." oder ",".
        text: Spalten für den Buchungstext (werden verbunden).
        gegenpartei: Spalte mit Auftraggeber/Empfänger, wenn vorhanden.
        referenz: Spalte mit Zahlungsreferenz, wenn vorhanden.
        saldo: Spalte mit dem Saldo nach der Bewegung, wenn vorhanden (wichtig für die Prüfung).
        id: Spalte mit einer eindeutigen Transaktions-ID, wenn vorhanden.
        waehrung: Spalte mit der Währung, wenn vorhanden.
        trennzeichen: nur wenn die Erkennung falsch liegt (";", ",", Tab).
        konto: Buchhaltungskonto (z.B. "1020"); leer, wenn die Datei eine zugeordnete IBAN enthält.
    """
    spec = {"kopfzeile": kopfzeile, "datum": datum, "datumsformat": datumsformat, "betrag": betrag,
            "belastung": belastung, "gutschrift": gutschrift, "vorzeichen": vorzeichen if vorzeichen == -1 else None,
            "dezimal": dezimal if dezimal == "," else None, "text": text or [], "gegenpartei": gegenpartei,
            "referenz": referenz, "saldo": saldo, "id": id, "waehrung": waehrung, "trennzeichen": trennzeichen,
            "konto": konto}
    return _call(api.bank_format_propose, datei, name, **spec)


def card_statement_text(datei: str) -> dict:
    """Der Text einer Kreditkartenabrechnung (PDF-Textebene, sonst OCR).

    Args:
        datei: z.B. "inbox/visa-2026-09.pdf".
    """
    return _call(api.card_statement_text, datei)


def propose_card_statement(datei: str, konto: str, saldo_alt: str, saldo_neu: str, buchungen: list[dict],
                           herausgeber: str = "", karte: str = "", von: str = "", bis: str = "") -> dict:
    """Die Transaktionen einer Kreditkartenabrechnung erfassen. aeradex prüft: saldo_alt + Buchungen = saldo_neu
    auf den Rappen, und jeder Betrag steht im Text der Abrechnung. Bucht nichts. Erneut aufrufen überschreibt.

    Args:
        datei: die Abrechnung, z.B. "inbox/visa-2026-09.pdf".
        konto: das Kreditkartenkonto (Passivkonto), z.B. "2040".
        saldo_alt: Saldo der Vorperiode, wie auf der Abrechnung ("1234.50"; Guthaben negativ).
        saldo_neu: neuer Saldo / zu bezahlender Betrag ("987.65").
        buchungen: [{"datum": "2026-08-22", "text": "Migros Bern", "betrag": "45.60", "original": "EUR 48.00"}, …];
            betrag in CHF wie auf der Abrechnung: Belastungen positiv, Zahlungen/Gutschriften negativ;
            Gebühren und Zinsen als eigene Zeilen; original nur bei Fremdwährung.
        herausgeber: z.B. "Viseca", "Swisscard", "Cornèrcard".
        karte: nur die letzten 4 Ziffern, z.B. "1234".
        von: Beginn der Abrechnungsperiode JJJJ-MM-TT.
        bis: Abrechnungsdatum JJJJ-MM-TT.
    """
    return _call(api.card_statement_propose, datei, konto, saldo_alt, saldo_neu, buchungen, herausgeber, karte,
                 von, bis)


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


def payroll_payment_export(monat: str, ausfuehrung: str) -> dict:
    """Bankdatei pain.001 für abgeschlossene Löhne erstellen; kein Bankversand.

    Args:
        monat: JJJJ-MM.
        ausfuehrung: Gewünschtes Zahlungsdatum JJJJ-MM-TT.
    """
    return _call(api.payroll_payment_export, monat, ausfuehrung)


def qst_sync(kanton: str, jahr: int) -> dict:
    """Offizielle Quellensteuertarife der ESTV laden oder aktualisieren.

    Args:
        kanton: Kantonskürzel oder ALLE.
        jahr: Tarifjahr ab 2025.
    """
    return _call(api.qst_sync, kanton, jahr)


def payslip(monat: str, mitarbeiter: str) -> dict:
    """Eine Lohnabrechnung anzeigen.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.payslip_show, monat, mitarbeiter)


def close_payslip(monat: str, mitarbeiter: str) -> dict:
    """Einzelne Lohnabrechnung abschliessen: friert Werte ein, erzeugt PDF und erneuert die
    Sammelbuchung des Monats. Für den ganzen Lohnlauf close_payroll verwenden.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.payslip_close, monat, mitarbeiter)


def close_payroll(monat: str) -> dict:
    """Lohnlauf abschliessen: alle Entwürfe des Monats einfrieren, PDFs erzeugen und als
    eine Sammelbuchung (Beleg L-JJJJ-MM) verbuchen — keine Einzelbuchung pro Mitarbeiter.

    Args:
        monat: JJJJ-MM.
    """
    return _call(api.payroll_close, monat)


def lohnausweis(jahr: int, mitarbeiter: str) -> dict:
    """Lohnausweis (Formular 11) aus den abgeschlossenen Abrechnungen des Jahres.
    Alle Monate der Anstellung müssen gerechnet und abgeschlossen sein.

    Args:
        jahr: Kalenderjahr.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.lohnausweis_create, jahr, mitarbeiter)


def payroll_account(jahr: int, mitarbeiter: str = "") -> dict:
    """Lohnkonto als Monatsmatrix (Lohnarten, Abzüge, Arbeitgeberbeiträge, Jahrestotal), nur abgeschlossene
    Abrechnungen. Ohne mitarbeiter die ganze Firma.

    Args:
        jahr: Kalenderjahr.
        mitarbeiter: z.B. "M0001"; leer = ganze Firma.
    """
    return _call(api.lohnkonto_firma, jahr, mitarbeiter or None)


def wage_declarations(jahr: int) -> dict:
    """Zahlen für die Jahres-Lohnmeldungen: Ausgleichskasse (AHV/IV/EO, ALV, FAK), UVG, KTG, BVG und
    Quellensteuer, pro Person und total (nach Geschlecht), Abgleich mit der Buchhaltung und Prüfhinweise
    (fehlende Angaben, Rentner, Jugendliche, Höchstlohn). Nur lesen; die Hinweise vor dem Einreichen klären.

    Args:
        jahr: Kalenderjahr.
    """
    return _call(api.lohnmeldungen, jahr)


# ---------- Personal ----------

_EMPLOYEE_ARGS = ("strasse", "nr", "plz", "ort", "land", "ahv_nr", "geburtsdatum", "geschlecht", "eintritt", "lohnart",
                  "monatslohn", "pensum", "stundenlohn", "standard_stunden", "vollzeit_stunden_woche",
                  "ferienzuschlag_satz", "bvg_betrag", "ag_bvg_betrag", "kinderzulagen", "aufenthalt", "qst_pflicht",
                  "qst_tarif", "qst_kanton", "qst_satz", "iban", "notizen")


def _employee_fields(values: dict) -> dict:
    """Tool arguments → the record's fields: empty ones left out, the Quellensteuer tariff as {code, kanton}."""
    fields = {k: v for k, v in values.items() if v not in ("", None)}
    tarif, kanton = fields.pop("qst_tarif", ""), fields.pop("qst_kanton", "")
    if tarif:
        fields["qst"] = {"code": str(tarif).upper(), **({"kanton": str(kanton).upper()} if kanton else {})}
    elif kanton:
        raise ValueError("qst_kanton nur zusammen mit qst_tarif")
    return fields


def employee_details(nummer: str) -> dict:
    """Alles zu einer Person: Stammdaten, Lohn, Sozialversicherungen, Quellensteuer, Notizen, abgelegte Dokumente.

    Args:
        nummer: z.B. "M0001".
    """
    return _call(api.employee_show, nummer)


def add_employee(vorname: str, nachname: str, strasse: str = "", nr: str = "", plz: str = "", ort: str = "",
                 land: str = "", ahv_nr: str = "", geburtsdatum: str = "", geschlecht: str = "", eintritt: str = "",
                 lohnart: str = "", monatslohn: str = "", pensum: str = "", stundenlohn: str = "",
                 standard_stunden: str = "", vollzeit_stunden_woche: str = "", ferienzuschlag_satz: str = "",
                 ferien_inbegriffen: bool = False, bvg_betrag: str = "", ag_bvg_betrag: str = "",
                 kinderzulagen: str = "", aufenthalt: str = "", qst_pflicht: str = "", qst_tarif: str = "",
                 qst_kanton: str = "", qst_satz: str = "", iban: str = "", notizen: str = "") -> dict:
    """Mitarbeiter:in für den Lohn erfassen (vorher employees() prüfen, nie doppelt). Nur Angaben übernehmen, die
    vorliegen; was kein eigenes Feld hat (Probezeit, Kündigungsfrist, Ferienanspruch, 13. Monatslohn, Spesen,
    Funktion …), in notizen.

    Args:
        vorname: Vorname.
        nachname: Nachname.
        strasse: Strasse ohne Nummer.
        nr: Hausnummer.
        plz: PLZ.
        ort: Ort.
        land: CH (Standard), DE, FR, IT, AT, LI …
        ahv_nr: 756.xxxx.xxxx.xx
        geburtsdatum: JJJJ-MM-TT.
        geschlecht: "w" oder "m" (für die Lohnmeldungen).
        eintritt: Eintrittsdatum JJJJ-MM-TT.
        lohnart: "monat" (Standard) oder "stunde".
        monatslohn: Monatslohn bei 100 % in CHF (lohnart monat), z.B. "5200".
        pensum: Pensum in %, z.B. "80" (Standard 100).
        stundenlohn: CHF pro Stunde (lohnart stunde).
        standard_stunden: Stunden pro Monat als Vorgabe (lohnart stunde).
        vollzeit_stunden_woche: Vollzeit-Wochenstunden des Betriebs (Standard 42).
        ferienzuschlag_satz: Ferienentschädigung als Anteil, z.B. "0.1064" für 10.64 % (lohnart stunde).
        ferien_inbegriffen: true, wenn die Ferienentschädigung im Stundenlohn schon enthalten ist.
        bvg_betrag: BVG-Abzug Arbeitnehmer CHF/Monat.
        ag_bvg_betrag: BVG Arbeitgeber CHF/Monat, nur wenn abweichend.
        kinderzulagen: Kinder-/Ausbildungszulagen CHF/Monat.
        aufenthalt: CH, C, B, L, G oder andere.
        qst_pflicht: auto (aus Aufenthalt, Standard), ja oder nein.
        qst_tarif: Quellensteuer-Tarifcode, z.B. "A0N" (Tarif, Kinder, Kirche Y/N); leer = keiner.
        qst_kanton: Kanton der Quellensteuer, nur wenn abweichend vom Wohnort.
        qst_satz: Satz vereinfachtes Verfahren als Anteil statt Tarif, z.B. "0.05".
        iban: Lohnkonto.
        notizen: alles Weitere zum Arbeitsverhältnis (Markdown).
    """
    loc = locals()
    try:
        fields = _employee_fields({k: loc[k] for k in _EMPLOYEE_ARGS})
    except ValueError as exc:
        return {"ok": False, "fehler": str(exc)}
    if ferien_inbegriffen:
        fields["ferien_inbegriffen"] = True
    return _call(api.employee_add, vorname, nachname, **fields)


def update_employee(nummer: str, felder: dict) -> dict:
    """Angaben einer Person ändern; nur die genannten Felder. Abgeschlossene Lohnabrechnungen bleiben unverändert.

    Args:
        nummer: z.B. "M0001".
        felder: wie add_employee (vorname, nachname, strasse, nr, plz, ort, land, ahv_nr, geburtsdatum, geschlecht,
            eintritt, lohnart, monatslohn, pensum, stundenlohn, standard_stunden, vollzeit_stunden_woche,
            ferienzuschlag_satz, ferien_inbegriffen, bvg_betrag, ag_bvg_betrag, kinderzulagen, aufenthalt,
            qst_pflicht, qst_tarif, qst_kanton, qst_satz, iban, notizen) plus austritt (JJJJ-MM-TT, "" = keiner)
            und aktiv (false = nicht mehr im Lohnlauf). notizen ersetzt den ganzen Text: zuerst employee_details lesen.
    """
    felder = dict(felder or {})
    if "qst_tarif" in felder or "qst_kanton" in felder:
        tarif, kanton = felder.pop("qst_tarif", ""), felder.pop("qst_kanton", "")
        if tarif:
            felder["qst"] = {"code": str(tarif).upper(), **({"kanton": str(kanton).upper()} if kanton else {})}
        elif kanton:
            return {"ok": False, "fehler": "qst_kanton nur zusammen mit qst_tarif"}
        else:
            felder["qst"] = None
    return _call(api.employee_update, nummer, **felder)


def file_employee_document(mitarbeiter: str, datei: str) -> dict:
    """Ein Dokument bei der Person ablegen (Arbeitsvertrag, Bewilligung, Zeugnis, Diplom). Aus der Inbox wird die
    Datei verschoben, ein Anhang aus dem Chat kopiert.

    Args:
        mitarbeiter: z.B. "M0001".
        datei: Pfad relativ zum Buch, z.B. "inbox/vertrag.pdf" oder der Pfad eines Anhangs.
    """
    try:
        b = book()
        path = _source_file(b, datei)
    except (BookError, ValueError) as exc:
        return {"ok": False, "fehler": str(exc)}
    out = _call(api.employee_document_add, mitarbeiter, path.name, path.read_bytes())
    if out.get("ok", True) is not False and b.rel(path).startswith("inbox/"):
        removed = _call(api.inbox_remove, path.name)
        if removed.get("ok") is False:
            out["hinweis"] = f"Kopie abgelegt; die Datei bleibt in der Inbox: {removed['fehler']}"
    return out


def payslip_inputs(monat: str, mitarbeiter: str, eingaben: dict) -> dict:
    """Eingaben einer Lohnabrechnung im Entwurf ändern und neu rechnen.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001".
        eingaben: stunden, bvg, kinderzulagen, korrektur, korrektur_text, qst_satzbestimmend, qst_gesamtpensum.
    """
    return _call(api.payslip_inputs, monat, mitarbeiter, eingaben)


def reopen_payslip(monat: str, mitarbeiter: str) -> dict:
    """Abgeschlossene Lohnabrechnung wieder öffnen (nimmt sie aus der Sammelbuchung), z.B. für eine Korrektur.

    Args:
        monat: JJJJ-MM.
        mitarbeiter: z.B. "M0001".
    """
    return _call(api.payslip_reopen, monat, mitarbeiter)


def cancel_payroll_payment(monat: str) -> dict:
    """Die Lohnzahlungsdatei eines Monats zurücknehmen (wenn sie nicht an die Bank ging).

    Args:
        monat: JJJJ-MM.
    """
    return _call(api.payroll_payment_cancel, monat)


def payroll_settings(saetze_an: dict | None = None, saetze_ag: dict | None = None, ag_bvg: str = "",
                     konten: dict | None = None) -> dict:
    """Lohneinstellungen ändern: Beitragssätze (als Anteil, 0.053 = 5.3 %), BVG Arbeitgeber, Lohnkonten.

    Args:
        saetze_an: Arbeitnehmer-Sätze je Code, z.B. {"ahv": "0.053", "nbu": "0.012", "ktg": "0.005"}.
        saetze_ag: Arbeitgeber-Sätze je Code, z.B. {"fak": "0.012", "vk": "0.003", "bu": "0.008"}.
        ag_bvg: "gleich_an" oder "betrag".
        konten: Konten je Rolle, z.B. {"lohnaufwand": "5000"}.
    """
    return _call(api.payroll_config_update, saetze_an, saetze_ag, ag_bvg or None, None, konten)


def add_expense(mitarbeiter: str, datum: str, text: str, betrag: str, konto: str = "", mwst: str = "",
                art: str = "uebrige", datei: str = "") -> dict:
    """Spesen, die eine Person ausgelegt hat: auf 2210 gebucht, mit dem nächsten Lohn ausbezahlt.

    Args:
        mitarbeiter: z.B. "M0001".
        datum: JJJJ-MM-TT.
        text: wofür.
        betrag: CHF brutto, z.B. "48.60".
        konto: Aufwandkonto; leer = Standard.
        mwst: MWST-Code bei effektiver Methode.
        art: "uebrige" oder "reise" (Reise, Verpflegung, Übernachtung; für den Lohnausweis).
        datei: Quittung in inbox/ (optional).
    """
    return _call(api.expense_add, mitarbeiter, datum, text, betrag, konto, mwst, None, art, datei)


def remove_expense(nummer: str) -> dict:
    """Spesenbeleg zurücknehmen, solange er nicht ausbezahlt ist.

    Args:
        nummer: Nummer des Spesenbelegs.
    """
    return _call(api.expense_remove, nummer)


# ---------- Konten und Einstellungen ----------

def add_account(nr: str, name: str, klasse: str = "", gruppe: str = "", waehrung: str = "") -> dict:
    """Neues Konto im Kontenplan (zuerst accounts() durchsuchen, ob es schon eines gibt).

    Args:
        nr: Kontonummer, z.B. "6570".
        name: Kontoname.
        klasse: aktiv, passiv, aufwand oder ertrag; leer = aus der Nummer.
        gruppe: Gruppe für die Jahresrechnung; leer = aus der Nummer.
        waehrung: nur für Fremdwährungskonten, z.B. "EUR".
    """
    return _call(api.add_account, nr, name, klasse, gruppe, waehrung)


def update_account(nr: str, name: str = "", gruppe: str = "", aktiv: bool | None = None, abschluss: str | None = None) -> dict:
    """Konto ändern: Name, Gruppe, aktiv/inaktiv, Abschluss in ein Eigenkapitalkonto (Privat → 2800).

    Args:
        nr: Kontonummer.
        name: neuer Name.
        gruppe: neue Gruppe.
        aktiv: false = ausblenden.
        abschluss: Eigenkapitalkonto, in das es bei der Eröffnung schliesst; "" = keines.
    """
    return _call(api.account_update, nr, name=name or None, gruppe=gruppe or None, aktiv=aktiv, abschluss=abschluss)


def update_settings(firma: str = "", uid: str = "", telefon: str = "", email: str = "", iban: str = "",
                    zahlungs_iban: str = "", zahlungsfrist_tage: int | None = None, co: str = "",
                    adresse: dict | None = None, konten: dict | None = None, mwst: dict | None = None,
                    web: str = "", rechnung: dict | None = None) -> dict:
    """Einstellungen der Firma ändern. Den Modus des Agenten (vorschlag/direkt) stellt nur ein Mensch um.

    Args:
        firma: Firmenname.
        uid: UID / MWST-Nummer.
        telefon: Telefon.
        email: E-Mail.
        iban: IBAN für Rechnungen (QR-Rechnung).
        zahlungs_iban: IBAN, von der Zahlungen ausgehen.
        zahlungsfrist_tage: Zahlungsfrist auf Rechnungen.
        co: Zusatzzeile der Adresse.
        adresse: {"strasse", "nr", "plz", "ort", "land"}.
        konten: Konten je Rolle, z.B. {"bank": "1020", "debitoren": "1100"}.
        mwst: {"methode": keine|effektiv|saldo, "periode": quartal|semester, "saldosteuersatz": "6.2"}.
        web: Website (steht im Briefkopf bzw. in der Fusszeile).
        rechnung: Brief-Layout von Rechnung und Offerte: {"layout": "brief"|"kompakt", "ort", "einleitung",
            "offerte_einleitung", "dank", "unterzeichner", "funktion", "bank" (Name der Bank), "logo_hoehe_mm"};
            nur genannte Felder ändern sich, "" entfernt eines.
    """
    loc = locals()
    fields = {k: loc[k] for k in ("firma", "uid", "telefon", "email", "iban", "zahlungs_iban", "co", "web") if loc[k]}
    if zahlungsfrist_tage is not None:
        fields["zahlungsfrist_tage"] = zahlungsfrist_tage
    return _call(api.settings_update, adresse, konten, mwst, rechnung=rechnung, **fields)


def plugins_overview() -> list | dict:
    """Verfügbare Plugins und welche dieses Buch eingeschaltet hat."""
    return _call(api.plugin_list)


def enable_plugin(name: str) -> dict:
    """Ein installiertes Plugin für dieses Buch einschalten.

    Args:
        name: Name aus plugins_overview, z.B. "leistungen".
    """
    return _call(api.plugin_enable, name)


def disable_plugin(name: str) -> dict:
    """Ein Plugin für dieses Buch ausschalten (seine Daten bleiben).

    Args:
        name: Name aus plugins_overview.
    """
    return _call(api.plugin_disable, name)


# ---------- Kunden, Rechnungen, Lieferanten ----------

def renumber_customer(nummer: str, neu: str) -> dict:
    """Kundennummer ändern; nur solange nichts im Buch auf den Kunden verweist.

    Args:
        nummer: heutige Nummer, z.B. "K0004".
        neu: neue Nummer, z.B. "K0001".
    """
    return _call(api.customer_renumber, nummer, neu)


def complete_follow_up(kunde: str, index: int, erledigt: bool = True) -> dict:
    """Einen Nachfass-Eintrag des Kundenverlaufs als erledigt markieren (Index aus customer_details/follow_ups).

    Args:
        kunde: Kundennummer.
        index: Index des Eintrags im Verlauf.
        erledigt: false = wieder offen.
    """
    return _call(api.customer_log_done, kunde, index, erledigt)


def invoice_details(nummer: str) -> dict:
    """Eine Rechnung mit Positionen, Zahlungen und Mahnungen.

    Args:
        nummer: Rechnungsnummer.
    """
    return _call(api.invoice_show, nummer)


def reminders() -> list | dict:
    """Überfällige Rechnungen mit den bisherigen Mahnungen und der nächsten Stufe."""
    return _call(api.reminders)


def create_reminder(nummern: list[str], datum: str = "", frist_tage: int | None = None) -> dict:
    """Nächste Mahnung (PDF mit QR-Zahlteil) je Rechnung erstellen; verschickt nichts.

    Args:
        nummern: Rechnungsnummern.
        datum: Mahndatum JJJJ-MM-TT; leer = heute.
        frist_tage: neue Zahlungsfrist in Tagen; leer = Standard.
    """
    return _call(api.reminder_create, nummern, datum or None, frist_tage)


def record_external_invoice(kunde: str, betrag: str, datum: str = "", rechnungsnr: str = "", text: str = "",
                            konto: str = "", mwst: str = "", faellig: str = "", datei: str = "") -> dict:
    """Eine ausserhalb von aeradex geschriebene eigene Rechnung als offenen Posten erfassen (Debitoren an Ertrag).

    Args:
        kunde: Kundennummer.
        betrag: Total CHF.
        datum: Rechnungsdatum JJJJ-MM-TT.
        rechnungsnr: Nummer auf der Rechnung.
        text: Bezeichnung.
        konto: Ertragskonto; leer = Standard.
        mwst: MWST-Code.
        faellig: Fälligkeit JJJJ-MM-TT.
        datei: die Rechnung in inbox/ (optional).
    """
    loc = locals()
    fields = {k: loc[k] for k in ("datum", "rechnungsnr", "text", "konto", "mwst", "faellig", "datei") if loc[k]}
    return _call(api.invoice_external, kunde, betrag, **fields)


def update_supplier(nummer: str, felder: dict) -> dict:
    """Lieferant ändern: name, strasse, nr, plz, ort, land, iban, konto, mwst, notizen.

    Args:
        nummer: Lieferantennummer.
        felder: nur die zu ändernden Felder.
    """
    felder = dict(felder or {})
    return _call(api.supplier_update, nummer, felder.pop("notizen", None), **felder)


def pay_supplier_bill(nummer: str, datum: str = "", betrag: str = "", konto: str = "") -> dict:
    """Zahlung einer Lieferantenrechnung verbuchen (ohne Zahlungslauf, z.B. per E-Banking oder bar bezahlt).

    Args:
        nummer: Kreditorennummer.
        datum: Zahldatum; leer = heute.
        betrag: leer = offener Betrag.
        konto: Zahlkonto; leer = Bank.
    """
    return _call(api.bill_pay, nummer, datum or None, betrag or None, konto or None)


def void_supplier_bill(nummer: str, grund: str) -> dict:
    """Lieferantenrechnung stornieren (falsch erfasst, Gutschrift erhalten).

    Args:
        nummer: Kreditorennummer.
        grund: warum.
    """
    return _call(api.bill_void, nummer, grund)


def book_payment_run(datei: str, datum: str = "") -> dict:
    """Einen ausgeführten Zahlungslauf (pain.001) als bezahlt verbuchen.

    Args:
        datei: die Zahlungsdatei aus create_payment_run.
        datum: Ausführungsdatum; leer = das in der Datei.
    """
    return _call(api.payment_run_book, datei, datum or None)


def payables() -> dict:
    """Offene Lieferantenrechnungen (Kreditoren) mit Fälligkeit."""
    return _call(api.payables)


# ---------- Belegeingang, Inbox, Bank ----------

def book_bill_draft(id: str) -> dict:
    """Einen bereiten Beleg-Entwurf so buchen, wie er steht (nur agent_modus: direkt; sonst bucht ein Mensch).

    Args:
        id: Entwurf, z.B. "ENT-0001".
    """
    return _direct(api.bill_draft_approve, id)


def discard_bill_draft(id: str) -> dict:
    """Beleg-Entwurf verwerfen (Duplikat, kein Beleg); die Datei bleibt in der Inbox.

    Args:
        id: Entwurf.
    """
    return _call(api.bill_draft_discard, id)


def remove_inbox_file(datei: str) -> dict:
    """Datei aus der Inbox löschen (falsch hochgeladen, doppelt). Nur auf Wunsch des Menschen.

    Args:
        datei: Dateiname oder inbox/…
    """
    return _call(api.inbox_remove, datei)


def attach_receipt(beleg: str, datei: str) -> dict:
    """Eine Quittung aus der Inbox an eine schon gebuchte Buchung hängen.

    Args:
        beleg: Belegnummer, z.B. "27-001".
        datei: z.B. "inbox/quittung.pdf".
    """
    return _call(api.attach_receipt, beleg, datei)


def import_bank_statement(datei: str) -> dict:
    """Kontoauszug camt.053 (oder ein bestätigtes Format) aus der Inbox importieren.

    Args:
        datei: z.B. "inbox/auszug.xml".
    """
    return _call(api.bank_import, datei)


def link_bank_transaction(id: str, beleg: str) -> dict:
    """Bankbewegung mit einer schon gebuchten Buchung abgleichen (nichts wird neu gebucht).

    Args:
        id: ID der Bankbewegung.
        beleg: Belegnummer.
    """
    return _call(api.bank_link, id, beleg)


def ignore_bank_transaction(id: str, grund: str) -> dict:
    """Bankbewegung als erledigt ohne Buchung markieren (z.B. schon anders erfasst).

    Args:
        id: ID der Bankbewegung.
        grund: warum.
    """
    return _call(api.bank_ignore, id, grund)


def unlink_bank_transaction(id: str) -> dict:
    """Abgleich oder Ignorieren einer Bankbewegung zurücknehmen (eine Buchung daraus bleibt, ggf. stornieren).

    Args:
        id: ID der Bankbewegung.
    """
    return _call(api.bank_unlink, id)


def add_bank_rule(konto: str, gegenpartei: str = "", text: str = "", betrag: str = "", richtung: str = "",
                  mwst: str = "", buchungstext: str = "", anwenden: bool = True) -> dict:
    """Bankregel: erkannte Bewegungen beim Import gegen ein Konto buchen (z.B. Swisscom → 6510).

    Args:
        konto: Gegenkonto.
        gegenpartei: Teil des Namens der Gegenpartei.
        text: Teil des Bankbuchungstexts.
        betrag: nur bei genau diesem Betrag.
        richtung: "ein" oder "aus"; leer = beide.
        mwst: MWST-Code.
        buchungstext: Text der Buchung.
        anwenden: true = auch die offenen Bewegungen jetzt.
    """
    return _call(api.bank_rule_add, konto, gegenpartei, text, betrag or None, richtung, mwst, buchungstext, anwenden)


def remove_bank_rule(regel: str) -> dict:
    """Bankregel löschen.

    Args:
        regel: ID aus bank_rules.
    """
    return _call(api.bank_rule_remove, regel)


def bank_reconciliation() -> list | dict:
    """Abstimmung je Bankkonto: Saldo laut Bank und laut Buchhaltung, Differenz."""
    return _call(api.bank_reconciliation)


# ---------- Journal, MWST, Abschluss, Budget ----------

def reject_proposals(ids: list[str]) -> dict:
    """Buchungsvorschläge verwerfen.

    Args:
        ids: IDs aus list_proposals.
    """
    return _call(api.reject, ids)


def amend_entry(beleg: str, datum: str, text: str, zeilen: list[dict]) -> dict:
    """Eine von Hand erfasste Buchung an Ort korrigieren (gleiche Nummer; nur agent_modus: direkt).

    Args:
        beleg: Belegnummer.
        datum: JJJJ-MM-TT.
        text: Buchungstext.
        zeilen: [{"soll": "6500", "haben": "1020", "betrag": "48.60", "mwst": ""}].
    """
    return _direct(api.amend_entry, beleg, datum, text, zeilen)


def book_mwst(periode: str) -> dict:
    """MWST-Abrechnung einer Periode verbuchen (Umbuchung auf die Abrechnungsschuld; nur agent_modus: direkt).

    Args:
        periode: z.B. "2026-Q3" oder "2026-S2".
    """
    return _direct(api.mwst_book, periode)


def mark_closing_step(jahr: int, schritt: str, erledigt: bool = True, notiz: str = "") -> dict:
    """Einen Schritt der Abschluss-Checkliste abhaken (Schlüssel aus closing_checklist).

    Args:
        jahr: Geschäftsjahr.
        schritt: Schlüssel des Schritts.
        erledigt: false = wieder offen.
        notiz: optional.
    """
    from .gitlog import AUTHOR
    return _call(api.closing_mark, jahr, schritt, erledigt, notiz, AUTHOR.get() or "Agent")


def book_closing_entry(jahr: int, art: str, wert: str = "", positionen: list[dict] | None = None, notiz: str = "") -> dict:
    """Abschlussbuchung per 31.12. (arbeiten, material, delkredere, ferien, privatanteile) nach closing_entry;
    nochmals buchen ersetzt sie (nur agent_modus: direkt).

    Args:
        jahr: Geschäftsjahr.
        art: arbeiten, material, delkredere, ferien oder privatanteile.
        wert: Bestand CHF (für die Bestände).
        positionen: Einzelposten wie closing_entry sie zeigt.
        notiz: Grundlage (Inventar vom …).
    """
    from .gitlog import AUTHOR
    return _direct(api.closing_entry_book, jahr, art, positionen, wert or None, notiz, AUTHOR.get() or "Agent")


def open_year(jahr: int | None = None) -> dict:
    """Das nächste Geschäftsjahr eröffnen (Saldovorträge).

    Args:
        jahr: das zu eröffnende Jahr; leer = das nächste.
    """
    return _call(api.year_open, jahr)


def set_budget(jahr: int, konto: str, betrag: str = "", monate: list[str] | None = None) -> dict:
    """Budget eines Kontos setzen: Jahresbetrag (gleichmässig verteilt) oder 12 Monatswerte (nur agent_modus: direkt).

    Args:
        jahr: Budgetjahr.
        konto: Erfolgskonto.
        betrag: Jahresbetrag CHF.
        monate: 12 Beträge Jan–Dez statt betrag.
    """
    return _direct(api.budget_set, jahr, konto, betrag or None, monate)


def save_notes_to_statements(jahr: int, text: str) -> dict:
    """Text des Anhangs zur Jahresrechnung speichern (Markdown).

    Args:
        jahr: Geschäftsjahr.
        text: Anhang.
    """
    return _call(api.anhang_save, jahr, text)


# ---------- chat only: files the person attached ----------

ATTACHMENTS = Path(".aeradex") / "lokal" / "chat"


def _source_file(b: Book, datei: str) -> Path:
    """A file the agent may read: inbox/, belege/ or an attachment from the chat."""
    path = (b.root / (datei or "")).resolve()
    rel = path.relative_to(b.root) if path.is_relative_to(b.root) else None
    if rel is None or not path.is_file() or not (rel.parts[:1] in (("inbox",), ("belege",))
                                                  or rel.parts[:3] == ATTACHMENTS.parts):
        raise ValueError(f"{datei} ist keine Datei in inbox/, belege/ oder den Anhängen")
    return path


def attachment_to_inbox(datei: str) -> dict:
    """Einen Anhang aus dem Chat in die Inbox legen — für Belege (danach create_bill_draft), Kontoauszüge
    (import_bank_statement) oder attach_receipt.

    Args:
        datei: Pfad des Anhangs, wie in der Nachricht genannt.
    """
    try:
        b = book()
        path = _source_file(b, datei)
    except (BookError, ValueError) as exc:
        return {"ok": False, "fehler": str(exc)}
    return _call(api.inbox_add, path.name, path.read_bytes())


# ---------- chat only: let Claude look at a receipt ----------

def propose_journal_lines(ref: str, zeilen: list[dict], art: str = "beleg", datum: str = "", hinweis: str = "") -> dict:
    """Die Zeilen eines Belegs oder einer Kreditkartenabrechnung für das Journal-Raster erfassen (Auftrag «Beleg …
    für das Journal auslesen»). aeradex prüft Konten, MWST-Codes und dass jeder Betrag im Text steht. Bucht nichts:
    der Mensch wählt das Gegenkonto und bucht. Erneut aufrufen überschreibt.

    Args:
        ref: Kennung aus dem Auftrag.
        zeilen: [{"datum": "JJJJ-MM-TT", "text": "Coop Thun", "konto": "4000", "betrag": "48.60", "mwst": ""}];
            betrag in CHF wie auf dem Beleg, Gutschriften negativ.
        art: "beleg" oder "karte" (Kreditkartenabrechnung).
        datum: Datum des Belegs bzw. der Abrechnung JJJJ-MM-TT.
        hinweis: optional, etwas, das der Mensch wissen sollte (ein Satz).
    """
    return _call(api.journal_lines_propose, ref, zeilen, art, datum, hinweis)


def read_inbox_file(datei: str) -> list[dict] | str:
    """Liest eine Datei aus inbox/, belege/ oder einen Anhang aus dem Chat (PDF, Bild, Word oder Text), damit du
    den Inhalt siehst.

    Args:
        datei: Pfad relativ zum Buch, z.B. "inbox/quittung.pdf".
    """
    try:
        path = _source_file(book(), datei)
    except (BookError, ValueError) as exc:
        return f"Fehler: {exc}"
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
    if suffix == ".docx":
        return _docx_text(data)[:40000] or f"Fehler: in {datei} steht kein Text"
    if suffix in (".doc", ".xls", ".xlsx", ".odt", ".pages", ".heic", ".zip"):
        return (f"Fehler: {suffix} kann ich nicht lesen — bitte als PDF, Bild oder Word (.docx). Kontoauszüge (CSV/Excel): "
                "attachment_to_inbox, dann bank_file_preview")
    return data.decode("utf-8", errors="replace")[:20000]


def _docx_text(data: bytes) -> str:
    """The text of a Word file, paragraph by paragraph (tables cell by cell), without any extra package."""
    import io
    import re
    import zipfile
    from xml.etree import ElementTree
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return ""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    lines = []
    for para in root.iter(f"{w}p"):
        text = "".join(t.text or "" if t.tag == f"{w}t" else "\t" if t.tag == f"{w}tab" else ""
                       for t in para.iter() if t.tag in (f"{w}t", f"{w}tab"))
        lines.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------- Berichte ----------

_REPORT_KEYS = ("jahr", "periode", "von", "bis", "spalten", "vergleich", "stichtag", "nach")


def period_report(typ: str = "", jahr: int | None = None, periode: str = "", von: str = "", bis: str = "",
                  spalten: str = "", vergleich: str = "", stichtag: str = "", nach: str = "", vorlage: str = "") -> dict:
    """Bericht für jede Periode (die Engine rechnet, nie selbst rechnen): erfolgsrechnung, bilanz, geldfluss,
    kennzahlen, debitoren, kreditoren, umsatz (+ Plugin-Berichte, siehe report_catalog). Mit Kommentar, falls gespeichert.

    Args:
        typ: Berichtsname (leer, wenn vorlage gesetzt).
        jahr: Geschäftsjahr; leer = aktuelles.
        periode: jahr, q1–q4, h1, h2, 01–12.
        von: JJJJ-MM-TT statt periode.
        bis: JJJJ-MM-TT statt periode.
        spalten: gesamt, monat oder quartal.
        vergleich: keine, vorperiode, vorjahr oder budget (Spalten Abweichung und Abw. %).
        stichtag: JJJJ-MM-TT für Bilanz-/Altersberichte.
        nach: umsatz: kunde, konto oder monat.
        vorlage: Name einer gespeicherten Berichtsvorlage.
    """
    loc = locals()
    return _call(api.report, typ, vorlage, **{k: loc[k] for k in _REPORT_KEYS if loc[k]})


def report_catalog() -> dict:
    """Verfügbare Berichte (inkl. Plugins) und gespeicherte Berichtsvorlagen."""
    return _call(api.report_list)


def save_report_comment(text: str, typ: str = "", jahr: int | None = None, periode: str = "", von: str = "",
                        bis: str = "", spalten: str = "", vergleich: str = "", stichtag: str = "", nach: str = "",
                        vorlage: str = "") -> dict:
    """Kommentar zu einem Bericht speichern (dieselben Parameter wie period_report). Nur Zahlen aus dem Bericht
    zitieren; ändern sich die Zahlen später, wird der Kommentar als veraltet markiert.

    Args:
        text: der Kommentar (3–6 Sätze).
        typ: Berichtsname.
        jahr: wie period_report.
        periode: wie period_report.
        von: wie period_report.
        bis: wie period_report.
        spalten: wie period_report.
        vergleich: wie period_report.
        stichtag: wie period_report.
        nach: wie period_report.
        vorlage: wie period_report.
    """
    from .gitlog import AUTHOR
    loc = locals()
    return _call(api.report_comment_save, text, typ, vorlage, AUTHOR.get() or "Agent",
                 **{k: loc[k] for k in _REPORT_KEYS if loc[k]})


def budget_overview(jahr: int | None = None) -> dict:
    """Budget eines Jahres pro Erfolgskonto (12 Monate, Jahr, Ist Vorjahr).

    Args:
        jahr: Budgetjahr; leer = aktuelles.
    """
    from datetime import date
    return _call(api.budget_show, jahr or date.today().year)


def budget_from_prior_year(jahr: int, prozent: str = "0") -> dict:
    """Budget aus dem Ist des Vorjahres (+ Prozent, Saisonverlauf bleibt). Im agent_modus 'vorschlag' nicht
    erlaubt — dann dem Menschen die Zahlen vorschlagen.

    Args:
        jahr: Budgetjahr.
        prozent: Veränderung gegenüber Vorjahr, z.B. "5".
    """
    try:
        mode = book().settings.get("agent_modus") or "vorschlag"
    except BookError as exc:
        return {"ok": False, "fehler": str(exc)}
    if mode != "direkt":
        return {"ok": False, "fehler": "agent_modus ist 'vorschlag': das Budget setzt ein Mensch (Berichte → Budget "
                "oder `aeradex budget vorjahr`). Schlage die Zahlen im Text vor."}
    return _call(api.budget_from_prior, jahr, prozent)

# Order matters for prompt caching: a stable list keeps the cached prefix valid.
SHARED = [status, check, accounts, balance, ledger, journal, report, history, list_inbox, mwst_report,
          mwst_reconciliation,
          propose_booking, list_proposals, book_entry, book_split, approve_proposals, reverse_entry, recode_preview, recode,
          accrual_proposals, book_accruals,
          exchange_rate, revaluation_preview,
          bill_drafts, bill_draft, create_bill_draft, complete_bill_draft, overdue_invoices, bank_rules, expenses,
          customers, customer_details, add_customer, update_customer, log_customer, follow_ups, invoices, create_invoice, open_akonto, match_payment, pay_invoice, credit_invoice,
          void_invoice, receivables, suppliers, add_supplier, scan_qr_bill, add_supplier_bill, supplier_bills,
          create_payment_run, bank_transactions, bank_suggestions, assign_bank_transaction, propose_bank_booking, suggest_bank_accounts,
          book_bank_transaction, transfer_bank_transaction, employees, payroll_run, payroll_payment_export, qst_sync, payslip, close_payroll, close_payslip, lohnausweis,
          payroll_account, wage_declarations,
          bank_file_preview, propose_bank_format, card_statement_text, propose_card_statement,
          closing_documents, mwst_details,
          period_report, report_catalog, save_report_comment, budget_overview, budget_from_prior_year,
          propose_journal_lines, closing_checklist, closing_entry, business_years, opening_balance, booking_history,
          employee_details, add_employee, update_employee, file_employee_document, payslip_inputs, reopen_payslip,
          cancel_payroll_payment, payroll_settings, add_expense, remove_expense,
          add_account, update_account, update_settings, plugins_overview, enable_plugin, disable_plugin,
          renumber_customer, complete_follow_up, invoice_details, reminders, create_reminder, record_external_invoice,
          update_supplier, pay_supplier_bill, void_supplier_bill, book_payment_run, payables,
          book_bill_draft, discard_bill_draft, remove_inbox_file, attach_receipt, import_bank_statement,
          link_bank_transaction, ignore_bank_transaction, unlink_bank_transaction, add_bank_rule, remove_bank_rule,
          bank_reconciliation, reject_proposals, amend_entry, book_mwst, mark_closing_step, book_closing_entry,
          open_year, set_budget, save_notes_to_statements, attachment_to_inbox]
CHAT_ONLY = [read_inbox_file]

# Public names for plugin tools: wrap an api function so errors come back as {"ok": False, …}.
call = _call
direct = _direct


def _plugin_book(root: Path | None) -> Book | None:
    try:
        return Book(root) if root else book()
    except BookError:
        return None


def shared_for(root: Path | None = None) -> list:
    """The built-in tools plus those of the plugins the book enables."""
    from . import plugins
    names = {fn.__name__ for fn in SHARED + CHAT_ONLY}
    extra = [fn for fn in plugins.agent_tools(_plugin_book(root)) if fn.__name__ not in names]
    return SHARED + extra


def instructions_for(root: Path | None = None) -> str:
    from . import plugins
    more = plugins.agent_instructions(_plugin_book(root))
    return INSTRUCTIONS + (f"\nPlugins dieses Buchs:\n{more}\n" if more else "")

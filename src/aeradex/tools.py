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
  bestehenden Kunden mit update_customer ergänzen, nie einen zweiten anlegen.
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
    return _call(api.dossier, jahr, teile, formate)


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
    return _call(api.mwst_abstimmung, jahr, als_pdf=als_pdf)


# ---------- debitoren ----------

def customers() -> list | dict:
    """Kundenliste."""
    return _call(api.customer_list)


def customer_details(nummer: str) -> dict:
    """Alle Angaben eines Kunden: Firma, Kontaktperson, Adresse, E-Mail, Rechnungsempfänger, Stundensatz, Notizen.

    Args:
        nummer: Kundennummer, z.B. "K0004".
    """
    from . import invoices as inv

    def get(b):
        c = inv.customer(b, nummer)
        return api.jsonable({**{k: v for k, v in c.items() if not k.startswith("_")}, "notizen": c.get("_notizen", "")})
    return _call(get)


def _same_customer(book: Book, name: str, firma: str) -> list[str]:
    """Existing customers with the same company (or, for private persons, the same name)."""
    from . import invoices as inv

    def norm(x) -> str:
        return " ".join(str(x or "").lower().replace(".", " ").split())
    key = norm(firma) or norm(name)
    return [f"{k} {c.get('firma') or c.get('name')}" for k, c in inv.customers(book).items()
            if key and key in (norm(c.get("firma")) or norm(c.get("name")), norm(c.get("name")))]


def add_customer(name: str, firma: str = "", strasse: str = "", nr: str = "", plz: str = "", ort: str = "",
                 land: str = "CH", email: str = "", rechnung_an: str = "firma", trotzdem_neu: bool = False) -> dict:
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
        rechnung_an: "firma" oder "person".
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
                 land=land, email=email, rechnung_an=rechnung_an)


def update_customer(nummer: str, name: str = "", firma: str = "", strasse: str = "", nr: str = "", plz: str = "",
                    ort: str = "", land: str = "", email: str = "", rechnung_an: str = "",
                    leeren: list[str] | None = None) -> dict:
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
        rechnung_an: "firma" oder "person".
        leeren: Felder, die entfernt werden sollen, z.B. ["name"] für eine Kontaktperson oder ["email"].
    """
    fields = {k: v for k, v in {"name": name, "firma": firma, "strasse": strasse, "nr": nr, "plz": plz, "ort": ort,
                                "land": land, "email": email, "rechnung_an": rechnung_an}.items() if str(v).strip()}
    clearable = {"name", "firma", "strasse", "nr", "plz", "ort", "email"}
    for key in leeren or []:
        if key not in clearable:
            return {"ok": False, "fehler": f"«{key}» kann nicht geleert werden (möglich: {', '.join(sorted(clearable))})"}
        fields[key] = ""
    if not fields:
        return {"ok": False, "fehler": "Keine Änderung angegeben"}
    return _call(api.customer_update, nummer, **fields)


def invoices(status: str = "") -> list | dict:
    """Rechnungen mit offenem Betrag.

    Args:
        status: offen, teilbezahlt, bezahlt, storniert; leer = alle.
    """
    return _call(api.invoice_list, status)


def create_invoice(kunde: str, positionen: list[dict], datum: str = "", text: str = "",
                   leistung_von: str = "", leistung_bis: str = "",
                   expected_revision: str | None = None, idempotency_key: str | None = None) -> dict:
    """QR-Rechnung ausstellen und verbuchen; erzeugt die PDF mit QR-Einzahlungsschein.

    Args:
        kunde: Kundennummer, z.B. "K0001".
        positionen: z.B. [{"text": "Beratung", "menge": 10, "einheit": "h", "preis": "150.00", "konto": "3400"}] (konto optional).
        datum: Rechnungsdatum JJJJ-MM-TT; leer = heute.
        text: Einleitungstext auf der Rechnung.
        leistung_von: Leistungsdatum bzw. Beginn des Leistungszeitraums JJJJ-MM-TT, wenn er vom Rechnungsdatum
            abweicht (MWST-Rechnungen müssen ihn nennen, Art. 26 MWSTG).
        leistung_bis: Ende des Leistungszeitraums JJJJ-MM-TT; leer bei einem einzelnen Tag.
        expected_revision: Stand aus status(); veraltete Änderungen werden zurückgewiesen.
        idempotency_key: Eindeutiger Schlüssel; bei Wiederholung desselben Aufrufs beibehalten.
    """
    return _call(api.invoice_create, kunde, positionen, datum or None, text, None, "", None,
                 leistung_von or None, leistung_bis or None,
                 expected_revision=expected_revision, idempotency_key=idempotency_key)


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
        art: leer = erkennen; sonst kreditor, quittung oder debitor.
    """
    return _call(api.bill_draft_create, datei, art)


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
          customers, customer_details, add_customer, update_customer, invoices, create_invoice, match_payment, pay_invoice, credit_invoice,
          void_invoice, receivables, suppliers, add_supplier, scan_qr_bill, add_supplier_bill, supplier_bills,
          create_payment_run, bank_transactions, bank_suggestions, assign_bank_transaction, propose_bank_booking, suggest_bank_accounts,
          book_bank_transaction, employees, payroll_run, payroll_payment_export, qst_sync, payslip, close_payroll, close_payslip, lohnausweis,
          bank_file_preview, propose_bank_format, card_statement_text, propose_card_statement,
          closing_documents, mwst_details,
          period_report, report_catalog, save_report_comment, budget_overview, budget_from_prior_year]
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

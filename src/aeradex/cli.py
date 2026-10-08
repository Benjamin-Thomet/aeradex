"""aeradex — command line. Every command can print JSON (--json) for agents."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import api
from .book import Book, BookError, find_root
from .files import FormatError


def _fmt(v) -> str:
    try:
        d = Decimal(str(v))
        return f"{abs(d) if not d else d:,.2f}".replace(",", "'")
    except Exception:
        return str(v)


def _table(rows: list[dict], cols: list[tuple[str, str]], right=()) -> str:
    if not rows:
        return "(keine)"
    cells = [[str(r.get(k, "") if r.get(k) is not None else "") for k, _ in cols] for r in rows]
    for row in cells:
        for i, (k, _) in enumerate(cols):
            if k in right and row[i]:
                row[i] = _fmt(row[i])
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (_, h) in enumerate(cols)]
    fmt = lambda vals: "  ".join(v.rjust(widths[i]) if cols[i][0] in right else v.ljust(widths[i])
                                 for i, v in enumerate(vals))
    return "\n".join([fmt([h for _, h in cols]), fmt(["-" * w for w in widths])] + [fmt(c) for c in cells])


def _positions(items: list[str]) -> list[dict]:
    """'Beratung;10;150' or 'Beratung;10 h;150;3400' → position dicts."""
    out = []
    for item in items:
        parts = [p.strip() for p in item.split(";")]
        if len(parts) < 3:
            raise BookError(f"Position '{item}': Format 'Text;Menge[ Einheit];Preis[;Konto]'")
        menge, _, einheit = parts[1].partition(" ")
        pos = {"text": parts[0], "menge": menge, "einheit": einheit, "preis": parts[2]}
        if len(parts) > 3 and parts[3]:
            pos["konto"] = parts[3]
        out.append(pos)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aeradex", description="Swiss bookkeeping your agent can run.")
    p.add_argument("--buch", help="Buchordner (Standard: aktueller Ordner oder $AERADEX_BUCH)")
    p.add_argument("--json", action="store_true", help="Ausgabe als JSON (für Agenten)")
    p.add_argument("--no-commit", action="store_true", help="Änderungen nicht in git committen")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="neues Buch anlegen")
    s.add_argument("ordner")
    s.add_argument("--firma", required=True)
    s.add_argument("--jahr", type=int)
    s.add_argument("--rechtsform", default="GmbH", help="AG, GmbH, Einzelfirma oder Verein (bestimmt den Kontenplan)")
    s.add_argument("--kontenplan", help="Vorlage statt der zur Rechtsform passenden: kmu, einzelfirma, verein, …")
    for f in ("uid", "strasse", "nr", "plz", "ort", "iban", "telefon", "email"):
        s.add_argument(f"--{f}", default="")
    s.add_argument("--ohne-git", action="store_true")

    sub.add_parser("status", help="Überblick: Liquidität, Ergebnis, offene Posten, To-dos")
    s = sub.add_parser("check", help="Buch prüfen")
    s.add_argument("--quiet", action="store_true", help="nur Fehler ausgeben (für git-Hook)")

    s = sub.add_parser("accounts", help="Kontenplan anzeigen/suchen")
    s.add_argument("suche", nargs="?", default="")
    s = sub.add_parser("account-add", help="Konto anlegen")
    s.add_argument("nr")
    s.add_argument("name")
    s.add_argument("--klasse", default="")
    s.add_argument("--gruppe", default="")
    s.add_argument("--waehrung", default="", help="Fremdwährung des Kontos (EUR, USD …), nur Bilanzkonten")

    s = sub.add_parser("kurs", help="BAZG-Tageskurs (CHF je Einheit)")
    s.add_argument("waehrung")
    s.add_argument("datum", nargs="?")
    s = sub.add_parser("bewertung", help="Fremdwährungskonten per Stichtag zum BAZG-Kurs bewerten")
    s.add_argument("stichtag", help="JJJJ-MM-TT, meist 31.12.")
    s.add_argument("--buchen", action="store_true", help="Kursdifferenzen buchen (sonst nur Vorschau)")

    s = sub.add_parser("balance", help="Saldenliste")
    s.add_argument("--jahr", type=int)
    s.add_argument("--periode", default="jahr", help="jahr, q1–q4, h1, h2, 01–12, JJJJ-MM-TT..JJJJ-MM-TT")
    s = sub.add_parser("ledger", help="Kontoblatt")
    s.add_argument("konto")
    s.add_argument("--jahr", type=int)
    s.add_argument("--pdf", nargs="?", const="", default=None, help="als PDF (optional Pfad)")
    s = sub.add_parser("journal", help="Journal anzeigen")
    s.add_argument("--jahr", type=int)
    s.add_argument("--monat", type=int)
    s.add_argument("--beleg", default="")
    s.add_argument("--konto", default="")
    s.add_argument("--suche", default="")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--pdf", nargs="?", const="", default=None)

    s = sub.add_parser("book", help="Buchung erfassen (Soll an Haben)")
    for f in ("datum", "soll", "haben", "betrag", "text"):
        s.add_argument(f"--{f}", required=True)
    s.add_argument("--beleg", default="")
    s.add_argument("--datei", help="Beleg-Datei (PDF/Bild); aus inbox/ wird sie verschoben")
    s.add_argument("--mwst", default="", help="MWST-Code (U81, V81, I81, …); Betrag ist dann brutto")
    s.add_argument("--waehrung", default="", help="Fremdwährung; Betrag ist dann in dieser Währung")
    s.add_argument("--kurs", default=None, help="Kurs CHF je Einheit (Standard: BAZG-Tageskurs)")
    s = sub.add_parser("book-split", help="Sammelbuchung aus JSON-Zeilen")
    s.add_argument("--datum", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--zeilen", required=True, help='JSON: [{"soll":"6500","betrag":"40"},{"haben":"1020","betrag":"40"}]')
    s.add_argument("--beleg", default="")
    s.add_argument("--datei")
    s = sub.add_parser("reverse", help="Beleg stornieren (Gegenbuchung)")
    s.add_argument("beleg")
    s.add_argument("--datum")
    s.add_argument("--text", default="")

    s = sub.add_parser("propose", help="Buchung vorschlagen (Agent → Mensch)")
    for f in ("datum", "soll", "haben", "betrag", "text"):
        s.add_argument(f"--{f}", required=True)
    s.add_argument("--begruendung", default="")
    s.add_argument("--datei", default="", help="Beleg-Datei; wird bei approve nach belege/ abgelegt")
    s.add_argument("--mwst", default="", help="MWST-Code, Betrag brutto")
    s.add_argument("--waehrung", default="", help="Fremdwährung; Betrag ist dann in dieser Währung")
    s.add_argument("--kurs", default=None, help="Kurs CHF je Einheit (Standard: BAZG-Tageskurs)")
    sub.add_parser("proposals", help="offene Vorschläge")
    s = sub.add_parser("approve", help="Vorschläge buchen")
    s.add_argument("ids", nargs="+", help="V-001 … oder 'alle'")
    s = sub.add_parser("reject", help="Vorschläge verwerfen")
    s.add_argument("ids", nargs="+")

    s = sub.add_parser("customer", help="Kunden")
    cs = s.add_subparsers(dest="sub", required=True)
    cs.add_parser("list")
    c = cs.add_parser("add")
    c.add_argument("--name", required=True)
    for f in ("firma", "strasse", "nr", "plz", "ort", "email"):
        c.add_argument(f"--{f}", default="")
    c.add_argument("--land", default="CH")
    c.add_argument("--rechnung-an", choices=["firma", "person"], default="firma")
    c.add_argument("--stundensatz")

    s = sub.add_parser("invoice", help="Rechnungen")
    ins = s.add_subparsers(dest="sub", required=True)
    c = ins.add_parser("list")
    c.add_argument("--status", default="", choices=["", "offen", "teilbezahlt", "bezahlt", "storniert"])
    c = ins.add_parser("show")
    c.add_argument("nr")
    c = ins.add_parser("create")
    c.add_argument("--kunde", required=True)
    c.add_argument("--pos", action="append", required=True, help="'Text;Menge[ Einheit];Preis[;Konto]', mehrfach")
    c.add_argument("--datum")
    c.add_argument("--text", default="")
    c.add_argument("--zahlungsfrist", type=int)
    c = ins.add_parser("void")
    c.add_argument("nr")
    c.add_argument("--grund", default="")
    for name in ("pay", "credit"):
        c = ins.add_parser(name)
        c.add_argument("nr")
        c.add_argument("--betrag")
        c.add_argument("--datum")
        c.add_argument("--konto")
        if name == "credit":
            c.add_argument("--grund", default="")
    c = ins.add_parser("match", help="offene Rechnung zu einer Gutschrift auf dem Bankkonto finden")
    c.add_argument("--betrag", required=True)
    c.add_argument("--text", default="")
    s = sub.add_parser("receivables", help="offene Debitoren")
    s.add_argument("--stichtag")
    s.add_argument("--pdf", nargs="?", const="", default=None)

    s = sub.add_parser("employee", help="Mitarbeitende")
    es = s.add_subparsers(dest="sub", required=True)
    es.add_parser("list")
    c = es.add_parser("add")
    c.add_argument("--vorname", required=True)
    c.add_argument("--nachname", required=True)
    for f in ("strasse", "nr", "plz", "ort", "ahv_nr", "geburtsdatum", "eintritt"):
        c.add_argument(f"--{f.replace('_', '-')}", dest=f, default="")
    c.add_argument("--lohnart", choices=["monat", "stunde"], default="monat")
    for f in ("monatslohn", "pensum", "stundenlohn", "standard_stunden", "bvg_betrag", "kinderzulagen",
              "ferienzuschlag_satz", "qst_satz"):
        c.add_argument(f"--{f.replace('_', '-')}", dest=f)
    c.add_argument("--qst-code", help="z.B. A0N (mit --qst-kanton/--qst-jahr)")
    c.add_argument("--qst-kanton")
    c.add_argument("--qst-jahr", type=int)

    s = sub.add_parser("payroll", help="Lohnlauf")
    ps = s.add_subparsers(dest="sub", required=True)
    c = ps.add_parser("run", help="Abrechnungen eines Monats berechnen (Entwurf)")
    c.add_argument("monat", help="JJJJ-MM")
    c.add_argument("--mitarbeiter")
    for f in ("stunden", "bvg", "kinderzulagen", "korrektur", "korrektur_text", "qst_satzbestimmend",
              "qst_gesamtpensum"):
        c.add_argument(f"--{f.replace('_', '-')}", dest=f)
    for name in ("show", "close", "reopen"):
        c = ps.add_parser(name, help="ohne Mitarbeiter: alle Entwürfe des Monats, eine Sammelbuchung"
                          if name == "close" else None)
        c.add_argument("monat")
        c.add_argument("mitarbeiter", nargs="?" if name == "close" else None)
    c = ps.add_parser("payment-export", help="Bankdatei pain.001 erstellen")
    c.add_argument("monat")
    c.add_argument("ausfuehrung")
    c = ps.add_parser("payment-cancel", help="Lohnzahlungsdatei zurückziehen; Bankauftrag separat stornieren")
    c.add_argument("monat")
    c = ps.add_parser("qst-sync", help="Offizielle ESTV-Tarife laden")
    c.add_argument("kanton", help="Kantonskürzel oder ALLE")
    c.add_argument("jahr", type=int)
    c = ps.add_parser("lohnkonto")
    c.add_argument("jahr", type=int)
    c.add_argument("mitarbeiter")
    c = ps.add_parser("lohnausweis")
    c.add_argument("jahr", type=int)
    c.add_argument("mitarbeiter")

    s = sub.add_parser("report", help="Jahresrechnung (Bilanz, Erfolgsrechnung, Anhang)")
    s.add_argument("--jahr", type=int)
    s.add_argument("--pdf", nargs="?", const="", default=None)
    s = sub.add_parser("bericht", help="Berichte für jede Periode: Erfolgsrechnung, Bilanz, Geldfluss, Kennzahlen, "
                                       "Alter, Umsatz; Vorlagen, Kommentare, Monatsbericht")
    s.add_argument("typ", help="erfolgsrechnung, bilanz, geldfluss, kennzahlen, debitoren, kreditoren, umsatz "
                               "(+ Plugins) · liste · monat · vorlage-speichern · vorlage-loeschen")
    s.add_argument("name", nargs="?", default="", help="vorlage-*: Name der Vorlage")
    s.add_argument("--jahr", type=int)
    s.add_argument("--periode", help="jahr, q1–q4, h1, h2, 01–12")
    s.add_argument("--von")
    s.add_argument("--bis")
    s.add_argument("--spalten", choices=["gesamt", "monat", "quartal"])
    s.add_argument("--vergleich", choices=["keine", "vorperiode", "vorjahr", "budget"])
    s.add_argument("--stichtag")
    s.add_argument("--nach", choices=["kunde", "konto", "monat"], help="umsatz: gruppiert nach")
    s.add_argument("--detail", action="store_true", help="mit Konten")
    s.add_argument("--format", default="text", choices=["text", "pdf", "xlsx", "csv"])
    s.add_argument("--out", help="Zieldatei (Standard: berichte/)")
    s.add_argument("--vorlage", default="", help="gespeicherte Vorlage ausführen")
    s.add_argument("--bericht-typ", dest="bericht_typ", default="", help="vorlage-speichern: welcher Bericht")
    s.add_argument("--monat", help="monat: JJJJ-MM (Standard: Vormonat)")
    s.add_argument("--mail", action="store_true", help="monat: per E-Mail senden (aeradex.yaml → mail)")
    s.add_argument("--kommentar", help="Kommentar zu diesem Bericht speichern")
    s.add_argument("--agent", action="store_true", help="Kommentar vom Agenten schreiben lassen")
    s = sub.add_parser("budget", help="Budget pro Konto und Monat (für Budget vs. Ist)")
    bus = s.add_subparsers(dest="sub", required=True)
    c = bus.add_parser("show")
    c.add_argument("--jahr", type=int)
    c = bus.add_parser("set", help="Budget eines Kontos: Jahresbetrag oder 12 Monatswerte")
    c.add_argument("--jahr", type=int)
    c.add_argument("--konto", required=True)
    c.add_argument("--betrag", help="Jahresbetrag (gleichmässig verteilt)")
    c.add_argument("--monate", help="12 Werte, kommagetrennt")
    c = bus.add_parser("vorjahr", help="Budget aus dem Ist des Vorjahres (+ Prozent), mit Saisonverlauf")
    c.add_argument("--jahr", type=int)
    c.add_argument("--prozent", default="0")
    c.add_argument("--basis", type=int, help="anderes Basisjahr")
    s = sub.add_parser("dossier", help="Abschlussunterlagen eines Jahres: alles als ZIP oder einzelne Teile")
    s.add_argument("--jahr", type=int)
    s.add_argument("--teil", default="", help="nur diesen Teil als Datei (statt ZIP): jahresrechnung, saldenliste, "
                                             "journal, kontoblaetter, belege, mwst, offene_posten, lohn, kontenplan")
    s.add_argument("--nur", default="", help="ZIP nur mit diesen Teilen (kommagetrennt)")
    s.add_argument("--format", default="", help="pdf, csv, dateien (Originalbelege); ZIP: kommagetrennt, Standard alle")
    s.add_argument("--liste", action="store_true", help="Teile und Belegnummern-Lücken anzeigen, nichts erzeugen")
    s.add_argument("--out", help="Zieldatei (Standard: berichte/)")
    s = sub.add_parser("export-buch", help="ganzes Buch als .aeradex-Datei für einen anderen aeradex-Nutzer")
    s.add_argument("datei", help="Zieldatei, z.B. 'Muster GmbH.aeradex'")
    s.add_argument("--passwort", action="store_true", help="mit Passwort verschlüsseln (Abfrage)")
    s.add_argument("--passwort-stdin", action="store_true", help="Passwort von stdin lesen (für Skripte)")
    s.add_argument("--ohne-inbox", action="store_true", help="unverarbeitete Dateien in inbox/ weglassen")
    s.add_argument("--ohne-historie", action="store_true", help="ohne Änderungsverlauf (git)")
    s = sub.add_parser("import-buch", help=".aeradex-Datei in einen neuen Ordner importieren")
    s.add_argument("datei")
    s.add_argument("ziel", nargs="?", help="neuer, leerer Ordner (ohne: nur Inhalt anzeigen)")
    s.add_argument("--passwort", action="store_true", help="Datei ist verschlüsselt (Abfrage)")
    s.add_argument("--passwort-stdin", action="store_true")
    s = sub.add_parser("allocation", help="Gewinnverwendung")
    als = s.add_subparsers(dest="sub", required=True)
    c = als.add_parser("set")
    c.add_argument("jahr", type=int)
    c.add_argument("--dividende", default="0")
    c.add_argument("--reserve", default="0")
    c = als.add_parser("book")
    c.add_argument("jahr", type=int)
    c.add_argument("--datum")
    c = als.add_parser("dividende", help="beschlossene Dividende auszahlen (65 %%) und Verrechnungssteuer (35 %%) buchen")
    c.add_argument("jahr", type=int)
    c.add_argument("--datum")
    c.add_argument("--konto", default="", help="Bankkonto (Standard 1020)")
    s = sub.add_parser("lieferant", help="Lieferanten")
    ls = s.add_subparsers(dest="sub", required=True)
    ls.add_parser("list")
    c = ls.add_parser("add")
    c.add_argument("--name", required=True)
    for f in ("strasse", "nr", "plz", "ort", "iban", "konto", "mwst", "email"):
        c.add_argument(f"--{f}", default="")
    c.add_argument("--land", default="CH")

    s = sub.add_parser("kreditor", help="Lieferantenrechnungen")
    ks = s.add_subparsers(dest="sub", required=True)
    c = ks.add_parser("scan", help="QR-Rechnung (PDF/Foto) lesen")
    c.add_argument("datei")
    c = ks.add_parser("list")
    c.add_argument("--status", default="", choices=["", "offen", "angewiesen", "bezahlt", "storniert"])
    c = ks.add_parser("add", help="Rechnung erfassen und buchen")
    c.add_argument("--lieferant", required=True)
    c.add_argument("--betrag", required=True, help="brutto")
    for f in ("datum", "faellig", "konto", "mwst", "rechnungsnr", "referenz", "referenz-typ", "mitteilung", "iban", "datei",
              "waehrung", "kurs"):
        c.add_argument(f"--{f}", dest=f.replace("-", "_"), default=None)
    c.add_argument("--position", action="append", default=[], metavar="KONTO:BETRAG[:MWST[:TEXT]]",
                   help="Aufteilung auf mehrere Konten (mehrfach), Summe = --betrag")
    c = ks.add_parser("pay", help="Zahlung buchen")
    c.add_argument("nr")
    c.add_argument("--datum")
    c.add_argument("--betrag", help="in der Währung des Zahlkontos (bei Fremdwährung vom CHF-Konto: belastete CHF)")
    c.add_argument("--konto", help="Zahlkonto (Standard: Bank bzw. Konto der Rechnungswährung)")
    c.add_argument("--kurs", help="Kurs am Zahltag (Standard: BAZG)")
    c.add_argument("--fw", help="nur diesen Betrag in Rechnungswährung begleichen (Teilzahlung)")
    c = ks.add_parser("void")
    c.add_argument("nr")
    c.add_argument("--grund", default="")
    ks.add_parser("offen", help="offene Kreditoren")
    c = ks.add_parser("einlesen", help="Rechnungen einlesen → Entwürfe (QR, Text, OCR; Konto via Lieferant, Jev, Agent)")
    c.add_argument("dateien", nargs="+")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--agent", dest="agent", action="store_true", default=None,
                   help="unsichere Entwürfe an den Agenten (Standard laut aeradex.yaml)")
    g.add_argument("--ohne-agent", dest="agent", action="store_false")
    ks.add_parser("entwuerfe", help="Entwürfe anzeigen")
    c = ks.add_parser("entwurf-agent", help="Entwurf vom Agenten kontieren lassen")
    c.add_argument("id")
    c = ks.add_parser("entwurf-verwerfen", help="Entwurf verwerfen (Datei bleibt in der Inbox)")
    c.add_argument("id")

    s = sub.add_parser("spesen", help="Spesen der Mitarbeitenden (Rückzahlung über den Lohn)")
    ss = s.add_subparsers(dest="sub", required=True)
    c = ss.add_parser("list")
    c.add_argument("--offen", action="store_true")
    c = ss.add_parser("add")
    c.add_argument("--mitarbeiter", required=True)
    c.add_argument("--datum", required=True)
    c.add_argument("--betrag", required=True)
    c.add_argument("--konto", required=True)
    c.add_argument("--text", default="")
    c.add_argument("--art", default="uebrige", choices=["reise", "uebrige"])
    c.add_argument("--mwst", default="")
    c.add_argument("--datei", default="")
    c = ss.add_parser("remove")
    c.add_argument("nummer")

    s = sub.add_parser("mahnung", help="Mahnwesen: überfällige Rechnungen und Mahnungen (PDF mit QR-Zahlteil)")
    ms = s.add_subparsers(dest="sub", required=True)
    c = ms.add_parser("list", help="überfällige Rechnungen")
    c.add_argument("--datum")
    c = ms.add_parser("erstellen", help="nächste Mahnstufe erstellen")
    c.add_argument("nummern", nargs="+")
    c.add_argument("--datum")
    c.add_argument("--frist", type=int, help="Frist in Tagen (Standard 10)")

    s = sub.add_parser("eingang", help="Belegeingang: Quittungen, Lieferantenrechnungen, eigene Rechnungen einlesen")
    es = s.add_subparsers(dest="sub", required=True)
    c = es.add_parser("einlesen", help="Belege einlesen → Entwürfe (nichts wird gebucht)")
    c.add_argument("dateien", nargs="+")
    c.add_argument("--art", default="", choices=["", "kreditor", "quittung", "debitor"], help="sonst erkannt")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--agent", dest="agent", action="store_true", default=None)
    g.add_argument("--ohne-agent", dest="agent", action="store_false")
    c = es.add_parser("list", help="Entwürfe")
    c.add_argument("--art", default="", choices=["", "kreditor", "quittung", "debitor"])
    c = es.add_parser("buchen", help="Quittungs-Entwurf so buchen, wie er vorbereitet ist")
    c.add_argument("id")
    c.add_argument("--konto", default="", help="Aufwandkonto (sonst aus dem Entwurf)")
    c.add_argument("--zahlkonto", default="", help="statt Bankabgleich/erkanntem Zahlkonto")
    c = es.add_parser("agent", help="Entwurf vom Agenten kontieren lassen")
    c.add_argument("id")
    c = es.add_parser("verwerfen")
    c.add_argument("id")

    s = sub.add_parser("zahlungslauf", help="Zahlungsdatei (pain.001) für das E-Banking")
    zs = s.add_subparsers(dest="sub", required=True)
    c = zs.add_parser("erstellen")
    c.add_argument("nummern", nargs="+", help="E-2026-0001 …")
    c.add_argument("--datum", required=True, help="Ausführungsdatum")
    zs.add_parser("list")
    c = zs.add_parser("bezahlt", help="ausgeführten Zahlungslauf verbuchen")
    c.add_argument("datei")
    c.add_argument("--datum")

    s = sub.add_parser("bank", help="Kontoauszüge (camt.053, CSV/Excel, Kreditkarte)")
    bs = s.add_subparsers(dest="sub", required=True)
    c = bs.add_parser("regel", help="Bankregeln: wiederkehrende Bewegungen beim Import buchen")
    c.add_argument("aktion", choices=["list", "add", "von", "remove"])
    c.add_argument("ref", nargs="?", help="von: Bewegungs-ID · remove: Regel-ID")
    c.add_argument("--konto")
    c.add_argument("--gegenpartei", default="")
    c.add_argument("--text", default="")
    c.add_argument("--betrag")
    c.add_argument("--richtung", default="", choices=["", "belastung", "gutschrift"])
    c.add_argument("--mwst", default="")
    c.add_argument("--buchungstext", default="")
    c.add_argument("--mit-betrag", action="store_true", help="von: nur bei genau diesem Betrag")
    c = bs.add_parser("import", help="Kontoauszug importieren und abgleichen (camt.053, gelernte CSV/Excel, "
                                     "gelesene Kreditkartenabrechnung)")
    c.add_argument("datei")
    c = bs.add_parser("format", help="CSV/Excel-Format einer Bank: vom Agenten lernen lassen, prüfen, bestätigen")
    c.add_argument("aktion", choices=["lernen", "pruefen", "bestaetigen", "list"])
    c.add_argument("ref", nargs="?", help="lernen/pruefen: Datei · bestaetigen: Formatname")
    c.add_argument("--konto", default="", help="lernen: Buchhaltungskonto (z.B. 1020), wenn die Datei keine IBAN nennt")
    c = bs.add_parser("karte", help="Kreditkartenabrechnung (PDF) vom Agenten lesen lassen und importieren")
    c.add_argument("datei")
    c.add_argument("--konto", required=True, help="Kreditkartenkonto (Passivkonto, z.B. 2040)")
    c = bs.add_parser("list")
    c.add_argument("--status", default="", choices=["", "offen", "gebucht", "abgeglichen", "ignoriert"])
    c = bs.add_parser("book", help="offene Bewegung gegen ein Konto buchen")
    c.add_argument("id")
    c.add_argument("--konto", required=True)
    c.add_argument("--text", default="")
    c.add_argument("--mwst", default="")
    c = bs.add_parser("zuordnen", help="mit Rechnung (R-…) oder Kreditor (E-…) begleichen")
    c.add_argument("id")
    c.add_argument("nummer")
    c = bs.add_parser("abgleichen", help="mit bestehendem Beleg verknüpfen")
    c.add_argument("id")
    c.add_argument("beleg")
    c = bs.add_parser("ignorieren")
    c.add_argument("id")
    c.add_argument("--grund", required=True)
    bs.add_parser("abstimmung", help="Schlusssaldo Bank gegen Buchhaltung")
    c = bs.add_parser("vorschlaege", help="was die offenen Bewegungen wahrscheinlich sind (Rechnung, Kreditor, Quittung …)")
    c.add_argument("ids", nargs="*", help="nur diese Bewegungen")
    c = bs.add_parser("uebernehmen", help="Vorschlag übernehmen: eine Bewegung (erster Vorschlag) oder --alle sicheren")
    c.add_argument("id", nargs="?")
    c.add_argument("--art", default="", choices=["", "vorschlag", "rechnung", "kreditor", "quittung", "beleg", "konto"])
    c.add_argument("--ziel", default="", help="Nummer/Konto des Vorschlags (mit --art)")
    c.add_argument("--alle", action="store_true", help="alle sicheren Vorschläge übernehmen")
    c = bs.add_parser("kontieren", help="Gegenkonten mit Jev (TypeSafe) vorschlagen")
    c.add_argument("ids", nargs="*", help="nur diese Bewegungen")
    c.add_argument("--schwelle", type=float, help="Konfidenz ab der ein Vorschlag entsteht (Standard aus Einstellungen)")

    s = sub.add_parser("mwst", help="MWST-Abrechnung")
    ms = s.add_subparsers(dest="sub", required=True)
    c = ms.add_parser("abrechnung", help="Ziffern für die ESTV-Abrechnung")
    c.add_argument("periode", help="z.B. 2026-Q1 oder 2026-S1")
    c.add_argument("--details", action="store_true", help="MWST-Report: alle Buchungen hinter jeder Ziffer")
    c.add_argument("--pdf", nargs="?", const="", default=None, help="Abrechnung mit Herkunft der Zahlen als PDF")
    c = ms.add_parser("buchen", help="Abrechnung buchen (MWST-Konten auf Abrechnungskonto)")
    c.add_argument("periode")
    c = ms.add_parser("export", help="eMWST-Datei (eCH-0217) fürs ESTV-Portal")
    c.add_argument("periode")
    c.add_argument("--korrektur", action="store_true", help="als Korrekturabrechnung")
    c.add_argument("--out")
    c = ms.add_parser("abstimmung", help="Umsatz- und Steuerabstimmung des Jahres (Buchhaltung gegen Abrechnungen)")
    c.add_argument("jahr", type=int)
    c.add_argument("--pdf", nargs="?", const="", default=None, help="als PDF (optional Pfad)")
    c = ms.add_parser("abgrenzung", help="vereinnahmte Entgelte: Steuer auf offenen Posten per 31.12. abgrenzen")
    c.add_argument("jahr", type=int)
    c.add_argument("--neu", action="store_true", help="bestehende Abgrenzung neu berechnen")
    s = sub.add_parser("jahr-eroeffnen", help="nächstes Geschäftsjahr eröffnen (Salden werden vorgetragen)")
    s.add_argument("jahr", type=int, nargs="?", help="Standard: das Jahr nach dem letzten")
    s = sub.add_parser("lock", help="Periode sperren (unveränderlich)")
    s.add_argument("bis")
    s = sub.add_parser("unlock", help="Sperre zurücknehmen (mit Grund)")
    s.add_argument("--bis")
    s.add_argument("--grund", required=True)
    s = sub.add_parser("log", help="Änderungsverlauf (git)")
    s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("qst-import", help="Quellensteuer-Tarife aus Wegleitung-PDF importieren")
    s.add_argument("pdf")
    s.add_argument("--kanton", required=True)
    s.add_argument("--jahr", type=int, required=True)
    sub.add_parser("mcp", help="MCP-Server (stdio) für Claude Code, opencode & Co. starten")
    s = sub.add_parser("serve", help="Mehrbenutzer-Server mit Login (hinter HTTPS-Proxy)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--config", help="Verzeichnis mit users.yaml und secret (Standard ~/.config/aeradex)")
    s.add_argument("--https", action="store_true", help="Cookies nur über HTTPS senden (empfohlen)")
    s = sub.add_parser("user", help="Benutzer für aeradex serve")
    us = s.add_subparsers(dest="sub", required=True)
    for name in ("add", "list", "passwort", "remove"):
        c = us.add_parser(name)
        c.add_argument("--config")
        if name != "list":
            c.add_argument("name")
        if name == "add":
            c.add_argument("--rolle", choices=["lesen", "buchhaltung", "admin"], default="buchhaltung")
            c.add_argument("--anzeige", default="", help="Name im Änderungsverlauf, z.B. 'Anna Muster'")
        if name in ("add", "passwort"):
            c.add_argument("--passwort-stdin", action="store_true", help="Passwort von stdin lesen (für Skripte)")
    s = sub.add_parser("ui", help="Oberfläche im Browser starten (lokal)")
    s.add_argument("--port", type=int, default=5151)
    s.add_argument("--kein-browser", action="store_true", help="Browser nicht automatisch öffnen")

    s = sub.add_parser("plugins", help="Plugins anzeigen, ein- und ausschalten")
    ps = s.add_subparsers(dest="sub")
    ps.add_parser("list", help="installierte Plugins (Standard)")
    for name, text in (("ein", "für dieses Buch einschalten"), ("aus", "für dieses Buch ausschalten")):
        c = ps.add_parser(name, help=text)
        c.add_argument("name")
    ps.add_parser("katalog", help="Plugins im Katalog (geprüft/ungeprüft, installiert?)")
    c = ps.add_parser("installieren", help="Plugin aus dem Katalog installieren")
    c.add_argument("name")
    c.add_argument("--ungeprueft", action="store_true", help="auch ein nicht geprüftes Plugin installieren")
    c.add_argument("--ja", action="store_true", help="ohne Rückfrage")
    c = ps.add_parser("pruefen", help="Maintainer: geprüften Code im Katalog festhalten (Fingerabdruck, Tests)")
    c.add_argument("name")
    c.add_argument("--von", required=True, help="wer geprüft hat")
    c.add_argument("--katalog", help="Katalog-Datei (Standard: die mitgelieferte)")
    c.add_argument("--ohne-tests", action="store_true")
    c = ps.add_parser("zurueckziehen", help="Maintainer: Prüfung zurückziehen")
    c.add_argument("name")
    c.add_argument("--katalog")
    _plugin_commands(sub)
    return p


def _plugin_commands(sub) -> None:
    """Commands contributed by installed plugins; a name aeradex already uses is skipped."""
    try:
        from . import plugins
        found = plugins.commands()
    except ImportError:
        return
    taken = set(sub.choices)
    for name, (plugin, cmd) in found.items():
        if name in taken:
            continue
        parser = sub.add_parser(name, help=f"{cmd.help} [Plugin {plugin}]")
        try:
            cmd.setup(parser)
        except Exception as exc:  # a broken plugin must not break the CLI
            parser.description = f"Plugin-Fehler: {exc}"


REPORT_PARAMS = ("jahr", "periode", "von", "bis", "spalten", "vergleich", "stichtag", "nach")


def _bericht(a, b: Book):
    """`aeradex bericht …`: run, export, comment, templates, monthly package."""
    from . import berichte, reports
    params = {k: getattr(a, k) for k in REPORT_PARAMS if getattr(a, k, None) not in (None, "")}
    if a.typ == "liste":
        return api.report_list(b)
    if a.typ == "monat":
        monat = a.monat
        if not monat:
            first = date.today().replace(day=1)
            prev = date.fromordinal(first.toordinal() - 1)
            monat = f"{prev.year}-{prev.month:02d}"
        return api.monthly_report(b, monat, a.mail)
    if a.typ == "vorlage-speichern":
        return api.report_template_save(b, a.name, a.bericht_typ, **params)
    if a.typ == "vorlage-loeschen":
        return api.report_template_delete(b, a.name)
    typ = "" if a.vorlage else a.typ
    if a.kommentar:
        return api.report_comment_save(b, a.kommentar, typ, a.vorlage, "Mensch (CLI)", **params)
    if a.agent:
        if a.vorlage:
            t = berichte.run_template(b, a.vorlage, **params)          # the template's parameters + overrides
            typ, params = t["typ"], t["parameter"]
        said = berichte.run_agent_comment(b.root, typ, **params)
        return {"ok": True, "meldung": "Kommentar vom Agenten gespeichert", "agent": said}
    if a.format != "text":
        data, name = api.report_export(b, typ, a.format, a.vorlage, a.detail, **params)
        out = Path(a.out) if a.out else b.root / "berichte" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return {"ok": True, "meldung": f"{a.format.upper()} geschrieben", "datei": str(out)}
    if a.json:
        return api.report(b, typ, a.vorlage, **params)
    rep = berichte.run_template(b, a.vorlage, **params) if a.vorlage else reports.run(b, typ, **params)
    c = berichte.comment(b, rep)
    text = berichte.text(rep, a.detail)
    if c:
        text += f"\nKommentar{' (veraltet)' if c['veraltet'] else ''}: {c['text']}\n"
    return {"_text": text + "\n" + berichte.stand_text(berichte.stand(b))}


def dispatch(a, book_path: Path | None):
    def book() -> Book:
        return Book(find_root(book_path))

    c = a.cmd
    if c == "plugins" and a.sub in ("katalog", "installieren", "pruefen", "zurueckziehen"):
        from . import marktplatz
        if a.sub == "katalog":
            try:
                b = book()
            except BookError:
                b = None
            return marktplatz.entries(b)
        if a.sub == "pruefen":
            return marktplatz.review(a.name, a.von, Path(a.katalog) if a.katalog else None, tests=not a.ohne_tests)
        if a.sub == "zurueckziehen":
            return marktplatz.revoke(a.name, Path(a.katalog) if a.katalog else None)
        e = marktplatz.entry(a.name)
        if not a.ja:
            print(f"{e['name']} {e.get('version', '')} — {e.get('beschreibung', '')}\n"
                  f"Status: {e.get('status')}. Ein Plugin ist Programmcode mit den Rechten von aeradex.\n"
                  f"Befehl: {' '.join(marktplatz.install_args(e, display=True))}", file=sys.stderr)
            if input("Installieren? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
                raise BookError("abgebrochen")
        out = marktplatz.install(a.name, a.ungeprueft)
        return {"ok": True, "meldung": f"Plugin {a.name} installiert — `aeradex ui` neu starten, dann im Buch einschalten "
                                       f"(aeradex plugins ein {a.name})", "pip": out}
    if c == "plugins":
        if a.sub == "ein":
            return api.plugin_enable(book(), a.name)
        if a.sub == "aus":
            return api.plugin_disable(book(), a.name)
        try:
            return api.plugin_list(book())
        except BookError:
            return api.plugin_list(None)
    from . import plugins
    plugin_cmds = plugins.commands()
    if c in plugin_cmds:
        name, cmd = plugin_cmds[c]
        if not cmd.needs_book:
            return cmd.run(None, a)
        b = book()
        if name not in plugins.enabled_names(b):
            raise BookError(f"Plugin '{name}' ist für dieses Buch nicht eingeschaltet (aeradex plugins ein {name})")
        return cmd.run(b, a)
    if c in ("export-buch", "import-buch"):
        password = None
        if a.passwort_stdin:
            password = sys.stdin.readline().rstrip("\n")
        elif a.passwort:
            import getpass
            password = getpass.getpass("Passwort: ")
            if c == "export-buch" and getpass.getpass("Wiederholen: ") != password:
                raise BookError("Passwörter stimmen nicht überein")
        if c == "export-buch":
            return api.book_export(book(), a.datei, password, a.ohne_inbox, a.ohne_historie)
        if not a.ziel:
            return api.book_inspect(a.datei, password)
        return api.book_import(a.datei, a.ziel, password)
    if c == "bericht":
        return _bericht(a, book())
    if c == "budget":
        b = book()
        jahr = a.jahr or date.today().year
        if a.sub == "show":
            return api.budget_show(b, jahr)
        if a.sub == "set":
            months = [x.strip() for x in a.monate.split(",")] if a.monate else None
            return api.budget_set(b, jahr, a.konto, a.betrag, months)
        return api.budget_from_prior(b, jahr, a.prozent, a.basis)
    if c == "dossier":
        b = book()
        if a.liste:
            return api.dossier_overview(b, a.jahr)
        if a.teil:
            return api.dossier_part(b, a.teil, a.jahr, a.format or "pdf", a.out)
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()]  # noqa: E731
        return api.dossier(b, a.jahr, split(a.nur), split(a.format), a.out)
    if c == "init":
        fields = {f: getattr(a, f) for f in ("uid", "strasse", "nr", "plz", "ort", "iban", "telefon", "email")}
        return api.init_book(Path(a.ordner), a.firma, a.jahr, a.kontenplan, a.rechtsform,
                             git=not a.ohne_git, **fields)
    if c == "status":
        return api.status(book())
    if c == "check":
        return api.run_check(book())
    if c == "accounts":
        return api.accounts(book(), a.suche)
    if c == "account-add":
        return api.add_account(book(), a.nr, a.name, a.klasse, a.gruppe, a.waehrung)
    if c == "kurs":
        return api.fx_rate(book(), a.waehrung, a.datum)
    if c == "bewertung":
        return api.fx_revalue(book(), a.stichtag) if a.buchen else api.fx_preview(book(), a.stichtag)
    if c == "balance":
        return api.balances(book(), a.jahr, a.periode)
    if c == "ledger":
        if a.pdf is not None:
            return api.ledger_pdf(book(), a.konto, a.jahr, a.pdf or None)
        return api.ledger(book(), a.konto, a.jahr)
    if c == "journal":
        if a.pdf is not None:
            return api.journal_pdf(book(), a.jahr, a.pdf or None)
        return api.journal_rows(book(), a.jahr, a.monat, a.beleg, a.konto, a.suche, a.limit)
    if c == "book":
        return api.post_entry(book(), a.datum, a.soll, a.haben, a.betrag, a.text, a.beleg, a.datei, a.mwst, a.waehrung, a.kurs)
    if c == "book-split":
        return api.post_split(book(), a.datum, a.text, json.loads(a.zeilen), a.beleg, a.datei)
    if c == "reverse":
        return api.reverse_entry(book(), a.beleg, a.datum, a.text)
    if c == "propose":
        return api.propose(book(), a.datum, a.soll, a.haben, a.betrag, a.text, a.begruendung, a.datei, a.mwst, waehrung=a.waehrung, kurs=a.kurs)
    if c == "proposals":
        return api.proposals(book())
    if c == "approve":
        return api.approve(book(), a.ids)
    if c == "reject":
        return api.reject(book(), a.ids)
    if c == "customer":
        if a.sub == "list":
            return api.customer_list(book())
        return api.customer_add(book(), name=a.name, firma=a.firma, strasse=a.strasse, nr=a.nr, plz=a.plz,
                                ort=a.ort, land=a.land, email=a.email, rechnung_an=a.rechnung_an,
                                stundensatz=a.stundensatz)
    if c == "invoice":
        b = book()
        if a.sub == "list":
            return api.invoice_list(b, a.status)
        if a.sub == "show":
            return api.invoice_show(b, a.nr)
        if a.sub == "create":
            return api.invoice_create(b, a.kunde, _positions(a.pos), a.datum, a.text, a.zahlungsfrist)
        if a.sub == "void":
            return api.invoice_void(b, a.nr, a.grund)
        if a.sub == "pay":
            return api.invoice_pay(b, a.nr, a.betrag, a.datum, a.konto)
        if a.sub == "credit":
            return api.invoice_credit(b, a.nr, a.betrag, a.datum, a.konto, a.grund)
        if a.sub == "match":
            return api.invoice_match(b, a.betrag, a.text)
    if c == "receivables":
        return api.receivables(book(), a.stichtag, a.pdf)
    if c == "employee":
        if a.sub == "list":
            return api.employee_list(book())
        fields = {k: getattr(a, k) for k in ("strasse", "nr", "plz", "ort", "ahv_nr", "geburtsdatum", "eintritt",
                                             "lohnart", "monatslohn", "pensum", "stundenlohn", "standard_stunden",
                                             "bvg_betrag", "kinderzulagen", "ferienzuschlag_satz", "qst_satz")
                  if getattr(a, k) not in (None, "")}
        if a.qst_code:
            fields["qst"] = {"kanton": a.qst_kanton, "jahr": a.qst_jahr, "code": a.qst_code.upper()}
        return api.employee_add(book(), a.vorname, a.nachname, **fields)
    if c == "payroll":
        b = book()
        if a.sub == "payment-export":
            return api.payroll_payment_export(b, a.monat, a.ausfuehrung)
        if a.sub == "payment-cancel":
            return api.payroll_payment_cancel(b, a.monat)
        if a.sub == "qst-sync":
            return api.qst_sync(b, a.kanton, a.jahr)
        if a.sub == "run":
            inputs = {k: getattr(a, k) for k in ("stunden", "bvg", "kinderzulagen", "korrektur", "korrektur_text",
                                                 "qst_satzbestimmend", "qst_gesamtpensum") if getattr(a, k) is not None}
            if inputs and not a.mitarbeiter:
                raise BookError("Eingaben (--stunden, …) nur zusammen mit --mitarbeiter")
            return api.payroll_run(b, a.monat, a.mitarbeiter, inputs or None)
        if a.sub == "show":
            return api.payslip_show(b, a.monat, a.mitarbeiter)
        if a.sub == "close":
            if not a.mitarbeiter:
                return api.payroll_close(b, a.monat)
            return api.payslip_close(b, a.monat, a.mitarbeiter)
        if a.sub == "reopen":
            return api.payslip_reopen(b, a.monat, a.mitarbeiter)
        if a.sub == "lohnkonto":
            return api.lohnkonto(b, a.jahr, a.mitarbeiter)
        if a.sub == "lohnausweis":
            return api.lohnausweis_create(b, a.jahr, a.mitarbeiter)
    if c == "report":
        return api.statement(book(), a.jahr, a.pdf or None, als_pdf=a.pdf is not None)
    if c == "allocation":
        if a.sub == "set":
            return api.allocation_set(book(), a.jahr, a.dividende, a.reserve)
        if a.sub == "dividende":
            return api.dividend_pay(book(), a.jahr, a.datum, a.konto)
        return api.allocation_book(book(), a.jahr, a.datum)
    if c == "lieferant":
        if a.sub == "list":
            return api.supplier_list(book())
        return api.supplier_add(book(), name=a.name, strasse=a.strasse, nr=a.nr, plz=a.plz, ort=a.ort, land=a.land,
                                iban=a.iban, konto=a.konto, mwst=a.mwst, email=a.email)
    if c == "kreditor":
        b = book()
        if a.sub == "scan":
            return api.qr_scan(b, a.datei)
        if a.sub == "list":
            return api.bill_list(b, a.status)
        if a.sub == "add":
            fields = {k: getattr(a, k) for k in ("datum", "faellig", "konto", "mwst", "rechnungsnr", "referenz",
                                                 "referenz_typ", "mitteilung", "iban", "datei", "waehrung", "kurs")
                      if getattr(a, k) is not None}
            if a.position:
                lines = []
                for spec in a.position:
                    parts = spec.split(":", 3)
                    if len(parts) < 2:
                        raise BookError(f"--position {spec}: erwartet KONTO:BETRAG[:MWST[:TEXT]]")
                    lines.append({"konto": parts[0], "betrag": parts[1], "mwst": parts[2] if len(parts) > 2 else "",
                                  "text": parts[3] if len(parts) > 3 else ""})
                fields["positionen"] = lines
            return api.bill_add(b, a.lieferant, a.betrag, **fields)
        if a.sub == "pay":
            return api.bill_pay(b, a.nr, a.datum, a.betrag, a.konto, a.kurs, a.fw)
        if a.sub == "void":
            return api.bill_void(b, a.nr, a.grund)
        if a.sub == "offen":
            return api.payables(b)
        if a.sub == "einlesen":
            from . import erfassung
            out = []
            for datei in a.dateien:
                res = api.bill_draft_create(Book(b.root), datei, getattr(a, "art", "") or "kreditor")
                d = res["entwurf"]
                use_agent = erfassung.agent_auto(Book(b.root)) if a.agent is None else a.agent
                if erfassung.needs_agent(d) and use_agent:
                    print(f"… {d['id']}: Konto unsicher, frage den Agenten", file=sys.stderr)
                    try:
                        d = api.bill_draft_agent(Book(b.root), d["id"])["entwurf"]
                    except BookError as exc:
                        print(f"✗ {d['id']}: {exc}", file=sys.stderr)
                        d = erfassung.draft(Book(b.root), d["id"])
                        d = {k: v for k, v in d.items() if not k.startswith("_")}
                out.append(d)
            return api.jsonable(out)
        if a.sub == "entwuerfe":
            return api.bill_drafts(b)
        if a.sub == "entwurf-agent":
            return api.bill_draft_agent(b, a.id)
        if a.sub == "entwurf-verwerfen":
            return api.bill_draft_discard(b, a.id)
    if c == "spesen":
        if a.sub == "list":
            return api.expense_list(book(), "", a.offen)
        if a.sub == "remove":
            return api.expense_remove(book(), a.nummer)
        return api.expense_add(book(), a.mitarbeiter, a.datum, a.text, a.betrag, a.konto, a.mwst, None, a.art, a.datei)
    if c == "mahnung":
        if a.sub == "list":
            return api.reminders(book(), a.datum)
        return api.reminder_create(book(), a.nummern, a.datum, a.frist)
    if c == "eingang":
        from . import erfassung
        b = book()
        if a.sub == "einlesen":
            out = []
            for datei in a.dateien:
                d = api.bill_draft_create(Book(b.root), datei, a.art)["entwurf"]
                use_agent = erfassung.agent_auto(Book(b.root)) if a.agent is None else a.agent
                if erfassung.needs_agent(d) and use_agent:
                    print(f"… {d['id']}: Konto unsicher, frage den Agenten", file=sys.stderr)
                    try:
                        d = api.bill_draft_agent(Book(b.root), d["id"])["entwurf"]
                    except BookError as exc:
                        print(f"✗ {d['id']}: {exc}", file=sys.stderr)
                        d = {k: v for k, v in erfassung.draft(Book(b.root), d["id"]).items() if not k.startswith("_")}
                out.append(d)
            return api.jsonable(out)
        if a.sub == "list":
            return [d for d in api.bill_drafts(b) if not a.art or d["art"] == a.art]
        if a.sub == "buchen":
            meta = erfassung.draft(b, a.id)
            v = erfassung.form_values(meta)
            zahlung = {"art": "konto", "konto": a.zahlkonto} if a.zahlkonto else meta.get("zahlung")
            return api.receipt_book(b, a.id, v["datum"] or None, (v["name"] or "Quittung").strip(), v["betrag"],
                                    a.konto or v["konto"], v["mwst"], v["positionen"] or None,
                                    "" if (v["waehrung"] or "CHF") == "CHF" else v["waehrung"], None, zahlung)
        if a.sub == "agent":
            return api.bill_draft_agent(b, a.id)
        if a.sub == "verwerfen":
            return api.bill_draft_discard(b, a.id)
    if c == "zahlungslauf":
        b = book()
        if a.sub == "erstellen":
            return api.payment_run(b, a.nummern, a.datum)
        if a.sub == "list":
            from . import kreditoren as kred
            return api.jsonable(kred.runs(b))
        return api.payment_run_book(b, a.datei, a.datum)
    if c == "bank":
        if a.sub == "regel":
            b = book()
            if a.aktion == "list":
                return api.bank_rules(b)
            if a.aktion == "von":
                return api.bank_rule_from(b, a.ref, a.mit_betrag)
            if a.aktion == "remove":
                return api.bank_rule_remove(b, a.ref)
            if not a.konto:
                raise BookError("--konto fehlt")
            return api.bank_rule_add(b, a.konto, a.gegenpartei, a.text, a.betrag, a.richtung, a.mwst, a.buchungstext)
        b = book()
        if a.sub == "import":
            return api.bank_import(b, a.datei)
        if a.sub == "format":
            if a.aktion == "list":
                return api.bank_format_list(b)
            if not a.ref:
                raise BookError("Datei bzw. Formatname fehlt")
            if a.aktion == "lernen":
                return api.bank_format_learn(b, a.ref, a.konto)
            if a.aktion == "pruefen":
                return api.bank_format_check(b, a.ref)
            return api.bank_format_confirm(b, a.ref)
        if a.sub == "karte":
            return api.card_statement_read(b, a.datei, a.konto)
        if a.sub == "list":
            return api.bank_list(b, a.status)
        if a.sub == "book":
            return api.bank_book(b, a.id, a.konto, a.text, a.mwst)
        if a.sub == "zuordnen":
            return api.bank_assign(b, a.id, a.nummer)
        if a.sub == "abgleichen":
            return api.bank_link(b, a.id, a.beleg)
        if a.sub == "ignorieren":
            return api.bank_ignore(b, a.id, a.grund)
        if a.sub == "kontieren":
            return api.bank_suggest(b, a.ids or None, a.schwelle)
        if a.sub == "vorschlaege":
            return api.bank_suggestions(b, a.ids or None)
        if a.sub == "uebernehmen":
            if a.alle:
                return api.bank_accept_all(b)
            if not a.id:
                raise BookError("ID einer Bewegung angeben oder --alle")
            return api.bank_accept(b, a.id, a.art, a.ziel)
        return api.bank_reconciliation(b)
    if c == "mwst":
        if a.sub == "abrechnung":
            if a.details or a.pdf is not None:
                return api.mwst_details(book(), a.periode, a.pdf or None, a.pdf is not None)
            return api.mwst_report(book(), a.periode)
        if a.sub == "export":
            return api.mwst_export(book(), a.periode, a.korrektur, a.out)
        if a.sub == "abstimmung":
            return api.mwst_abstimmung(book(), a.jahr, a.pdf, a.pdf is not None)
        if a.sub == "abgrenzung":
            return api.mwst_abgrenzung(book(), a.jahr, a.neu)
        return api.mwst_book(book(), a.periode)
    if c == "jahr-eroeffnen":
        return api.year_open(book(), a.jahr)
    if c == "lock":
        return api.lock(book(), a.bis)
    if c == "unlock":
        return api.unlock(book(), a.bis, a.grund)
    if c == "log":
        return api.history(book(), a.limit)
    if c == "qst-import":
        from . import qst_import
        data = qst_import.build(Path(a.pdf), a.kanton, a.jahr)
        target = qst_import.Path(qst_import.__file__).resolve().parent / "data" / "qst_tarife" / f"{data['kanton']}-{data['jahr']}.json"
        target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return {"ok": True, "datei": str(target)}
    raise BookError(f"Unbekannter Befehl {c}")


def render(cmd: str, sub: str | None, result) -> str:
    """Human output for the common commands; everything else as indented JSON."""
    if isinstance(result, dict) and set(result) == {"_text"}:
        return result["_text"]
    if isinstance(result, dict) and "meldung" in result and result.get("ok"):
        lines = [f"✓ {result['meldung']}"]
        if result.get("commit"):
            lines.append(f"  commit {result['commit']}")
        if result.get("pdf"):
            lines.append(f"  PDF {result['pdf']}")
        if result.get("datei") and isinstance(result.get("datei"), str):
            lines.append(f"  Datei {result['datei']}")
        return "\n".join(lines)
    if cmd == "check":
        if not result["probleme"]:
            return "✓ Buch ist in Ordnung."
        return "\n".join(f"[{p['stufe']}] {p['ort']}: {p['meldung']}" for p in result["probleme"]) + \
            ("\n✓ keine Fehler" if result["ok"] else "\n✗ Buch hat Fehler")
    if cmd == "status":
        r = result
        out = [f"{r['firma']}  ({r['buch']})",
               f"  Jahre {r['jahre'][0]}–{r['jahre'][-1]} · {r['buchungen']} Buchungszeilen · gesperrt bis {r['sperre_bis'] or '—'}",
               f"  Flüssige Mittel     {_fmt(r['fluessige_mittel']):>14}",
               f"  Ergebnis lfd. Jahr  {_fmt(r['jahresergebnis_laufend']):>14}",
               f"  Offene Rechnungen   {r['offene_rechnungen']:>4}  {_fmt(r['offen_total']):>14}",
               f"  Vorschläge          {r['vorschlaege']:>4}",
               f"  Inbox               {len(r['inbox']):>4}  {', '.join(r['inbox'][:5])}",
               f"  Prüfung             {r['fehler']} Fehler, {r['warnungen']} Warnungen"]
        out += [f"  · {h['ort']}: {h['meldung']}" for h in r["hinweise"]]
        return "\n".join(out)
    if cmd == "accounts":
        return _table(result, [("konto", "Konto"), ("name", "Name"), ("klasse", "Klasse"), ("gruppe", "Gruppe")])
    if cmd == "balance":
        return f"Saldenliste {result['von']} – {result['bis']}\n" + _table(
            result["konten"], [("konto", "Konto"), ("name", "Name"), ("eroeffnung", "Eröffnung"),
                               ("soll", "Soll"), ("haben", "Haben"), ("saldo", "Saldo")],
            right=("eroeffnung", "soll", "haben", "saldo"))
    if cmd == "ledger" and "zeilen" in result:
        return (f"Konto {result['konto']} {result['name']} · {result['jahr']}\nEröffnung {_fmt(result['eroeffnung'])}\n"
                + _table(result["zeilen"], [("datum", "Datum"), ("beleg", "Beleg"), ("text", "Text"),
                                            ("gegenkonto", "Gegenkto"), ("soll", "Soll"), ("haben", "Haben"),
                                            ("saldo", "Saldo")], right=("soll", "haben", "saldo"))
                + f"\nSaldo {_fmt(result['saldo'])}")
    if cmd == "journal" and isinstance(result, list):
        return _table(result, [("datum", "Datum"), ("beleg", "Beleg"), ("text", "Text"), ("soll", "Soll"),
                               ("haben", "Haben"), ("betrag", "Betrag"), ("quelle", "Quelle")], right=("betrag",))
    if cmd == "proposals":
        return _table(result, [("ID", "ID"), ("Datum", "Datum"), ("Text", "Text"), ("Soll", "Soll"),
                               ("Haben", "Haben"), ("Betrag", "Betrag"), ("Begründung", "Begründung")],
                      right=("Betrag",))
    if cmd == "invoice" and sub == "list":
        return _table(result, [("nummer", "Rechnung"), ("datum", "Datum"), ("name", "Kunde"), ("total", "Total"),
                               ("offen", "Offen"), ("status", "Status")], right=("total", "offen"))
    if cmd == "bank" and sub == "vorschlaege" and isinstance(result, dict):
        if not result:
            return "Keine Vorschläge für offene Bewegungen."
        return "\n".join(f"{tid}\n" + "\n".join(f"  {'✓' if o['sicher'] else '?'} {o['label']}  ({o['grund']})"
                                                for o in opts) for tid, opts in result.items()) + \
            "\n\n✓ = sicher (aeradex bank uebernehmen --alle) · ? = prüfen (aeradex bank uebernehmen ID --art … --ziel …)"
    if cmd == "bank" and sub == "list":
        return _table(result, [("ID", "ID"), ("Datum", "Datum"), ("Betrag", "Betrag"), ("Gegenpartei", "Gegenpartei"),
                               ("Text", "Text"), ("Status", "Status"), ("Beleg", "Beleg")], right=("Betrag",))
    if cmd == "kreditor" and sub == "list":
        return _table(result, [("nummer", "Kreditor"), ("datum", "Datum"), ("name", "Lieferant"), ("rechnungsnr", "Rg.-Nr."),
                               ("faellig", "Fällig"), ("total", "Betrag"), ("offen", "Offen"), ("status", "Status")],
                      right=("total", "offen"))
    if cmd in ("customer", "employee", "lieferant") and sub == "list":
        return _table(result, [(k, k.capitalize()) for k in result[0]] if result else [])
    if cmd == "report" and "aktiven" in result:
        out = [f"{result['firma']} — Jahresrechnung {result['jahr']}"]
        for title, key in (("BILANZ — Aktiven", "aktiven"), ("BILANZ — Passiven", "passiven"), ("ERFOLGSRECHNUNG", "erfolg")):
            out.append(f"\n{title}{'':<52}{result['jahr']:>14}{result['vorjahr']:>14}")
            for r in result[key]:
                label = r["label"] if r["stil"] == "line" else r["label"].upper()
                out.append(f"  {label[:62]:<64}{_fmt(r['aktuell']):>14}{_fmt(r['vorjahr']):>14}")
        if Decimal(result["differenz"]):
            out.append(f"\n✗ Bilanzdifferenz {result['differenz']}")
        if result.get("pdf"):
            out.append(f"\nPDF {result['pdf']}")
        return "\n".join(out)
    if cmd == "mwst" and "zeilen" in result:
        cash = result["abrechnungsart"] == "vereinnahmt"
        out = [f"MWST-Umsatzabstimmung {result['jahr']} ({result['methode']}, {result['abrechnungsart']}e Entgelte)"]
        head = f"  {'Ziffer':<11}{'Buchhaltung':>14}" + (f"{'+offen 1.1.':>14}{'−offen 31.12.':>14}" if cash else "") \
            + f"{'Zu dekl.':>14}{'Deklariert':>14}{'Differenz':>12}"
        out.append(head)
        for z in result["zeilen"]:
            out.append(f"  {z['ziffer']:<11}{_fmt(z['buchhaltung']):>14}"
                       + (f"{_fmt(z['offen_anfang']):>14}{_fmt(z['offen_ende']):>14}" if cash else "")
                       + f"{_fmt(z['soll']):>14}{_fmt(z['deklariert']):>14}{_fmt(z['differenz']):>12}"
                       + ("  Rundung" if z["rundung"] else ""))
        e = result["ertrag"]
        out.append(f"\nErtrag laut Erfolgsrechnung {_fmt(e['total'])} · mit Umsatzcode {_fmt(e['mit_code'])} · ohne Code {_fmt(e['ohne_code'])}")
        for k in e["konten"]:
            if Decimal(k["ohne_code"]):
                out.append(f"  {k['konto']} {k['name'][:40]:<42} ohne Code {_fmt(k['ohne_code']):>14}")
        out.append("\nMWST-Konten per 31.12.")
        for k in result["steuerkonten"]:
            out.append(f"  {k['konto']} {k['rolle']:<24} Saldo {_fmt(k['saldo']):>12}  erwartet {_fmt(k['erwartet']):>12}"
                       f"  Differenz {_fmt(k['differenz']):>10}" + (f"  ({k['erklaerung']})" if k["erklaerung"] else ""))
        for h in result["hinweise"]:
            out.append(f"· {h}")
        out.append("\n✓ abgestimmt" if result["ok"] else "\n✗ nicht abgestimmt")
        if result.get("pdf"):
            out.append(f"PDF: {result['pdf']}")
        return "\n".join(out)
    if cmd == "mwst" and "gruppen" in result:
        out = [f"MWST-Report {result['periode']} ({result['methode']}, {result['abrechnungsart']}e Entgelte) · "
               f"{result['von']} – {result['bis']} · {result['zeilen']} Buchungszeilen"]
        for g in result["gruppen"]:
            out.append(f"\nZiffer {g['ziffer']} {g['label']}")
            for z in g["zeilen"]:
                out.append(f"  {z['datum']}  {z['beleg']:<16} {z['text'][:38]:<38} {z['konto']:>5} {z['code']:<4}"
                           f"{_fmt(z['entgelt']) if Decimal(z['entgelt']) else '':>13}"
                           f"{_fmt(z['steuer']) if Decimal(z['steuer']) else '':>11}"
                           + (f"  ({z['hinweis']})" if z["hinweis"] else ""))
            out.append(f"  {'Summe der Buchungen':<72}{_fmt(g['entgelt']):>13}{_fmt(g['steuer']):>11}")
            ze, zs = g["ziffer_entgelt"], g["ziffer_steuer"]
            out.append(f"  {'Ziffer ' + g['ziffer'] + ' in der Abrechnung':<72}{_fmt(ze) if ze is not None else '':>13}"
                       f"{_fmt(zs) if zs is not None else '':>11}")
            if Decimal(g["differenz_entgelt"]):
                out.append(f"  ✗ Entgelt weicht um {_fmt(g['differenz_entgelt'])} ab")
            if Decimal(g["differenz_steuer"]):
                out.append(f"  · Steuer: ESTV rechnet Entgelt × Satz, Rundungsdifferenz {_fmt(g['differenz_steuer'])}")
        z = result["ziffern"]
        out.append(f"\n{'Zahllast' if Decimal(result['zahllast']) >= 0 else 'Guthaben'} "
                   f"{_fmt(abs(Decimal(result['zahllast'])))}")
        if result.get("pdf"):
            out.append(f"PDF {result['pdf']}")
        return "\n".join(out)
    if cmd == "mwst" and "ziffern" in result:
        z = result["ziffern"]
        out = [f"MWST-Abrechnung {result['periode']} ({result['methode']}) · {result['von']} – {result['bis']}"]
        for key in sorted(z, key=lambda k: (k[:3], k)):
            out.append(f"  Ziffer {key:<12}{_fmt(z[key]):>14}")
        out.append(f"  {'Zahllast' if Decimal(result['zahllast']) >= 0 else 'Guthaben':<19}{_fmt(abs(Decimal(result['zahllast']))):>14}"
                   + ("   (gebucht)" if result["gebucht"] else ""))
        return "\n".join(out)
    if cmd == "mahnung" and sub == "list" and isinstance(result, list):
        if not result:
            return "Keine überfälligen Rechnungen."
        return _table([{**r, "stand": r["letzte"]["bezeichnung"] if r["letzte"] else "—",
                        "naechste": r["naechste_bezeichnung"] + ("" if r["bereit"] else f" ab {r['ab']}")} for r in result],
                      [("nummer", "Rechnung"), ("name", "Kunde"), ("faellig", "Fällig"), ("tage", "Tage"),
                       ("offen", "Offen"), ("stand", "Gemahnt"), ("naechste", "Nächster Schritt")], right=("tage", "offen"))
    if cmd in ("kreditor", "eingang") and sub in ("einlesen", "entwuerfe", "list") and isinstance(result, list):
        if not result:
            return "Keine Entwürfe."
        out = []
        for d in result:
            f = d.get("felder") or {}
            g = lambda k: (f.get(k) or {}).get("wert", "")  # noqa: E731
            konto = d.get("konto") or {}
            out.append(f"{d['id']}  {d.get('art', 'kreditor'):<9} {d['status']:<14} {g('name')[:28]:<28} {g('betrag'):>10}  "
                       f"Konto {konto.get('wert') or '—'}" + (f" ({konto.get('quelle')})" if konto.get("quelle") else ""))
            for h in d.get("hinweise") or []:
                out.append(f"      · {h}")
        out.append("\nPrüfen und buchen: Oberfläche → Prüfen → Eingang (Quittungen auch: aeradex eingang buchen ID)")
        return "\n".join(out)
    if cmd == "plugins" and sub == "katalog" and isinstance(result, list):
        out = []
        for e in result:
            mark = "✓ geprüft" if e["geprueft"] else "· ungeprüft"
            state = f"installiert {e['installiert']}" if e["installiert"] else "nicht installiert"
            code = f", Code {e['code']}" if e["code"] else ""
            out.append(f"{e['name']:<20} {mark:<12} {state}{code}\n    {e.get('beschreibung', '')}\n"
                       f"    darf: {', '.join(e['rechte']) or '—'}")
        return "\n".join(out) or "Der Katalog ist leer."
    if cmd == "plugins" and isinstance(result, list):
        if not result:
            return "Keine Plugins installiert. Plugins sind Python-Pakete: pip install aeradex-<name>"
        out = []
        for r in result:
            mark = ("✗" if not r["ok"] else "◆" if r["nur_daten"] else "●" if r["eingeschaltet"] else "○")
            out.append(f"{mark} {r['name']:<20} {r['version']:<8} {r['beschreibung']}")
            if r["fehler"]:
                out.append(f"    ✗ {r['fehler']}")
            elif r["hooks"]:
                out.append(f"    {', '.join(r['hooks'])}")
        out.append("\n● eingeschaltet  ○ installiert, für dieses Buch aus  ◆ nur Daten (immer verfügbar)  ✗ Problem")
        return "\n".join(out)
    if cmd == "dossier" and isinstance(result, dict) and "teile" in result:
        out = [f"Abschlussunterlagen {result['jahr']}" + (" — ENTWURF (Jahr nicht gesperrt)" if result["entwurf"] else "")]
        for t in result["teile"]:
            mark = "✓" if t["vorhanden"] else "·"
            out.append(f"  {mark} {t['nr']} {t['teil']:<15} {t['label']:<22} {'/'.join(t['formate']):<16} {t['beschreibung']}")
        out.append("\nBelegnummern-Lücken:" if result["luecken"] else "\nBelegnummern lückenlos.")
        for g in result["luecken"]:
            out.append(f"  {g['beleg']:<14} {g['art']:<11} {g['grund']}")
        return "\n".join(out)
    if isinstance(result, dict) and set(result) == {"pdf"}:
        return f"PDF {result['pdf']}"
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Global flags are accepted anywhere on the line — agents put them at the end.
    flags = [f for f in ("--json", "--no-commit") if f in argv]
    argv = flags + [x for x in argv if x not in flags]
    a = build_parser().parse_args(argv)
    if a.no_commit:
        os.environ["AERADEX_NO_COMMIT"] = "1"
    if a.cmd == "mcp":
        from .mcp_server import serve
        if a.buch:
            os.environ["AERADEX_BUCH"] = a.buch
        serve()
        return 0
    if a.cmd == "user":
        from .web.auth import UserStore, config_dir
        store = UserStore(config_dir(a.config))
        try:
            if a.sub == "list":
                for u in store.list():
                    print(f"{u.name:<16} {u.rolle:<12} {u.anzeige}")
                return 0
            if a.sub == "remove":
                store.remove(a.name)
                print(f"✓ Benutzer {a.name} entfernt")
                return 0
            if a.passwort_stdin:
                password = sys.stdin.readline().rstrip("\n")
            else:
                import getpass
                password = getpass.getpass("Passwort: ")
                if getpass.getpass("Wiederholen: ") != password:
                    print("✗ Passwörter stimmen nicht überein", file=sys.stderr)
                    return 2
            if a.sub == "add":
                u = store.add(a.name, password, a.rolle, a.anzeige)
                print(f"✓ Benutzer {u.name} ({u.rolle}) angelegt in {store.path}")
            else:
                store.set_password(a.name, password)
                print(f"✓ Passwort für {a.name} geändert; bestehende Anmeldungen sind beendet")
            return 0
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
    if a.cmd == "serve":
        try:
            from .web.app import run_server
        except ImportError as exc:
            print(f"✗ Für den Server fehlen Pakete ({exc.name}): pip install 'aeradex[ui]'", file=sys.stderr)
            return 2
        try:
            root = find_root(Path(a.buch) if a.buch else None)
        except BookError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        run_server(root, a.host, a.port, Path(a.config) if a.config else None, a.https)
        return 0
    if a.cmd == "ui":
        try:
            from .web.app import run
        except ImportError as exc:
            print(f"✗ Für die Oberfläche fehlen Pakete ({exc.name}): pip install 'aeradex[ui]'", file=sys.stderr)
            return 2
        try:
            root = find_root(Path(a.buch) if a.buch else None)
        except BookError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        run(root, a.port, open_browser=not a.kein_browser)
        return 0
    try:
        result = dispatch(a, Path(a.buch) if a.buch else None)
    except (BookError, FormatError, ValueError, RuntimeError) as exc:
        if a.json:
            print(json.dumps({"ok": False, "fehler": str(exc)}, ensure_ascii=False))
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 2
    if a.cmd == "check":
        if a.quiet:
            errors = [p for p in result["probleme"] if p["stufe"] == "fehler"]
            for p in errors:
                print(f"aeradex check: {p['ort']}: {p['meldung']}", file=sys.stderr)
            return 1 if errors else 0
        print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else render("check", None, result))
        return 0 if result["ok"] else 1
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(a.cmd, getattr(a, "sub", None), result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

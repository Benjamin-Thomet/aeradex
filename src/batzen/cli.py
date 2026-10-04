"""batzen — command line. Every command can print JSON (--json) for agents."""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    p = argparse.ArgumentParser(prog="batzen", description="Swiss bookkeeping your agent can run.")
    p.add_argument("--buch", help="Buchordner (Standard: aktueller Ordner oder $BATZEN_BUCH)")
    p.add_argument("--json", action="store_true", help="Ausgabe als JSON (für Agenten)")
    p.add_argument("--no-commit", action="store_true", help="Änderungen nicht in git committen")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="neues Buch anlegen")
    s.add_argument("ordner")
    s.add_argument("--firma", required=True)
    s.add_argument("--jahr", type=int)
    s.add_argument("--rechtsform", default="GmbH")
    s.add_argument("--kontenplan", default="kmu")
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
        c = ps.add_parser(name)
        c.add_argument("monat")
        c.add_argument("mitarbeiter")
    c = ps.add_parser("lohnkonto")
    c.add_argument("jahr", type=int)
    c.add_argument("mitarbeiter")
    c = ps.add_parser("lohnausweis")
    c.add_argument("jahr", type=int)
    c.add_argument("mitarbeiter")

    s = sub.add_parser("report", help="Jahresrechnung (Bilanz, Erfolgsrechnung, Anhang)")
    s.add_argument("--jahr", type=int)
    s.add_argument("--pdf", nargs="?", const="", default=None)
    s = sub.add_parser("allocation", help="Gewinnverwendung")
    als = s.add_subparsers(dest="sub", required=True)
    c = als.add_parser("set")
    c.add_argument("jahr", type=int)
    c.add_argument("--dividende", default="0")
    c.add_argument("--reserve", default="0")
    c = als.add_parser("book")
    c.add_argument("jahr", type=int)
    c.add_argument("--datum")
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
    for f in ("datum", "faellig", "konto", "mwst", "rechnungsnr", "referenz", "referenz-typ", "mitteilung", "iban", "datei"):
        c.add_argument(f"--{f}", dest=f.replace("-", "_"), default=None)
    c = ks.add_parser("pay", help="Zahlung buchen")
    c.add_argument("nr")
    c.add_argument("--datum")
    c.add_argument("--betrag")
    c = ks.add_parser("void")
    c.add_argument("nr")
    c.add_argument("--grund", default="")
    ks.add_parser("offen", help="offene Kreditoren")

    s = sub.add_parser("zahlungslauf", help="Zahlungsdatei (pain.001) für das E-Banking")
    zs = s.add_subparsers(dest="sub", required=True)
    c = zs.add_parser("erstellen")
    c.add_argument("nummern", nargs="+", help="E-2026-0001 …")
    c.add_argument("--datum", required=True, help="Ausführungsdatum")
    zs.add_parser("list")
    c = zs.add_parser("bezahlt", help="ausgeführten Zahlungslauf verbuchen")
    c.add_argument("datei")
    c.add_argument("--datum")

    s = sub.add_parser("bank", help="Kontoauszüge (camt.053)")
    bs = s.add_subparsers(dest="sub", required=True)
    c = bs.add_parser("import", help="camt.053-Datei importieren und abgleichen")
    c.add_argument("datei")
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
    c = bs.add_parser("kontieren", help="Gegenkonten mit Jev (TypeSafe) vorschlagen")
    c.add_argument("ids", nargs="*", help="nur diese Bewegungen")
    c.add_argument("--schwelle", type=float, help="Konfidenz ab der ein Vorschlag entsteht (Standard aus Einstellungen)")

    s = sub.add_parser("mwst", help="MWST-Abrechnung")
    ms = s.add_subparsers(dest="sub", required=True)
    c = ms.add_parser("abrechnung", help="Ziffern für die ESTV-Abrechnung")
    c.add_argument("periode", help="z.B. 2026-Q1 oder 2026-S1")
    c = ms.add_parser("buchen", help="Abrechnung buchen (MWST-Konten auf Abrechnungskonto)")
    c.add_argument("periode")
    c = ms.add_parser("export", help="eMWST-Datei (eCH-0217) fürs ESTV-Portal")
    c.add_argument("periode")
    c.add_argument("--korrektur", action="store_true", help="als Korrekturabrechnung")
    c.add_argument("--out")
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
    s.add_argument("--config", help="Verzeichnis mit users.yaml und secret (Standard ~/.config/batzen)")
    s.add_argument("--https", action="store_true", help="Cookies nur über HTTPS senden (empfohlen)")
    s = sub.add_parser("user", help="Benutzer für batzen serve")
    us = s.add_subparsers(dest="sub", required=True)
    for name in ("add", "list", "passwort", "remove"):
        c = us.add_parser(name)
        c.add_argument("--config")
        if name != "list":
            c.add_argument("name")
        if name == "add":
            c.add_argument("--rolle", choices=["lesen", "buchhaltung", "admin"], default="buchhaltung")
            c.add_argument("--anzeige", default="", help="Name im Änderungsverlauf, z.B. 'Benjamin Thomet'")
        if name in ("add", "passwort"):
            c.add_argument("--passwort-stdin", action="store_true", help="Passwort von stdin lesen (für Skripte)")
    s = sub.add_parser("ui", help="Oberfläche im Browser starten (lokal)")
    s.add_argument("--port", type=int, default=5151)
    s.add_argument("--kein-browser", action="store_true", help="Browser nicht automatisch öffnen")
    return p


def dispatch(a, book_path: Path | None):
    def book() -> Book:
        return Book(find_root(book_path))

    c = a.cmd
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
        return api.add_account(book(), a.nr, a.name, a.klasse, a.gruppe)
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
        return api.post_entry(book(), a.datum, a.soll, a.haben, a.betrag, a.text, a.beleg, a.datei, a.mwst)
    if c == "book-split":
        return api.post_split(book(), a.datum, a.text, json.loads(a.zeilen), a.beleg, a.datei)
    if c == "reverse":
        return api.reverse_entry(book(), a.beleg, a.datum, a.text)
    if c == "propose":
        return api.propose(book(), a.datum, a.soll, a.haben, a.betrag, a.text, a.begruendung, a.datei, a.mwst)
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
        if a.sub == "run":
            inputs = {k: getattr(a, k) for k in ("stunden", "bvg", "kinderzulagen", "korrektur", "korrektur_text",
                                                 "qst_satzbestimmend", "qst_gesamtpensum") if getattr(a, k) is not None}
            if inputs and not a.mitarbeiter:
                raise BookError("Eingaben (--stunden, …) nur zusammen mit --mitarbeiter")
            return api.payroll_run(b, a.monat, a.mitarbeiter, inputs or None)
        if a.sub == "show":
            return api.payslip_show(b, a.monat, a.mitarbeiter)
        if a.sub == "close":
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
                                                 "referenz_typ", "mitteilung", "iban", "datei") if getattr(a, k) is not None}
            return api.bill_add(b, a.lieferant, a.betrag, **fields)
        if a.sub == "pay":
            return api.bill_pay(b, a.nr, a.datum, a.betrag)
        if a.sub == "void":
            return api.bill_void(b, a.nr, a.grund)
        if a.sub == "offen":
            return api.payables(b)
    if c == "zahlungslauf":
        b = book()
        if a.sub == "erstellen":
            return api.payment_run(b, a.nummern, a.datum)
        if a.sub == "list":
            from . import kreditoren as kred
            return api.jsonable(kred.runs(b))
        return api.payment_run_book(b, a.datei, a.datum)
    if c == "bank":
        b = book()
        if a.sub == "import":
            return api.bank_import(b, a.datei)
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
        return api.bank_reconciliation(b)
    if c == "mwst":
        if a.sub == "abrechnung":
            return api.mwst_report(book(), a.periode)
        if a.sub == "export":
            return api.mwst_export(book(), a.periode, a.korrektur, a.out)
        return api.mwst_book(book(), a.periode)
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
    if cmd == "mwst" and "ziffern" in result:
        z = result["ziffern"]
        out = [f"MWST-Abrechnung {result['periode']} ({result['methode']}) · {result['von']} – {result['bis']}"]
        for key in sorted(z, key=lambda k: (k[:3], k)):
            out.append(f"  Ziffer {key:<12}{_fmt(z[key]):>14}")
        out.append(f"  {'Zahllast' if Decimal(result['zahllast']) >= 0 else 'Guthaben':<19}{_fmt(abs(Decimal(result['zahllast']))):>14}"
                   + ("   (gebucht)" if result["gebucht"] else ""))
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
        os.environ["BATZEN_NO_COMMIT"] = "1"
    if a.cmd == "mcp":
        from .mcp_server import serve
        if a.buch:
            os.environ["BATZEN_BUCH"] = a.buch
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
            print(f"✗ Für den Server fehlen Pakete ({exc.name}): pip install 'batzen[ui]'", file=sys.stderr)
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
            print(f"✗ Für die Oberfläche fehlen Pakete ({exc.name}): pip install 'batzen[ui]'", file=sys.stderr)
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
                print(f"batzen check: {p['ort']}: {p['meldung']}", file=sys.stderr)
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

"""Revolut-Kontoauszüge (CSV, Business und Privat) für den Bankimport.

Revolut liefert kein camt.053, sondern CSV. Dieses Plugin liest beide Exporte
(Business: «Date completed (UTC)», «Payment currency», «Amount», «Fee», «Balance»;
Privat: «Completed Date», «Currency», «Amount», «Fee», «Balance») und macht daraus
Kontoauszüge, die allkvitt wie camt.053 importiert: automatische Zuordnung,
offene Posten, Saldoabstimmung.

Konfiguration im Buch (allkvitt.yaml), je Währung ein Konto:

    revolut:
      konten: {CHF: "1022"}

oder `allkvitt revolut-konto CHF 1022`. Gebühren werden als eigene Bewegung
importiert (zum Buchen auf Bankspesen). Nur abgeschlossene Transaktionen.
"""
from __future__ import annotations

import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation

from allkvitt import api
from allkvitt.book import BookError
from allkvitt.files import parse_date
from allkvitt.plugins import BankFormat, Command, Finding, hookimpl

ALLKVITT_PLUGIN_API = 1
__version__ = "0.1.0"

# Column names of the two exports (first match wins), compared case-insensitively.
COLUMNS = {
    "datum": ["date completed (utc)", "completed date", "date completed", "date started (utc)", "started date"],
    "betrag": ["amount"],
    "gebuehr": ["fee"],
    "waehrung": ["payment currency", "currency"],
    "text": ["description"],
    "referenz": ["reference"],
    "gegenpartei": ["payer", "beneficiary", "counterparty"],
    "id": ["id"],
    "status": ["state"],
    "saldo": ["balance"],
    "typ": ["type"],
}
DONE = {"completed", "abgeschlossen", ""}


def _columns(header: list[str]) -> dict[str, str]:
    lower = {h.strip().lower(): h for h in header}
    found = {}
    for key, names in COLUMNS.items():
        for name in names:
            if name in lower:
                found[key] = lower[name]
                break
    return found


def _rows(data: bytes) -> tuple[dict[str, str], list[dict]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:2000]
    dialect = csv.Sniffer().sniff(sample, delimiters=",;") if sample else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return _columns(reader.fieldnames or []), list(reader)


def detect(filename: str, data: bytes) -> bool:
    if not filename.lower().endswith(".csv"):
        return False
    try:
        cols, _ = _rows(data[:4000])
    except csv.Error:
        return False
    return {"datum", "betrag", "waehrung", "text"} <= set(cols) and "gebuehr" in cols


def _num(value: str) -> Decimal:
    raw = (value or "").strip().replace("'", "").replace(" ", "")
    if not raw:
        return Decimal(0)
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise BookError(f"Revolut-CSV: Betrag '{value}' unlesbar") from None


def accounts(book) -> dict[str, str]:
    return {str(k).upper(): str(v) for k, v in ((book.settings.get("revolut") or {}).get("konten") or {}).items()}


def parse(data: bytes, book) -> list[dict]:
    cols, rows = _rows(data)
    mapping = accounts(book)
    by_currency: dict[str, list[dict]] = {}
    for n, r in enumerate(rows, 1):
        status = (r.get(cols.get("status", ""), "") or "").strip().lower()
        if status not in DONE:
            continue                        # pending, declined, reverted …
        currency = (r[cols["waehrung"]] or "").strip().upper()
        when = parse_date((r[cols["datum"]] or "").strip()[:10], "Revolut-Datum")
        amount, fee = _num(r[cols["betrag"]]), _num(r.get(cols.get("gebuehr", ""), ""))
        rid = (r.get(cols.get("id", ""), "") or "").strip() or hashlib.sha1(repr(sorted(r.items())).encode()).hexdigest()[:12]
        base = {"datum": when, "gegenpartei": (r.get(cols.get("gegenpartei", ""), "") or "").strip(),
                "referenz_typ": "", "referenz": (r.get(cols.get("referenz", ""), "") or "").strip(),
                "endtoend": "", "_saldo": r.get(cols.get("saldo", ""), "")}
        text = " · ".join(t for t in ((r[cols["text"]] or "").strip(), (r.get(cols.get("typ", ""), "") or "").strip().title()) if t)
        entries = by_currency.setdefault(currency, [])
        if amount:
            entries.append({**base, "betrag": amount, "text": text[:200], "bankref": rid, "position": str(n)})
        if fee:
            # Fee columns are positive amounts charged on top (Business sometimes signs them negative).
            entries.append({**base, "betrag": -abs(fee), "text": f"Revolut-Gebühr: {text}"[:200], "gegenpartei": "Revolut",
                            "referenz": "", "bankref": f"{rid}-fee", "position": f"{n}f"})
    if not by_currency:
        raise BookError("Revolut-CSV ohne abgeschlossene Transaktionen")
    statements = []
    for currency, entries in sorted(by_currency.items()):
        if currency not in mapping:
            raise BookError(f"Revolut-Konto in {currency} ist keinem Buchhaltungskonto zugeordnet: "
                            f"allkvitt revolut-konto {currency} <konto> (z.B. 1022)")
        entries.sort(key=lambda e: e["datum"])
        last = entries[-1]
        closing = _num(last["_saldo"]) if last["_saldo"] else None
        first, end = entries[0]["datum"].isoformat(), last["datum"].isoformat()
        for e in entries:
            e.pop("_saldo", None)
        statements.append({"id": f"REVOLUT-{currency}-{first}-{end}", "konto": mapping[currency], "iban": "",
                           "von": first, "bis": end, "eroeffnung": None, "schluss": closing,
                           "schluss_datum": end if closing is not None else None, "buchungen": entries})
    return statements


@hookimpl
def allkvitt_bank_formats():
    return [BankFormat(name="revolut", label="Revolut CSV (Business und Privat)", suffixes=(".csv",),
                       detect=detect, parse=parse)]


@hookimpl
def allkvitt_check(book, rows):
    if not accounts(book):
        return [Finding("hinweis", "allkvitt.yaml", "Revolut-Plugin eingeschaltet, aber kein Konto zugeordnet "
                        "(allkvitt revolut-konto CHF 1022)")]
    return [Finding("warnung", "allkvitt.yaml", f"Revolut {cur}: Konto {nr} fehlt im Kontenplan")
            for cur, nr in accounts(book).items() if nr not in book.accounts]


def _assign(book, currency: str, konto: str):
    currency = currency.upper()
    acct = book.account(konto)
    if not acct.is_balance_sheet or acct.klasse != "aktiv":
        raise BookError(f"Konto {konto} ist kein Aktivkonto")
    if acct.waehrung != currency:
        raise BookError(f"Konto {konto} führt {acct.waehrung}, nicht {currency} — für {currency} ein Konto mit "
                        f"dieser Währung anlegen (allkvitt account-add … --waehrung {currency})")
    cfg = dict(book.settings.get("revolut") or {})
    cfg["konten"] = {**accounts(book), currency: konto}
    book.settings.data["revolut"] = cfg
    book.save_settings()
    return cfg["konten"], [book.root / "allkvitt.yaml"]


def _setup(parser):
    parser.add_argument("waehrung", help="z.B. CHF")
    parser.add_argument("konto", help="Buchhaltungskonto, z.B. 1022")


@hookimpl
def allkvitt_commands():
    return [Command("revolut-konto", "Revolut-Währungskonto einem Buchhaltungskonto zuordnen",
                    lambda book, a: api.write(book, f"Revolut {a.waehrung.upper()} → Konto {a.konto}", _assign,
                                              a.waehrung, a.konto), _setup)]


@hookimpl
def allkvitt_instructions():
    return ("- Revolut: Kontoauszüge kommen als CSV (Revolut → Statement → CSV) und werden wie camt.053 mit "
            "dem Bankimport eingelesen; Gebühren erscheinen als eigene Bewegung (Gegenkonto Bankspesen).")

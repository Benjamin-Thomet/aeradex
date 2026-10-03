"""Customers, invoices, payments and credit notes.

The invoice is a financial record, so it is frozen when issued: positions,
totals, the debtor address and the QR reference are written into its file
together with a fingerprint, and `check` refuses any later change. To correct
an issued invoice you void it (only while unpaid and unlocked) or credit it.

Payments and credit notes are journal rows whose `Quelle` names the invoice
(`zahlung:R-2026-0001`, `gutschrift:R-2026-0001`); the invoice's open amount
is derived from them, so the journal stays the single source of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import qrbill_ch as qr
from .book import Book, BookError, Row
from .files import (CENT, parse_amount, parse_date, read_frontmatter, slug,
                    write_frontmatter)
from .journal import ensure_open, next_beleg, post

ZERO = Decimal("0")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------- Customers ----------

def customers(book: Book) -> dict[str, dict]:
    out = {}
    folder = book.root / "kunden"
    for path in sorted(folder.glob("*.md")) if folder.exists() else []:
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        meta["_notizen"] = body.strip()
        out[str(meta.get("nummer"))] = meta
    return out


def customer(book: Book, nr: str) -> dict:
    found = customers(book).get(str(nr))
    if found is None:
        raise BookError(f"Kunde {nr} nicht gefunden")
    return found


def add_customer(book: Book, name: str, firma: str = "", strasse: str = "", nr: str = "",
                 plz: str = "", ort: str = "", land: str = "CH", email: str = "",
                 rechnung_an: str = "firma", stundensatz=None, notizen: str = "") -> tuple[dict, Path]:
    existing = customers(book)
    nums = [int(k[1:]) for k in existing if re.fullmatch(r"K\d+", k)]
    number = f"K{max(nums, default=0) + 1:04d}"
    meta = {"nummer": number, "name": name.strip(), "firma": firma.strip(),
            "rechnung_an": rechnung_an,
            "adresse": {"strasse": strasse, "nr": str(nr), "plz": str(plz), "ort": ort, "land": land},
            "email": email}
    if stundensatz is not None:
        meta["stundensatz"] = money(stundensatz)
    path = book.root / "kunden" / f"{number}-{slug(firma or name)}.md"
    write_frontmatter(path, meta, notizen)
    return meta, path


# ---------- Invoices ----------

FROZEN = ("nummer", "kunde", "an", "datum", "faellig", "waehrung", "positionen", "total",
          "referenz_typ", "referenz", "debitorenkonto")


def fingerprint(meta: dict) -> str:
    frozen = {k: meta.get(k) for k in FROZEN}
    blob = json.dumps(frozen, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def invoice_paths(book: Book) -> list[Path]:
    folder = book.root / "rechnungen"
    return sorted(folder.glob("*/R-*.md")) if folder.exists() else []


def invoices(book: Book) -> dict[str, dict]:
    out = {}
    for path in invoice_paths(book):
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        meta["_text"] = body.strip()
        out[str(meta.get("nummer"))] = meta
    return out


def invoice(book: Book, nr: str) -> dict:
    found = invoices(book).get(nr)
    if found is None:
        raise BookError(f"Rechnung {nr} nicht gefunden")
    return found


def next_invoice_number(book: Book, year: int) -> str:
    prefix = f"R-{year}-"
    nums = [int(k[len(prefix):]) for k in invoices(book) if k.startswith(prefix) and k[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:04d}"


def _snapshot_address(cust: dict) -> dict:
    a = cust.get("adresse") or {}
    return {"name": qr.invoice_name(cust), "zusatz": cust.get("name") if cust.get("firma") and cust.get("rechnung_an") != "person" else "",
            "strasse": a.get("strasse") or "", "nr": str(a.get("nr") or ""),
            "plz": str(a.get("plz") or ""), "ort": a.get("ort") or "", "land": a.get("land") or "CH"}


def normalize_positions(raw: list[dict], default_konto: str) -> list[dict]:
    out = []
    for i, p in enumerate(raw, 1):
        text = str(p.get("text") or "").strip()
        if not text:
            raise BookError(f"Position {i}: Text fehlt")
        menge = Decimal(str(p.get("menge", 1)))
        preis = parse_amount(p.get("preis", 0), f"Position {i}")
        betrag = money(menge * preis) if p.get("betrag") is None else money(p["betrag"])
        if betrag != money(menge * preis):
            raise BookError(f"Position {i}: Betrag {betrag} ≠ Menge × Preis {money(menge * preis)}")
        out.append({"text": text, "menge": menge, "einheit": str(p.get("einheit") or ""),
                    "preis": money(preis), "betrag": betrag,
                    "konto": str(p.get("konto") or default_konto)})
    if not out:
        raise BookError("Eine Rechnung braucht mindestens eine Position")
    return out


def issue_invoice(book: Book, kunde: str, positionen: list[dict], datum=None, text: str = "",
                  zahlungsfrist: int | None = None) -> tuple[dict, list[Path]]:
    """Issue and book an invoice: DR Debitoren / CR Ertrag (per position account)."""
    cust = customer(book, kunde)
    d = parse_date(datum, "datum") if datum else date.today()
    ensure_open(book, d)
    s = book.settings
    pos = normalize_positions(positionen, s.konto("ertrag"))
    for p in pos:
        book.account(p["konto"])
    total = sum((p["betrag"] for p in pos), ZERO)
    if total <= 0:
        raise BookError("Rechnungstotal muss positiv sein")
    nummer = next_invoice_number(book, d.year)
    iban = qr.normalize_iban(s.get("iban"))
    ref_type = qr.reference_type_for(iban) if iban else "NON"
    frist = int(zahlungsfrist if zahlungsfrist is not None else s.get("zahlungsfrist_tage") or 30)
    meta = {
        "nummer": nummer, "kunde": cust["nummer"], "an": _snapshot_address(cust),
        "datum": d.isoformat(), "faellig": (d + timedelta(days=frist)).isoformat(),
        "waehrung": s.get("waehrung") or "CHF",
        "positionen": [{**p, "menge": _num(p["menge"])} for p in pos],
        "total": total, "referenz_typ": ref_type,
        "referenz": qr.make_reference(ref_type, nummer, s.get("qr_referenz_praefix") or ""),
        "debitorenkonto": s.konto("debitoren"),
        "status": "aktiv",
    }
    meta["fingerprint"] = fingerprint(_canonical(meta))
    path = book.root / "rechnungen" / str(d.year) / f"{nummer}.md"
    rows = booking_rows(meta)
    touched = post(book, rows)
    write_frontmatter(path, meta, text)
    return meta, touched + [path]


def _num(value: Decimal):
    """Quantities stay as typed: 10 not 10.00, 1.5 not 1.50."""
    value = Decimal(str(value))
    return int(value) if value == value.to_integral() else float(value)


def _canonical(meta: dict) -> dict:
    """Normalise numbers so the fingerprint is the same before writing and after
    reading the YAML back (Decimal vs float, int vs '10')."""
    out = dict(meta)
    out["total"] = f"{money(meta.get('total')):.2f}"
    out["positionen"] = [{**p, "menge": format(Decimal(str(p.get("menge"))).normalize(), "f"),
                          "preis": f"{money(p.get('preis')):.2f}", "betrag": f"{money(p.get('betrag')):.2f}"}
                         for p in meta.get("positionen") or []]
    for key in ("datum", "faellig"):
        out[key] = str(meta.get(key))
    return out


def invoice_fingerprint_ok(meta: dict) -> bool:
    return meta.get("fingerprint") == fingerprint(_canonical(meta))


def booking_rows(meta: dict) -> list[Row]:
    """The journal rows an active invoice owns. One row when every position
    goes to the same revenue account, else a split booking."""
    nummer = meta["nummer"]
    d = parse_date(meta["datum"])
    per_konto: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in meta["positionen"]:
        per_konto[str(p.get("konto"))] += money(p["betrag"])
    name = (meta.get("an") or {}).get("name") or meta.get("kunde")
    text = f"Rechnung {nummer} – {name}"
    quelle = f"rechnung:{nummer}"
    deb = str(meta.get("debitorenkonto"))
    if len(per_konto) == 1:
        konto, amount = next(iter(per_konto.items()))
        return [Row(d, nummer, text, deb, konto, amount, quelle)]
    rows = [Row(d, nummer, text, deb, "", money(meta["total"]), quelle)]
    rows += [Row(d, nummer, text, "", konto, amount, quelle) for konto, amount in per_konto.items()]
    return rows


def settlements(book: Book) -> dict[str, list[Row]]:
    """Payment and credit-note rows per invoice number."""
    out: dict[str, list[Row]] = defaultdict(list)
    for r in book.rows:
        kind, _, nr = r.quelle.partition(":")
        if kind in ("zahlung", "gutschrift") and nr:
            out[nr].append(r)
    return out


def invoice_state(book: Book, meta: dict, paid_rows: list[Row] | None = None,
                  as_of: date | None = None) -> dict:
    if paid_rows is None:
        paid_rows = settlements(book).get(meta["nummer"], [])
    rows = [r for r in paid_rows if as_of is None or r.datum <= as_of]
    paid = sum((r.betrag for r in rows if r.quelle.startswith("zahlung:")), ZERO)
    credited = sum((r.betrag for r in rows if r.quelle.startswith("gutschrift:")), ZERO)
    total = money(meta.get("total"))
    open_amount = total - paid - credited if meta.get("status") != "storniert" else ZERO
    state = ("storniert" if meta.get("status") == "storniert" else
             "bezahlt" if open_amount <= 0 else "teilbezahlt" if paid or credited else "offen")
    return {"nummer": meta["nummer"], "kunde": meta.get("kunde"),
            "name": (meta.get("an") or {}).get("name"), "datum": str(meta.get("datum")),
            "faellig": str(meta.get("faellig")), "total": total, "bezahlt": paid,
            "gutgeschrieben": credited, "offen": open_amount, "status": state,
            "referenz": meta.get("referenz") or ""}


def void_invoice(book: Book, nr: str, grund: str = "") -> tuple[dict, list[Path]]:
    meta = invoice(book, nr)
    if meta.get("status") == "storniert":
        raise BookError(f"{nr} ist bereits storniert")
    ensure_open(book, parse_date(meta["datum"]))
    if settlements(book).get(nr):
        raise BookError(f"{nr} hat Zahlungen oder Gutschriften — stattdessen gutschreiben "
                        "(batzen invoice credit) oder die Zahlungen zuerst stornieren")
    touched = book.remove_rows(lambda r: r.quelle == f"rechnung:{nr}")
    path = meta.pop("_pfad")
    text = meta.pop("_text", "")
    meta["status"] = "storniert"
    meta["storniert_am"] = date.today().isoformat()
    if grund:
        meta["storno_grund"] = grund
    write_frontmatter(path, meta, text)
    return meta, touched + [path]


def _settle(book: Book, nr: str, kind: str, betrag, datum, konto: str, text: str) -> tuple[Row, list[Path]]:
    meta = invoice(book, nr)
    if meta.get("status") == "storniert":
        raise BookError(f"{nr} ist storniert")
    state = invoice_state(book, meta)
    amount = money(betrag) if betrag not in (None, "") else state["offen"]
    if amount <= 0:
        raise BookError(f"{nr} ist bereits beglichen" if state["offen"] <= 0 else "Betrag muss positiv sein")
    if amount > state["offen"]:
        raise BookError(f"Betrag {amount} übersteigt den offenen Betrag {state['offen']} von {nr}")
    d = parse_date(datum, "datum") if datum else date.today()
    row = Row(d, next_beleg(book, d.year), text, konto, str(meta.get("debitorenkonto")),
              amount, f"{kind}:{nr}")
    return row, post(book, [row])


def pay_invoice(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None) -> tuple[Row, list[Path]]:
    """Book a (partial) payment: DR Bank / CR Debitoren."""
    konto = konto or book.settings.konto("bank")
    return _settle(book, nr, "zahlung", betrag, datum, konto, f"Zahlung Rechnung {nr}")


def credit_invoice(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None,
                   grund: str = "") -> tuple[Row, list[Path]]:
    """Gutschrift: write off (part of) the open receivable, DR Erlösminderung / CR Debitoren."""
    konto = konto or book.settings.konto("gutschrift")
    text = f"Gutschrift zu Rechnung {nr}" + (f": {grund}" if grund else "")
    return _settle(book, nr, "gutschrift", betrag, datum, konto, text)


def find_match(book: Book, betrag, text: str) -> tuple[dict | None, str | None]:
    """Guess which open invoice a bank credit pays: QR/SCOR reference in the text,
    then the invoice number, then an amount equal to the open or full total."""
    states = [invoice_state(book, m) for m in invoices(book).values()]
    candidates = [s for s in states if s["status"] in ("offen", "teilbezahlt")]
    text = text or ""
    alnum = re.sub(r"[^0-9A-Z]", "", text.upper())
    digits = re.sub(r"\D", "", text)
    for s in candidates:
        ref = re.sub(r"[^0-9A-Z]", "", s["referenz"].upper())
        if ref and (ref in alnum or (ref.isdigit() and ref in digits)):
            return s, "referenz"
    for s in candidates:
        if s["nummer"].upper() in text.upper():
            return s, "nummer"
    try:
        target = money(betrag)
    except Exception:
        return None, None
    hits = [s for s in candidates if target in (s["offen"], s["total"])]
    if hits:
        hits.sort(key=lambda s: s["datum"])
        return hits[0], ("betrag" if len(hits) == 1 else "betrag_mehrdeutig")
    return None, None


AGE_BUCKETS = [("0–30", 0, 30), ("31–60", 31, 60), ("61–90", 61, 90), (">90", 91, 10 ** 9)]


def aged_receivables(book: Book, as_of: date | None = None) -> dict:
    """Offene Posten per Stichtag, aged by invoice date, reconciled with the Debitoren account."""
    from .ledger import BalanceEngine

    as_of = as_of or date.today()
    paid = settlements(book)
    rows, buckets, total = [], {b[0]: ZERO for b in AGE_BUCKETS}, ZERO
    for meta in invoices(book).values():
        issued = parse_date(meta["datum"])
        if issued > as_of or meta.get("status") == "storniert":
            continue
        st = invoice_state(book, meta, paid.get(meta["nummer"], []), as_of)
        if st["offen"] <= 0:
            continue
        age = (as_of - issued).days
        label = next(b[0] for b in AGE_BUCKETS if b[1] <= age <= b[2])
        buckets[label] += st["offen"]
        total += st["offen"]
        rows.append({**st, "alter_tage": age, "kategorie": label})
    rows.sort(key=lambda r: (r["datum"], r["nummer"]))
    deb = book.settings.konto("debitoren")
    saldo = BalanceEngine(book).balance_at(deb, as_of)
    return {"stichtag": as_of, "posten": rows, "kategorien": buckets, "total_offen": total,
            "debitorenkonto": deb, "saldo_debitoren": saldo, "differenz": saldo - total}

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
# Added with MWST support; part of the fingerprint only when present, so
# invoices issued before keep their fingerprint.
FROZEN_OPTIONAL = ("mwst", "mwst_methode", "mwst_konto", "netto", "extern", "kurs")
QR_CURRENCIES = ("CHF", "EUR")          # what a Swiss QR-bill can carry


def fingerprint(meta: dict) -> str:
    frozen = {k: meta.get(k) for k in FROZEN}
    frozen.update({k: meta.get(k) for k in FROZEN_OPTIONAL if k in meta})
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


def normalize_positions(raw: list[dict], default_konto: str, default_mwst: str = "") -> list[dict]:
    from . import mwst as vat
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
        item = {"text": text, "menge": menge, "einheit": str(p.get("einheit") or ""),
                "preis": money(preis), "betrag": betrag,
                "konto": str(p.get("konto") or default_konto)}
        code = str(p.get("mwst") if p.get("mwst") is not None else default_mwst).strip().upper()
        if code:
            c = vat.code(code)
            if c.kind not in ("umsatz", "befreit", "ausgenommen"):
                raise BookError(f"Position {i}: {code} ist ein Vorsteuer-Code, auf Rechnungen nur U81, U26, U38, U0, UA")
            item["mwst"] = c.code
        out.append(item)
    if not out:
        raise BookError("Eine Rechnung braucht mindestens eine Position")
    return out


def mwst_breakdown(positions: list[dict]) -> list[dict]:
    """Net and tax per MWST code — tax is computed on the net sum per rate."""
    from . import mwst as vat
    per: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in positions:
        if p.get("mwst"):
            per[p["mwst"]] += money(p["betrag"])
    out = []
    for c, net in per.items():
        rate = vat.CODES[c].rate
        out.append({"code": c, "satz": rate, "netto": net, "steuer": vat.tax_from_net(net, rate) if rate else ZERO})
    return out


def currency(meta: dict) -> str:
    return str(meta.get("waehrung") or "CHF").upper()


def is_foreign(meta: dict) -> bool:
    return currency(meta) != "CHF"


def _rate(book: Book, cur: str, d: date, kurs=None) -> Decimal | None:
    if cur == "CHF":
        return None
    if not re.fullmatch(r"[A-Z]{3}", cur):
        raise BookError(f"Währung '{cur}' ist kein ISO-Code")
    from . import fx
    rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
    if rate <= 0:
        raise BookError("Kurs muss positiv sein")
    return rate


def _to_chf(rows: list[Row], cur: str, rate: Decimal) -> list[Row]:
    """Rows in the invoice currency → CHF at the invoice rate; the foreign amount stays on each row.
    The rounding Rappen go to the largest revenue line (never the receivable)."""
    from .journal import exact_rate
    for r in rows:
        r.waehrung, r.fw, r.kurs = cur, r.betrag, rate
        r.betrag = money(r.betrag * rate)
    soll = sum((r.betrag for r in rows if r.soll), ZERO)
    haben = sum((r.betrag for r in rows if r.haben), ZERO)
    if soll != haben:
        side = [r for r in rows if r.haben and not r.soll] or [r for r in rows if r.haben]
        target = max(side, key=lambda r: r.betrag)
        target.betrag += soll - haben
        exact_rate(target)
    return rows


def issue_invoice(book: Book, kunde: str, positionen: list[dict], datum=None, text: str = "",
                  zahlungsfrist: int | None = None, waehrung: str = "", kurs=None) -> tuple[dict, list[Path]]:
    """Issue and book an invoice: DR Debitoren / CR Ertrag (per position account),
    with MWST per rate when the book is MWST-pflichtig."""
    from . import mwst as vat
    cust = customer(book, kunde)
    d = parse_date(datum, "datum") if datum else date.today()
    ensure_open(book, d)
    s = book.settings
    cfg = vat.config(book)
    pos = normalize_positions(positionen, s.konto("ertrag"), "U81" if cfg["methode"] != "keine" else "")
    if cfg["methode"] == "keine" and any(p.get("mwst") for p in pos):
        raise BookError("Dieses Buch ist nicht MWST-pflichtig — Positionen ohne MWST-Code erfassen")
    for p in pos:
        book.account(p["konto"])
    cur = (waehrung or s.get("waehrung") or "CHF").strip().upper()
    if cur not in QR_CURRENCIES:
        raise BookError(f"Eine QR-Rechnung gibt es nur in CHF oder EUR, nicht in {cur}")
    rate = _rate(book, cur, d, kurs)
    netto = sum((p["betrag"] for p in pos), ZERO)
    breakdown = mwst_breakdown(pos)
    total = netto + sum((b["steuer"] for b in breakdown), ZERO)
    if total <= 0:
        raise BookError("Rechnungstotal muss positiv sein")
    nummer = next_invoice_number(book, d.year)
    iban = qr.normalize_iban(s.get("iban"))
    ref_type = qr.reference_type_for(iban) if iban else "NON"
    frist = int(zahlungsfrist if zahlungsfrist is not None else s.get("zahlungsfrist_tage") or 30)
    meta = {
        "nummer": nummer, "kunde": cust["nummer"], "an": _snapshot_address(cust),
        "datum": d.isoformat(), "faellig": (d + timedelta(days=frist)).isoformat(),
        "waehrung": cur,
        "positionen": [{**p, "menge": _num(p["menge"])} for p in pos],
        "total": total, "referenz_typ": ref_type,
        "referenz": qr.make_reference(ref_type, nummer, s.get("qr_referenz_praefix") or ""),
        "debitorenkonto": s.konto("debitoren"),
        "status": "aktiv",
    }
    if breakdown:
        meta.update({"netto": netto, "mwst": breakdown, "mwst_methode": cfg["methode"],
                     "mwst_konto": cfg["konten"]["umsatzsteuer"]})
    if rate is not None:
        meta["kurs"] = str(rate)
    meta["fingerprint"] = fingerprint(_canonical(meta))
    path = book.root / "rechnungen" / str(d.year) / f"{nummer}.md"
    rows = booking_rows(meta)
    touched = post(book, rows)
    write_frontmatter(path, meta, text)
    return meta, touched + [path]


def record_external(book: Book, kunde: str, betrag, datum=None, faellig=None, rechnungsnr: str = "",
                    referenz: str = "", referenz_typ: str = "", konto: str = "", mwst: str = "",
                    datei: str = "", text: str = "", waehrung: str = "", kurs=None) -> tuple[dict, list[Path]]:
    """Book an invoice that was issued outside batzen (Word, another program, by hand), so it
    is an open item like any other: payments, bank matching, credit notes, Mahnungen.
    `betrag` is the gross total as on the invoice; with a MWST code the tax is taken out of it."""
    from . import mwst as vat
    from .journal import attach
    cust = customer(book, kunde)
    d = parse_date(datum, "datum") if datum else date.today()
    ensure_open(book, d)
    s = book.settings
    gross = parse_amount(betrag, "betrag")
    cur = (waehrung or "CHF").strip().upper()
    rate = _rate(book, cur, d, kurs)
    if gross <= 0 or gross != money(gross):
        raise BookError("Betrag muss positiv sein und höchstens zwei Nachkommastellen haben")
    konto = str(konto or s.konto("ertrag"))
    book.account(konto)
    cfg = vat.config(book)
    code = (mwst or "").strip().upper()
    net, tax = gross, ZERO
    if code:
        c = vat.code(code)
        if c.kind not in ("umsatz", "befreit", "ausgenommen"):
            raise BookError(f"{code} ist kein Umsatz-Code (U81, U26, U38, U0, UA)")
        if cfg["methode"] == "keine":
            raise BookError("Dieses Buch ist nicht MWST-pflichtig — ohne MWST-Code erfassen")
        if c.rate:
            tax = vat.tax_from_gross(gross, c.rate)
            net = gross - tax
    nummer = next_invoice_number(book, d.year)
    label = f"Rechnung {rechnungsnr}" if rechnungsnr else "Rechnung"
    position = {"text": f"{label} (ausserhalb von batzen erstellt)", "menge": 1, "einheit": "", "preis": net,
                "betrag": net, "konto": konto, **({"mwst": code} if code else {})}
    frist = int(s.get("zahlungsfrist_tage") or 30)
    meta = {
        "nummer": nummer, "kunde": cust["nummer"], "an": _snapshot_address(cust),
        "datum": d.isoformat(),
        "faellig": (parse_date(faellig, "faellig") if faellig else d + timedelta(days=frist)).isoformat(),
        "waehrung": cur, "positionen": [position], "total": gross,
        "referenz_typ": (referenz_typ or ("SCOR" if (referenz or "").upper().startswith("RF") else
                                          "QRR" if re.fullmatch(r"\d{27}", referenz or "") else "NON")).upper(),
        "referenz": (referenz or "").replace(" ", ""),
        "debitorenkonto": s.konto("debitoren"), "status": "aktiv",
        "extern": {"rechnungsnr": rechnungsnr or ""},
    }
    if code:
        meta.update({"netto": net, "mwst": [{"code": code, "satz": vat.CODES[code].rate, "netto": net, "steuer": tax}],
                     "mwst_methode": cfg["methode"], "mwst_konto": cfg["konten"]["umsatzsteuer"]})
    if rate is not None:
        meta["kurs"] = str(rate)
    meta["fingerprint"] = fingerprint(_canonical(meta))
    rows = booking_rows(meta)
    touched = post(book, rows)
    if datei:
        source = Path(datei) if Path(datei).is_absolute() else book.root / datei
        target = attach(book, rows[0], source)
        meta["datei"] = str(target.relative_to(book.root))
        touched += [target, source]
    path = book.root / "rechnungen" / str(d.year) / f"{nummer}.md"
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
    if "netto" in meta:
        out["netto"] = f"{money(meta.get('netto')):.2f}"
    if "mwst" in meta:
        out["mwst"] = [{"code": b.get("code"), "satz": f"{Decimal(str(b.get('satz'))):.1f}",
                        "netto": f"{money(b.get('netto')):.2f}", "steuer": f"{money(b.get('steuer')):.2f}"}
                       for b in meta.get("mwst") or []]
    out["positionen"] = [{**p, "menge": format(Decimal(str(p.get("menge"))).normalize(), "f"),
                          "preis": f"{money(p.get('preis')):.2f}", "betrag": f"{money(p.get('betrag')):.2f}"}
                         for p in meta.get("positionen") or []]
    for key in ("datum", "faellig"):
        out[key] = str(meta.get(key))
    return out


def invoice_fingerprint_ok(meta: dict) -> bool:
    return meta.get("fingerprint") == fingerprint(_canonical(meta))


def booking_rows(meta: dict) -> list[Row]:
    """The journal rows an active invoice owns, in CHF (a foreign invoice at its rate)."""
    rows = _booking_rows(meta)
    if is_foreign(meta):
        rows = _to_chf(rows, currency(meta), Decimal(str(meta["kurs"])))
    return rows


def _booking_rows(meta: dict) -> list[Row]:
    """The journal rows an active invoice owns. One row when every position goes
    to the same revenue account without MWST, else a split booking: Debitoren
    gross in Soll; per account and code the net (effective method) or gross
    (Saldo method) in Haben; per code the Umsatzsteuer (effective method)."""
    nummer = meta["nummer"]
    d = parse_date(meta["datum"])
    name = (meta.get("an") or {}).get("name") or meta.get("kunde")
    text = f"Rechnung {nummer} – {name}"
    quelle = f"rechnung:{nummer}"
    deb = str(meta.get("debitorenkonto"))
    per: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for p in meta["positionen"]:
        per[(str(p.get("konto")), str(p.get("mwst") or ""))] += money(p["betrag"])
    total = money(meta["total"])
    if not meta.get("mwst"):
        if len(per) == 1:
            (konto, _), amount = next(iter(per.items()))
            return [Row(d, nummer, text, deb, konto, amount, quelle)]
        rows = [Row(d, nummer, text, deb, "", total, quelle)]
        return rows + [Row(d, nummer, text, "", k, a, quelle) for (k, _), a in per.items()]
    tax = {b["code"]: money(b["steuer"]) for b in meta["mwst"]}
    rows = [Row(d, nummer, text, deb, "", total, quelle)]
    if meta.get("mwst_methode") == "saldo":
        # Revenue stays gross: spread each code's tax over its accounts, last one takes the rounding.
        by_code: dict[str, list] = defaultdict(list)
        for (konto, code), net in per.items():
            by_code[code].append((konto, net))
        for code, items in by_code.items():
            net_sum = sum((n for _, n in items), ZERO)
            remaining = tax.get(code, ZERO)
            for i, (konto, net) in enumerate(items):
                share = remaining if i == len(items) - 1 else money(tax.get(code, ZERO) * net / net_sum) if net_sum else ZERO
                remaining -= share
                rows.append(Row(d, nummer, text, "", konto, net + share, quelle, mwst=code))
        return rows
    for (konto, code), net in per.items():
        rows.append(Row(d, nummer, text, "", konto, net, quelle, mwst=code))
    for code, amount in tax.items():
        if amount:
            rows.append(Row(d, nummer, text, "", str(meta.get("mwst_konto")), amount, quelle, mwst=code))
    return rows


def settlements(book: Book) -> dict[str, list[Row]]:
    """Payment and credit-note rows per invoice number — only the line that
    clears the receivable (a credit note with MWST also has net and tax lines)."""
    out: dict[str, list[Row]] = defaultdict(list)
    receivable = {str(m.get("debitorenkonto")) for m in invoices(book).values()} or {book.settings.konto("debitoren")}
    for r in book.rows:
        kind, _, nr = r.quelle.partition(":")
        if kind in ("zahlung", "gutschrift") and nr and r.haben in receivable:
            out[nr].append(r)
    return out


def invoice_state(book: Book, meta: dict, paid_rows: list[Row] | None = None,
                  as_of: date | None = None) -> dict:
    if paid_rows is None:
        paid_rows = settlements(book).get(meta["nummer"], [])
    rows = [r for r in paid_rows if as_of is None or r.datum <= as_of]
    amount = (lambda r: r.fw or ZERO) if is_foreign(meta) else (lambda r: r.betrag)
    paid = sum((amount(r) for r in rows if r.quelle.startswith("zahlung:")), ZERO)
    credited = sum((amount(r) for r in rows if r.quelle.startswith("gutschrift:")), ZERO)
    total = money(meta.get("total"))
    open_amount = total - paid - credited if meta.get("status") != "storniert" else ZERO
    state = ("storniert" if meta.get("status") == "storniert" else
             "bezahlt" if open_amount <= 0 else "teilbezahlt" if paid or credited else "offen")
    return {"nummer": meta["nummer"], "kunde": meta.get("kunde"),
            "name": (meta.get("an") or {}).get("name"), "datum": str(meta.get("datum")),
            "faellig": str(meta.get("faellig")), "total": total, "bezahlt": paid,
            "gutgeschrieben": credited, "offen": open_amount, "status": state,
            "referenz": meta.get("referenz") or "",
            "extern": (meta.get("extern") or {}).get("rechnungsnr", "") if meta.get("extern") else None,
            "waehrung": currency(meta),
            "offen_chf": (book_value(book, meta, as_of, rows) if is_foreign(meta) else open_amount)
            if meta.get("status") != "storniert" else ZERO}


def book_value(book: Book, meta: dict, until: date | None = None, paid_rows: list[Row] | None = None) -> Decimal:
    """CHF value of what is still owed on a foreign invoice: as booked, plus Stichtag
    revaluations, minus what payments and credit notes cleared — up to `until`."""
    from . import fx
    paid_rows = settlements(book).get(meta["nummer"], []) if paid_rows is None else paid_rows
    deb = str(meta.get("debitorenkonto"))
    value = sum((r.betrag for r in booking_rows(meta) if r.soll == deb), ZERO)
    value += sum((diff for d, diff in fx.invoice_revaluations(book).get(meta["nummer"], [])
                  if until is None or d <= until), ZERO)
    value -= sum((r.betrag for r in paid_rows if until is None or r.datum <= until), ZERO)
    return value


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


def _settle(book: Book, nr: str, kind: str, betrag, datum, konto: str, text: str,
            mwst: str = "", kurs=None, fw=None) -> tuple[Row, list[Path]]:
    meta = invoice(book, nr)
    if meta.get("status") == "storniert":
        raise BookError(f"{nr} ist storniert")
    if is_foreign(meta):
        return _settle_foreign(book, meta, kind, betrag, datum, konto, text, mwst, kurs, fw)
    if kind == "zahlung" and book.account(konto).is_foreign:
        raise BookError(f"Konto {konto} führt {book.account(konto).waehrung}, {nr} lautet auf CHF")
    state = invoice_state(book, meta)
    amount = money(betrag) if betrag not in (None, "") else state["offen"]
    if amount <= 0:
        raise BookError(f"{nr} ist bereits beglichen" if state["offen"] <= 0 else "Betrag muss positiv sein")
    if amount > state["offen"]:
        raise BookError(f"Betrag {amount} übersteigt den offenen Betrag {state['offen']} von {nr}")
    d = parse_date(datum, "datum") if datum else date.today()
    row = Row(d, next_beleg(book, d.year), text, konto, str(meta.get("debitorenkonto")),
              amount, f"{kind}:{nr}")
    if mwst:
        from .mwst import config, split
        if config(book)["methode"] == "effektiv":
            # A credit note reverses revenue: split like a negative sale.
            rows = split(book, Row(d, row.beleg, text, str(meta.get("debitorenkonto")), konto, amount), mwst)
            rows = [Row(r.datum, r.beleg, r.text, r.haben, r.soll, r.betrag, f"{kind}:{nr}", mwst=r.mwst) for r in rows]
            return row, post(book, rows)
        row.mwst = mwst
    return row, post(book, [row])


def _settle_foreign(book: Book, meta: dict, kind: str, betrag, datum, konto: str, text: str, mwst: str,
                    kurs, fw) -> tuple[Row, list[Path]]:
    """A foreign invoice: a credit note reverses at the invoice rate; a payment clears the book
    value and books the difference to what was received as Kursgewinn/-verlust. `betrag` is in
    the receiving account's currency (into a CHF account: the CHF credited)."""
    from . import fx
    nr, cur = meta["nummer"], currency(meta)
    st = invoice_state(book, meta)
    open_fw = st["offen"]
    d = parse_date(datum, "datum") if datum else date.today()
    deb = str(meta.get("debitorenkonto"))
    quelle = f"{kind}:{nr}"
    beleg = next_beleg(book, d.year)
    if kind == "gutschrift":
        amount = money(betrag) if betrag not in (None, "") else open_fw
        if amount <= 0 or amount > open_fw:
            raise BookError(f"Gutschrift {amount} {cur}: offen sind {open_fw} {cur}")
        base = Row(d, beleg, text, deb, konto, amount)
        if mwst:
            from .mwst import config, split
            if config(book)["methode"] == "effektiv":
                rows = [Row(r.datum, r.beleg, r.text, r.haben, r.soll, r.betrag, quelle, mwst=r.mwst)
                        for r in split(book, base, mwst)]
            else:
                rows = [Row(d, beleg, text, konto, deb, amount, quelle, mwst=mwst)]
        else:
            rows = [Row(d, beleg, text, konto, deb, amount, quelle)]
        rows = _to_chf(rows, cur, Decimal(str(meta["kurs"])))
        return next(r for r in rows if r.haben == deb), post(book, rows)
    acct = book.account(konto)
    if acct.is_foreign:
        if acct.waehrung != cur:
            raise BookError(f"Konto {konto} führt {acct.waehrung}, {nr} lautet auf {cur}")
        settle = money(fw if fw not in (None, "") else betrag) if (fw or betrag) not in (None, "") else open_fw
        rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
        received = money(settle * rate)
    else:
        settle = money(fw) if fw not in (None, "") else open_fw
        rate = None
        if betrag not in (None, ""):
            received = money(betrag)                        # what the bank actually credited in CHF
        else:
            rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
            received = money(settle * rate)
    if settle <= 0:
        raise BookError(f"{nr} ist bereits beglichen")
    if settle > open_fw:
        raise BookError(f"{settle} {cur} übersteigt den offenen Betrag {open_fw} {cur} von {nr}")
    value = book_value(book, meta, d)
    clear = value if settle == open_fw else money(value * settle / open_fw)
    rows = [Row(d, beleg, text, "", deb, clear, quelle, waehrung=cur, fw=settle,
                kurs=(clear / settle).quantize(Decimal("1e-10")).normalize())]
    bank_row = Row(d, beleg, text, konto, "", received, quelle)
    if acct.is_foreign:
        bank_row.waehrung, bank_row.fw, bank_row.kurs = cur, settle, rate
    rows.append(bank_row)
    diff = received - clear
    if diff:
        fx._ensure_accounts(book)
    if diff > 0:
        rows.append(Row(d, beleg, f"Kursgewinn {nr}", "", book.settings.konto("kursgewinn"), diff, quelle))
    elif diff < 0:
        rows.append(Row(d, beleg, f"Kursverlust {nr}", book.settings.konto("kursverlust"), "", -diff, quelle))
    return rows[0], post(book, rows)


def amount_fits(book: Book, st: dict, amount: Decimal, konto: str) -> bool:
    """Could a bank credit of `amount` (in the account's currency) pay this invoice?"""
    if st.get("waehrung", "CHF") == "CHF":
        return amount <= st["offen"]
    acct = book.accounts.get(konto)
    if acct is not None and acct.is_foreign:
        return acct.waehrung == st["waehrung"] and amount <= st["offen"]
    return True                                       # CHF credit for a foreign invoice: the rate decides


def pay_invoice(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None, kurs=None,
                fw=None) -> tuple[Row, list[Path]]:
    """Book a (partial) payment: DR Bank / CR Debitoren. `betrag` is in the receiving account's currency."""
    konto = konto or book.settings.konto("bank")
    return _settle(book, nr, "zahlung", betrag, datum, konto, f"Zahlung Rechnung {nr}", kurs=kurs, fw=fw)


def credit_invoice(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None,
                   grund: str = "") -> tuple[Row, list[Path]]:
    """Gutschrift: write off (part of) the open receivable, DR Erlösminderung / CR Debitoren.
    On an invoice with MWST at a single rate the Umsatzsteuer is reduced accordingly."""
    konto = konto or book.settings.konto("gutschrift")
    text = f"Gutschrift zu Rechnung {nr}" + (f": {grund}" if grund else "")
    meta = invoice(book, nr)
    codes = [b["code"] for b in meta.get("mwst") or [] if Decimal(str(b.get("satz") or 0))]
    if len(codes) == 1:
        return _settle(book, nr, "gutschrift", betrag, datum, konto, text, mwst=codes[0])
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
        ext = (s.get("extern") or "").upper()
        if len(ext) >= 4 and re.search(r"(?<![0-9A-Z])" + re.escape(ext) + r"(?![0-9A-Z])", text.upper()):
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
        buckets[label] += st["offen_chf"]
        total += st["offen_chf"]
        rows.append({**st, "alter_tage": age, "kategorie": label})
    rows.sort(key=lambda r: (r["datum"], r["nummer"]))
    deb = book.settings.konto("debitoren")
    saldo = BalanceEngine(book).balance_at(deb, as_of)
    return {"stichtag": as_of, "posten": rows, "kategorien": buckets, "total_offen": total,
            "debitorenkonto": deb, "saldo_debitoren": saldo, "differenz": saldo - total}

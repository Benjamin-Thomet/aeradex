"""Spesen: expenses employees paid themselves, reimbursed with the next payslip.

    spesen/<JJJJ>/SP-JJJJ-NNNN.yaml   one claim: who, when, what, accounts, the receipt

A claim is booked when entered — the expense against the account for owed expenses
(`konten.spesen`, default 2210 Sonstige kurzfristige Verbindlichkeiten), so the books show
what the company owes — and
owns those rows (Quelle spesen:SP-…). The payroll run picks up the employee's
open claims up to the month's end; they are paid out with the net wage (not
subject to social insurance) and booked from 2210 to the payout account when the
payslip is closed. Whether a claim is paid is not stored on the claim: it is the
payslip that lists it, so reopening a payslip makes it open again.

`art`: `reise` (travel, meals, lodging — Lohnausweis 13.1.1) or `uebrige`
(other effective expenses — Lohnausweis 13.1.2, amount and kind).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from .book import Book, BookError, Row
from .files import CENT, parse_amount, parse_date, read_yaml, write_yaml

ARTEN = {"reise": "Reise, Verpflegung, Übernachtung", "uebrige": "Übrige effektive Spesen"}
ZERO = Decimal("0")


def konto(book: Book) -> str:
    return book.settings.konto("spesen")


def claims(book: Book) -> dict[str, dict]:
    folder = book.root / "spesen"
    out = {}
    for path in sorted(folder.glob("*/SP-*.yaml")) if folder.exists() else []:
        meta = read_yaml(path) or {}
        meta["_pfad"] = path
        out[str(meta["nummer"])] = meta
    return out


def claim(book: Book, nr: str) -> dict:
    found = claims(book).get(nr)
    if found is None:
        raise BookError(f"Spesenbeleg {nr} nicht gefunden")
    return found


def lines(meta: dict) -> list[dict]:
    if meta.get("positionen"):
        return list(meta["positionen"])
    return [{"konto": str(meta["konto"]), "betrag": str(meta["betrag"]), "mwst": meta.get("mwst") or "", "text": ""}]


def booking_rows(book: Book, meta: dict) -> list[Row]:
    from .journal import convert
    from .mwst import split
    nr = meta["nummer"]
    d = parse_date(meta["datum"])
    quelle = f"spesen:{nr}"
    text = f"Spesen {meta['mitarbeiter']} {meta.get('name', '')}: {meta.get('text', '')}".strip()
    rows: list[Row] = []
    for p in lines(meta):
        row = Row(d, nr, text + (f" · {p['text']}" if p.get("text") else ""), str(p["konto"]),
                  str(meta.get("spesenkonto") or konto(book)), parse_amount(p["betrag"]), quelle)
        rows += split(book, row, str(p.get("mwst") or "").upper())
    for r in rows:
        r.quelle = quelle
    if (meta.get("waehrung") or "CHF") != "CHF":
        rows = convert(book, rows, meta["waehrung"], Decimal(str(meta["kurs"])))
    return rows


def amount_chf(book: Book, meta: dict) -> Decimal:
    owed = str(meta.get("spesenkonto") or konto(book))
    return sum((r.betrag for r in booking_rows(book, meta) if r.haben == owed), ZERO)


def assignments(book: Book) -> dict[str, str]:
    """claim → payslip that pays it ("2026-03:M0001")."""
    from . import payroll
    out = {}
    for p in payroll.payslips(book):
        for nr in (p.get("eingaben") or {}).get("spesen") or []:
            out[str(nr)] = f"{int(p['jahr'])}-{int(p['monat']):02d}:{p['mitarbeiter']}"
    return out


def open_claims(book: Book, mitarbeiter: str, until: date) -> list[dict]:
    taken = assignments(book)
    return [m for nr, m in claims(book).items() if m["mitarbeiter"] == mitarbeiter and nr not in taken
            and parse_date(m["datum"]) <= until]


def add(book: Book, mitarbeiter: str, datum, text: str, betrag, konto_: str = "", mwst: str = "",
        positionen: list[dict] | None = None, art: str = "uebrige", datei: str = "", waehrung: str = "",
        kurs=None) -> tuple[dict, list[Path]]:
    from . import payroll
    from .journal import attach, ensure_open, post
    emp = payroll.employee(book, mitarbeiter)
    d = parse_date(datum, "datum")
    ensure_open(book, d)
    if art not in ARTEN:
        raise BookError(f"Art: {', '.join(ARTEN)}")
    amount = parse_amount(betrag, "betrag")
    if amount <= 0:
        raise BookError("Betrag muss positiv sein")
    split_lines = []
    for i, p in enumerate(positionen or [], 1):
        book.account(str(p.get("konto") or ""))
        split_lines.append({"konto": str(p["konto"]), "betrag": f"{parse_amount(p['betrag']):.2f}",
                            "mwst": str(p.get("mwst") or "").upper(), "text": str(p.get("text") or "")})
    if split_lines and sum((Decimal(p["betrag"]) for p in split_lines), ZERO) != amount:
        raise BookError(f"Die Positionen ergeben nicht {amount:.2f}")
    if not split_lines:
        if not konto_:
            raise BookError("Aufwandkonto fehlt")
        book.account(konto_)
    owed = konto(book)
    book.account(owed)
    cur = (waehrung or "CHF").upper()
    rate = None
    if cur != "CHF":
        from . import fx
        rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
    nums = [int(k.rsplit("-", 1)[1]) for k in claims(book) if k.startswith(f"SP-{d.year}-")]
    nr = f"SP-{d.year}-{max(nums, default=0) + 1:04d}"
    meta = {"nummer": nr, "mitarbeiter": emp["nummer"], "name": payroll.display_name(emp), "datum": d.isoformat(),
            "text": text.strip() or "Spesen", "art": art, "betrag": f"{amount:.2f}", "spesenkonto": owed}
    if split_lines:
        meta["positionen"] = split_lines
    else:
        meta.update({"konto": str(konto_), "mwst": (mwst or "").upper()})
    if rate is not None:
        meta.update({"waehrung": cur, "kurs": str(rate)})
    rows = booking_rows(book, meta)
    touched = post(book, rows)
    if datei:
        source = Path(datei) if Path(datei).is_absolute() else book.root / datei
        target = attach(book, rows[0], source)
        meta["datei"] = str(target.relative_to(book.root))
        touched += [target, source]
    path = book.root / "spesen" / str(d.year) / f"{nr}.yaml"
    write_yaml(path, meta)
    return meta, touched + [path]


def remove(book: Book, nr: str) -> list[Path]:
    """Withdraw a claim that no payslip lists yet (the booking goes with it)."""
    from .journal import ensure_open
    meta = claim(book, nr)
    if nr in assignments(book):
        raise BookError(f"{nr} steht auf der Lohnabrechnung {assignments(book)[nr]} — dort zuerst entfernen")
    ensure_open(book, parse_date(meta["datum"]))
    touched = book.remove_rows(lambda r: r.quelle == f"spesen:{nr}")
    meta["_pfad"].unlink()
    return touched + [meta["_pfad"]]


def summary(book: Book) -> list[dict]:
    taken = assignments(book)
    return [{"nummer": nr, "mitarbeiter": m["mitarbeiter"], "name": m.get("name"), "datum": m["datum"],
             "text": m.get("text"), "art": m.get("art"), "betrag": amount_chf(book, m),
             "waehrung": m.get("waehrung") or "CHF", "datei": m.get("datei", ""),
             "lohn": taken.get(nr, "")} for nr, m in claims(book).items()]

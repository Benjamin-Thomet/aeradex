"""Posting to the journal: manual bookings, Beleg numbers, the posting lock,
and agent proposals awaiting a human's approval."""
from __future__ import annotations

import re
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

from .book import PROPOSAL_COLUMNS, Book, BookError, Row
from .files import MdTable, fmt_amount, parse_amount, parse_date, read_table, write_table

ZERO = Decimal("0")


def lock_problem(book: Book, d: date) -> str | None:
    lock = book.settings.sperre_bis
    if lock and d <= lock:
        return f"Die Bücher sind bis {lock.isoformat()} gesperrt ({d.isoformat()} liegt darin)."
    return None


def ensure_open(book: Book, d: date) -> None:
    problem = lock_problem(book, d)
    if problem:
        raise BookError(problem)


def next_beleg(book: Book, year: int, extra: set[str] | None = None) -> str:
    """Next running Beleg number 'JJ-NNN' for a year. Max-based, so a deleted
    middle number leaves a gap and is never reused (what auditors expect)."""
    prefix = f"{year % 100:02d}-"
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    highest = 0
    for ref in [r.beleg for r in book.rows] + sorted(extra or ()) + _proposal_belege(book):
        m = pattern.match(ref or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}{highest + 1:03d}"


def validate_rows(book: Book, rows: list[Row]) -> None:
    """Refuse rows that `check` would reject, before they reach the file."""
    belege: dict[str, list[Row]] = {}
    for r in rows:
        ensure_open(book, r.datum)
        if r.betrag <= 0:
            raise BookError(f"{r.beleg}: Betrag muss positiv sein (Soll/Haben tauschen statt Minus)")
        if r.betrag != r.betrag.quantize(Decimal("0.01")):
            raise BookError(f"{r.beleg}: Betrag {r.betrag} hat mehr als zwei Nachkommastellen")
        if not (r.soll or r.haben):
            raise BookError(f"{r.beleg}: Soll- oder Habenkonto fehlt")
        for nr in (r.soll, r.haben):
            if nr:
                book.account(nr)
        if r.mwst:
            from .mwst import code
            code(r.mwst)
        belege.setdefault(r.beleg, []).append(r)
    existing = {r.beleg for r in book.rows}
    for beleg, group in belege.items():
        if not beleg:
            raise BookError("Jede Buchung braucht eine Belegnummer")
        if beleg in existing:
            raise BookError(f"Beleg {beleg} ist bereits gebucht")
        if len({r.datum for r in group}) != 1:
            raise BookError(f"Beleg {beleg}: alle Zeilen müssen dasselbe Datum haben")
        s = sum((r.betrag for r in group if r.soll), ZERO)
        h = sum((r.betrag for r in group if r.haben), ZERO)
        if s != h:
            raise BookError(f"Beleg {beleg} ist nicht ausgeglichen: Soll {s} ≠ Haben {h}")


def book_entry(book: Book, datum, soll: str, haben: str, betrag, text: str,
               beleg: str = "", quelle: str = "", attachment: Path | None = None,
               mwst: str = "", waehrung: str = "", kurs=None) -> tuple[Row, list[Path]]:
    """Post one simple booking (Soll an Haben). With a MWST code the gross amount
    is split into net and tax (effective method). With a foreign `waehrung`,
    `betrag` is in that currency and is converted at `kurs` (default: the BAZG
    daily rate of the booking date). Returns the row and the files touched."""
    from .mwst import split
    d = parse_date(datum, "datum")
    row = Row(datum=d, beleg=beleg or next_beleg(book, d.year), text=text.strip(),
              soll=str(soll or "").strip(), haben=str(haben or "").strip(),
              betrag=parse_amount(betrag, "betrag"), quelle=quelle)
    rows = convert(book, split(book, row, mwst), waehrung, kurs)
    if row not in rows:     # MWST split: report the gross as the booking
        gross = max(rows, key=lambda r: r.betrag)
        row.betrag, row.waehrung, row.fw, row.kurs = gross.betrag, gross.waehrung, gross.fw, gross.kurs
    return row, post(book, rows, attachment)


def convert(book: Book, rows: list[Row], waehrung: str = "", kurs=None) -> list[Row]:
    """Rows whose amounts are in `waehrung` → CHF at `kurs` (default: BAZG rate of the
    booking date). Every row keeps its foreign amount and the rate (journal columns FW,
    Kurs). Without a currency, refuse rows that touch a foreign-currency account."""
    waehrung = (waehrung or "").strip().upper()
    touched = {book.accounts[a].waehrung for r in rows for a in (r.soll, r.haben)
               if a in book.accounts and book.accounts[a].is_foreign}
    if not waehrung or waehrung == "CHF":
        if touched:
            raise BookError(f"Konto führt {', '.join(sorted(touched))}: Währung und Betrag in dieser Währung angeben")
        return rows
    if touched - {waehrung}:
        raise BookError(f"Konto führt {', '.join(sorted(touched - {waehrung}))}, Buchung ist in {waehrung}")
    from . import fx
    rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, waehrung, rows[0].datum)
    if rate <= 0:
        raise BookError("Kurs muss positiv sein")
    for r in rows:
        r.waehrung, r.fw, r.kurs = waehrung, r.betrag, rate
        r.betrag = (r.betrag * rate).quantize(Decimal("0.01"))
    _rebalance(book, rows)
    return rows


def _rebalance(book: Book, rows: list[Row]) -> None:
    """After converting a split booking, push the rounding Rappen onto the largest one-sided row."""
    soll = sum((r.betrag for r in rows if r.soll), ZERO)
    haben = sum((r.betrag for r in rows if r.haben), ZERO)
    if soll != haben:
        # never the row of a foreign-currency account: its CHF must stay FW × Kurs
        one_sided = [r for r in rows if bool(r.soll) != bool(r.haben) and r.fw is not None]
        one_sided = [r for r in one_sided if (r.soll or r.haben) not in book.accounts or not book.accounts[r.soll or r.haben].is_foreign] or one_sided
        if one_sided:
            target = max(one_sided, key=lambda r: r.betrag)
            target.betrag += (haben - soll) if target.soll else (soll - haben)
            exact_rate(target)


def exact_rate(row: Row) -> None:
    """A row that absorbed rounding Rappen gets the rate its CHF amount implies, so CHF = FW × Kurs holds."""
    if row.fw and row.kurs and abs(row.betrag - (row.fw * row.kurs).quantize(Decimal("0.01"))) > Decimal("0.01"):
        row.kurs = (row.betrag / row.fw).quantize(Decimal("1e-10")).normalize()


def post(book: Book, rows: list[Row], attachment: Path | None = None) -> list[Path]:
    validate_rows(book, rows)
    touched = book.add_rows(rows)
    if attachment:
        touched.append(attach(book, rows[0], attachment))
    return touched


def attach(book: Book, row: Row, source: Path) -> Path:
    """Copy a receipt to belege/<JJJJ>/<Beleg> <name> so it is found by its number."""
    source = Path(source)
    if not source.is_absolute():
        # Relative paths ("inbox/quittung.pdf") are relative to the book, not to
        # wherever the CLI, the MCP server or the UI process happens to run.
        source = book.root / source
    if not source.exists():
        raise BookError(f"Beleg-Datei {source.relative_to(book.root) if source.is_relative_to(book.root) else source} nicht gefunden")
    target = book.root / "belege" / str(row.datum.year) / f"{row.beleg} {source.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve().is_relative_to((book.root / "inbox").resolve()):
        shutil.move(str(source), target)
    else:
        shutil.copy2(source, target)
    return target


def receipts_for(book: Book, beleg: str, year: int) -> list[Path]:
    folder = book.root / "belege" / str(year)
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.name == beleg or p.name.startswith(beleg + " "))


def reverse(book: Book, beleg: str, datum=None, text: str = "") -> tuple[list[Row], list[Path]]:
    """Storno: post the mirror image of a manual Beleg (Soll and Haben swapped).
    Booked rows are never deleted — a correction is a new booking."""
    original = [r for r in book.rows if r.beleg == beleg]
    if not original:
        raise BookError(f"Beleg {beleg} nicht gefunden")
    if any(r.quelle for r in original):
        raise BookError(f"Beleg {beleg} gehört zu {original[0].quelle} — dort stornieren")
    d = parse_date(datum, "datum") if datum else date.today()
    ref = next_beleg(book, d.year)
    rows = [Row(datum=d, beleg=ref, text=text or f"Storno {beleg}: {r.text}", soll=r.haben,
                haben=r.soll, betrag=r.betrag, quelle="", mwst=r.mwst) for r in original]
    return rows, post(book, rows)


# ---------- Proposals (agent → human) ----------

def proposals_path(book: Book) -> Path:
    return book.root / "vorschlaege.md"


def _proposal_table(book: Book) -> MdTable:
    path = proposals_path(book)
    if path.exists():
        table = read_table(path)
        if table.columns:
            table.columns += [c for c in PROPOSAL_COLUMNS if c not in table.columns]
            table.align["Betrag"] = "right"
            return table
    return MdTable(columns=list(PROPOSAL_COLUMNS), rows=[], align={"Betrag": "right"},
                   head="# Buchungsvorschläge\n\nVom Agenten vorgeschlagen, noch nicht gebucht. "
                        "Freigeben mit `batzen approve <ID>`, verwerfen mit `batzen reject <ID>`.")


def _proposal_belege(book: Book) -> list[str]:
    path = proposals_path(book)
    if not path.exists():
        return []
    return [r.get("Beleg", "") for r in read_table(path).rows]


def list_proposals(book: Book) -> list[dict]:
    return _proposal_table(book).rows if proposals_path(book).exists() else []


def propose(book: Book, datum, soll: str, haben: str, betrag, text: str, begruendung: str = "",
            beleg: str = "", datei: str = "", mwst: str = "", bank: str = "",
            waehrung: str = "", kurs=None) -> tuple[dict, Path]:
    """Record a booking an agent suggests. It is validated now (so a proposal is
    always postable) but only reaches the journal on approval."""
    d = parse_date(datum, "datum")
    row = Row(datum=d, beleg=beleg or next_beleg(book, d.year), text=text.strip(),
              soll=str(soll or "").strip(), haben=str(haben or "").strip(),
              betrag=parse_amount(betrag, "betrag"))
    from .mwst import split
    waehrung = (waehrung or "").strip().upper()
    if waehrung == "CHF":
        waehrung = ""
    if waehrung and kurs in (None, ""):
        from . import fx
        kurs = fx.rate(book, waehrung, d)      # fixed now, so approval books what was reviewed
    converted = convert(book, split(book, Row(row.datum, row.beleg, row.text, row.soll, row.haben, row.betrag), mwst),
                        waehrung, kurs)
    validate_rows(book, converted)
    if bank:
        from . import bank as bk
        tx = bk.find(book, bank)
        if tx["Status"] != "offen":
            raise BookError(f"Bankbewegung {bank} ist {tx['Status']}")
        amount = parse_amount(tx["Betrag"])
        if abs(amount) != converted[0].betrag or (tx["Konto"] not in (row.soll, row.haben)):
            raise BookError(f"Vorschlag passt nicht zur Bankbewegung {bank} ({tx['Konto']}, {amount})")
    if datei:
        source = Path(datei) if Path(datei).is_absolute() else book.root / datei
        if not source.exists():
            raise BookError(f"Beleg-Datei {datei} nicht gefunden")
        datei = str(source.resolve().relative_to(book.root)) if source.resolve().is_relative_to(book.root) else str(source.resolve())
    table = _proposal_table(book)
    ids = [int(r["ID"][2:]) for r in table.rows if re.fullmatch(r"V-\d+", r.get("ID", ""))]
    pid = f"V-{max(ids, default=0) + 1:03d}"
    cells = {"ID": pid, "Datum": d.isoformat(), "Beleg": row.beleg, "Text": row.text,
             "Soll": row.soll, "Haben": row.haben, "Betrag": fmt_amount(row.betrag),
             "MWST": (mwst or "").strip().upper(), "Begründung": begruendung.strip(), "Datei": datei or "",
             "Bank": bank or "", "FW": waehrung, "Kurs": str(parse_amount(kurs, "kurs")) if waehrung else ""}
    table.rows.append(cells)
    write_table(proposals_path(book), table)
    return cells, proposals_path(book)


def _take_proposals(book: Book, ids: list[str]) -> tuple[list[dict], MdTable]:
    table = _proposal_table(book)
    wanted = {i.upper() for i in ids}
    if "ALLE" in wanted or "ALL" in wanted:
        wanted = {r["ID"] for r in table.rows}
    found = [r for r in table.rows if r["ID"] in wanted]
    missing = wanted - {r["ID"] for r in found}
    if missing:
        raise BookError(f"Vorschlag nicht gefunden: {', '.join(sorted(missing))}")
    table.rows = [r for r in table.rows if r["ID"] not in wanted]
    return found, table


def approve(book: Book, ids: list[str]) -> tuple[list[Row], list[Path]]:
    found, table = _take_proposals(book, ids)
    from .mwst import split
    rows, firsts = [], []
    for p in found:
        row = Row(datum=parse_date(p["Datum"]), beleg=p["Beleg"], text=p["Text"], soll=p["Soll"],
                  haben=p["Haben"], betrag=parse_amount(p["Betrag"]))
        firsts.append(row)
        rows += convert(book, split(book, row, p.get("MWST", "")), p.get("FW", ""), p.get("Kurs") or None)
    touched = post(book, rows)
    for p, row in zip(found, firsts):
        if p.get("Bank"):
            from . import bank as bk
            touched += bk._set(book, p["Bank"], Status="gebucht", Beleg=row.beleg)
    # The receipt travels with the proposal: approving files it under belege/.
    for p, row in zip(found, firsts):
        if p.get("Datei"):
            source = Path(p["Datei"]) if Path(p["Datei"]).is_absolute() else book.root / p["Datei"]
            if source.exists():
                touched += [attach(book, row, source), source]
    write_table(proposals_path(book), table)
    return rows, touched + [proposals_path(book)]


def reject(book: Book, ids: list[str]) -> tuple[list[dict], list[Path]]:
    found, table = _take_proposals(book, ids)
    write_table(proposals_path(book), table)
    return found, [proposals_path(book)]

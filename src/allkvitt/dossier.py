"""Abschlussunterlagen: every document of a business year, as one ZIP or one by one.

Each part (Jahresrechnung, Journal, Belegordner …) is built by one function for
both ways out, so the ZIP and a single download never differ.

Belegnummer = Laufnummer. The Belegordner stamps every page with the Beleg
number the journal carries — there is no second, positional count that could
shift when a booking is cancelled or a document voided. Numbers are never
reused (`journal.next_beleg` keeps a high-water mark per year); a number missing
from a series is listed in the Lückenverzeichnis with the reason found in the
book or its git history.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from . import __version__, gitlog
from . import invoices as inv
from . import kreditoren as kred
from . import pdf as pdfmod
from .book import Book, BookError, Row
from .files import parse_date
from .ledger import BalanceEngine, account_ledger, trial_balance

ZERO = Decimal("0")
PDF_SUFFIXES = (".pdf",)
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")

# Number series allkvitt issues itself; a gap in one of them needs an explanation.
SERIES = [re.compile(r"^(\d{2}-)(\d+)$"), re.compile(r"^(R-\d{4}-)(\d+)$"), re.compile(r"^(E-\d{4}-)(\d+)$")]


# ---------- small helpers ----------

def natural_key(beleg: str) -> tuple:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", beleg or "") if p)


def amount(value) -> str:
    v = Decimal(str(value or 0))
    return f"{abs(v) if not v else v:.2f}"


def csv_bytes(header: list[str], rows: list[list]) -> bytes:
    """Semicolon CSV with BOM: opens straight in Excel with Swiss settings."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else amount(v) if isinstance(v, Decimal) else
                    v.isoformat() if isinstance(v, date) else str(v) for v in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Kontext:
    """What every part needs for one year, computed once."""

    def __init__(self, book: Book, year: int):
        if year not in book.years():
            raise BookError(f"Kein Geschäftsjahr {year} (vorhanden: {', '.join(map(str, book.years()))})")
        self.book, self.year = book, year
        self.engine = BalanceEngine(book)
        self.rows = [r for r in book.rows if r.datum.year == year]
        self.stichtag = date(year, 12, 31)
        lock = book.settings.sperre_bis
        self.entwurf = not (lock and lock >= self.stichtag)
        self._luecken = None

        self._payslips = None

    @property
    def luecken(self) -> list[dict]:
        if self._luecken is None:
            self._luecken = belegluecken(self.book, self.year)
        return self._luecken

    @property
    def payslips(self) -> list[dict]:
        if self._payslips is None:
            from . import payroll
            self._payslips = [p for p in payroll.payslips(self.book, self.year) if p.get("status") == "abgeschlossen"]
        return self._payslips

    @contextmanager
    def draft(self):
        """Reports built inside carry «ENTWURF» while the year is not locked."""
        token = pdfmod.WATERMARK.set("ENTWURF" if self.entwurf else "")
        try:
            yield
        finally:
            pdfmod.WATERMARK.reset(token)

    def report(self, build, *args) -> bytes:
        with self.draft():
            return build(*args)


# ---------- gaps in the number series ----------

def _git_trace(book: Book, ref: str, paths: tuple[str, ...]) -> dict | None:
    """The last commit that added or removed the cell `| ref ` in `paths`."""
    if not gitlog.is_repo(book.root):
        return None
    out = gitlog._git(book.root, "log", "-1", "--format=%h\x1f%ad\x1f%s", "--date=short",
                      "-S", f"| {ref} ", "--", *paths, check=False).stdout.strip()
    if not out:
        return None
    h, d, s = out.split("\x1f", 2)
    return {"commit": h, "datum": d, "nachricht": s}


def belegluecken(book: Book, year: int, trace: bool = True) -> list[dict]:
    """Numbers missing from allkvitt's own series of `year`, each with the reason found:
    voided invoice/bill, rejected proposal, removed in a commit, or 'unbekannt'.
    `trace=False` skips the git history (fast, for `check`)."""
    from .journal import _proposal_belege
    used: dict[str, set[int]] = defaultdict(set)
    refs = {r.beleg for r in book.rows}
    voided: dict[str, dict] = {}
    for meta in list(inv.invoices(book).values()) + list(kred.bills(book).values()):
        refs.add(meta["nummer"])
        if meta.get("status") == "storniert":
            voided[meta["nummer"]] = meta
    pending = set(_proposal_belege(book))
    refs |= pending
    yy, yyyy = f"{year % 100:02d}-", f"-{year}-"
    for ref in refs:
        for pattern in SERIES:
            m = pattern.match(ref or "")
            if m and (m.group(1) == yy or yyyy in m.group(1)):
                used[m.group(1)].add(int(m.group(2)))
    issued = book.issued_numbers().get(year, 0)
    if issued:
        used[yy].add(0)                 # the series exists even if every booking is gone
    out = []
    width = {}
    for ref in refs:
        for pattern in SERIES:
            m = pattern.match(ref or "")
            if m:
                width[m.group(1)] = len(m.group(2))
    booked_refs = {r.beleg for r in book.rows}
    for prefix, nums in sorted(used.items()):
        nums.discard(0)
        top = max([*nums, issued if prefix == yy else 0])
        for n in range(1, top + 1):
            ref = f"{prefix}{n:0{width.get(prefix, 3)}d}"
            booked = ref in booked_refs
            if n in nums and (booked or ref in pending):
                if ref in pending and not booked:
                    out.append({"beleg": ref, "art": "reserviert", "grund": "offener Vorschlag, noch nicht gebucht"})
                continue
            if ref in voided:
                meta = voided[ref]
                grund = f"storniert am {meta.get('storniert_am', '?')}"
                if meta.get("storno_grund"):
                    grund += f": {meta['storno_grund']}"
                out.append({"beleg": ref, "art": "storniert", "grund": grund})
                continue
            found = _git_trace(book, ref, ("journal",)) if trace else None
            if found:
                out.append({"beleg": ref, "art": "entfernt",
                            "grund": f"aus dem Journal entfernt am {found['datum']} (Commit {found['commit']}: "
                                     f"{found['nachricht']})"})
                continue
            found = _git_trace(book, ref, ("vorschlaege.md",)) if trace else None
            if found:
                out.append({"beleg": ref, "art": "verworfen",
                            "grund": f"Vorschlag verworfen am {found['datum']} (Commit {found['commit']}: "
                                     f"{found['nachricht']})"})
                continue
            out.append({"beleg": ref, "art": "unbekannt", "grund": "keine Buchung und keine Spur im Änderungsverlauf"})
    return out


def orphan_receipts(book: Book) -> list[str]:
    """Files in belege/ whose number no booking and no document carries (any more)."""
    folder = book.root / "belege"
    if not folder.exists():
        return []
    from . import spesen
    known = {r.beleg for r in book.rows}
    known |= set(inv.invoices(book)) | set(kred.bills(book))
    try:
        known |= {s["nummer"] for s in spesen.summary(book)}
    except Exception:
        pass
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not p.name.startswith(".") and p.name.split(" ")[0] not in known \
                and p.stem not in known:
            out.append(book.rel(p))
    return out


# ---------- the Belegordner ----------

@dataclass
class Beleg:
    nummer: str
    datum: date
    rows: list[Row]
    dateien: list[Path]           # original files in belege/ (and rechnungen/, lohn/)
    art: str                      # datei | rechnung | lohn | bank | intern | fehlt | storniert
    hinweis: str = ""

    @property
    def text(self) -> str:
        return self.rows[0].text if self.rows else self.hinweis

    @property
    def betrag(self) -> Decimal:
        return sum((r.betrag for r in self.rows if r.soll), ZERO)


def _bank_refs(book: Book) -> dict[str, dict]:
    from . import bank
    try:
        return {t.get("Beleg"): t for t in bank.transactions(book) if t.get("Beleg")}
    except Exception:
        return {}


def belege(k: Kontext) -> list[Beleg]:
    """Every Beleg of the year in order (date, then number), with what documents it."""
    from .journal import receipts_for
    book = k.book
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in k.rows:
        groups[r.beleg].append(r)
    invoices = inv.invoices(book)
    bank_refs = _bank_refs(book)
    out = []
    for nr, rows in groups.items():
        files = receipts_for(book, nr, k.year)
        quelle = rows[0].quelle
        art, hinweis = "datei", ""
        if not files and nr in invoices:
            pdf_path = book.root / "rechnungen" / str(parse_date(invoices[nr]["datum"]).year) / f"{nr}.pdf"
            files = [pdf_path] if pdf_path.exists() else []
            art, hinweis = "rechnung", "Ausgangsrechnung"
        elif not files and quelle.startswith("lohn:"):
            art, hinweis = "lohn", "Lohnabrechnung"
            ym, _, emp = quelle.partition(":")[2].partition(":")
            y, m = ym.split("-")
            slip = book.root / "lohn" / y / m / f"{emp}.pdf"
            files = [slip] if slip.exists() else []
        elif not files and nr in bank_refs:
            t = bank_refs[nr]
            art = "bank"
            hinweis = (f"Beleg ist der Kontoauszug {t.get('Auszug', '')}, Bewegung {t.get('ID')} vom "
                       f"{t.get('Datum')}: {t.get('Gegenpartei') or ''} {t.get('Text') or ''}".strip())
        elif not files and quelle and not quelle.startswith("kreditor:"):
            art, hinweis = "intern", f"Interner Buchungsbeleg ({quelle.partition(':')[0]})"
        elif not files:
            art, hinweis = "fehlt", "Keine Belegdatei vorhanden"
        out.append(Beleg(nr, min(r.datum for r in rows), rows, files, art, hinweis))
    out.sort(key=lambda b: (b.datum, natural_key(b.nummer)))
    return out


def _lohn_pdf(book: Book, quelle: str) -> bytes | None:
    from . import payroll
    try:
        ym, _, emp = quelle.partition(":")[2].partition(":")
        y, m = ym.split("-")
        meta = payroll.load_payslip(book, int(y), int(m), emp)
        return pdfmod.payslip_pdf(book, meta, payroll.employee(book, emp))
    except Exception:
        return None


def _document_pages(k: Kontext, b: Beleg) -> list:
    """The pages of one Beleg as pypdf page objects (originals, or a generated sheet)."""
    from pypdf import PdfReader
    pages = []
    problems = []
    for f in b.dateien:
        suffix = f.suffix.lower()
        try:
            if suffix in PDF_SUFFIXES:
                reader = PdfReader(str(f))
                if reader.is_encrypted:
                    reader.decrypt("")
                pages += list(reader.pages)
            elif suffix in IMAGE_SUFFIXES:
                pages += list(PdfReader(io.BytesIO(pdfmod.image_page_pdf(f))).pages)
            else:
                problems.append(f"{f.name}: Dateiformat kann nicht eingebunden werden — siehe Originaldatei")
        except Exception as exc:
            problems.append(f"{f.name}: nicht lesbar ({type(exc).__name__}) — siehe Originaldatei")
    if not pages and b.art == "lohn":
        data = _lohn_pdf(k.book, b.rows[0].quelle)
        if data:
            pages = list(PdfReader(io.BytesIO(data)).pages)
    if not pages or problems:
        notes = ([b.hinweis] if b.hinweis else []) + problems
        sheet = pdfmod.beleg_sheet_pdf(k.book, b.nummer, b.text, b.rows, notes, b.art == "fehlt" or bool(problems))
        pages += list(PdfReader(io.BytesIO(sheet)).pages)
    return pages


def belegordner_pdf(k: Kontext) -> bytes:
    from pypdf import PdfReader, PdfWriter
    items = belege(k)
    body = PdfWriter()
    spans = []                # (beleg, first page index in body, page count)
    for b in items:
        pages = _document_pages(k, b)
        first = len(body.pages)
        for i, page in enumerate(pages, 1):
            pdfmod.stamp(page, f"Beleg {b.nummer} · {b.datum:%d.%m.%Y} · CHF {pdfmod.chf(b.betrag)} · "
                               f"Seite {i}/{len(pages)}")
            body.add_page(page)
        spans.append((b, first, len(pages)))

    def front(offset: int) -> bytes:
        index = [[b.nummer, pdfmod.d(b.datum), b.text, pdfmod.chf(b.betrag), str(first + offset + 1),
                  str(n), _art_label(b)] for b, first, n in spans]
        blocks = [("small", "Jede Seite trägt oben rechts die Belegnummer der Buchung im Journal. "
                            "Nummern werden nie neu vergeben; fehlende Nummern stehen im Lückenverzeichnis."),
                  ("table", ["Beleg", "Datum", "Text", "CHF", "Seite", "Seiten", "Grundlage"], index,
                   (0.17, 0.1, 0.35, 0.11, 0.07, 0.06, 0.14), {"right": (3, 4, 5), "wrap": (2, 6), "zebra": True}),
                  ("page",), ("h2", "Lückenverzeichnis"),
                  ("small", "Nummern aus den Serien von allkvitt (JJ-NNN, R-JJJJ-NNNN, E-JJJJ-NNNN), zu denen es "
                            "in diesem Jahr keine Buchung gibt.")]
        blocks.append(("table", ["Nummer", "Art", "Grund"], [[g["beleg"], g["art"], g["grund"]] for g in k.luecken],
                       (0.16, 0.14, 0.7), {"wrap": (2,)}) if k.luecken else ("text", "Keine Lücken."))
        with k.draft():
            return pdfmod.report_pdf(k.book, f"Belegordner {k.year}", blocks,
                                     subtitle=f"{k.book.settings.firma} · {len(spans)} Belege · "
                                              f"Stand {date.today():%d.%m.%Y}")

    # The index names page numbers, which depend on the length of the index itself.
    offset = len(PdfReader(io.BytesIO(front(0))).pages)
    head = front(offset)
    if len(PdfReader(io.BytesIO(head)).pages) != offset:
        offset = len(PdfReader(io.BytesIO(head)).pages)
        head = front(offset)
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(head)))
    body_bytes = io.BytesIO()
    body.write(body_bytes)
    reader = PdfReader(body_bytes)
    for page in reader.pages:
        writer.add_page(page)
    writer.add_outline_item("Belegverzeichnis", 0)
    for b, first, n in spans:
        if n:
            writer.add_outline_item(f"{b.nummer} {b.text}"[:80], first + offset)
    writer.add_metadata({"/Title": f"Belegordner {k.year}", "/Author": k.book.settings.firma})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _art_label(b: Beleg) -> str:
    return {"datei": ", ".join(f.name for f in b.dateien) or "Datei", "rechnung": "Ausgangsrechnung",
            "lohn": "Lohnabrechnung", "bank": "Kontoauszug", "intern": "interner Beleg",
            "fehlt": "FEHLT"}.get(b.art, b.art)


def belegverzeichnis_csv(k: Kontext) -> bytes:
    rows = [[b.nummer, b.datum, b.text, b.betrag, _art_label(b),
             " | ".join(k.book.rel(f) for f in b.dateien)] for b in belege(k)]
    rows += [[g["beleg"], "", f"LÜCKE ({g['art']}): {g['grund']}", "", "", ""] for g in k.luecken]
    return csv_bytes(["Beleg", "Datum", "Text", "Betrag CHF", "Grundlage", "Dateien"], rows)


def belegdateien(k: Kontext) -> list[tuple[str, bytes]]:
    """The original files, named as in belege/ (they start with the Beleg number)."""
    out = []
    for b in belege(k):
        for f in b.dateien:
            name = f.name if f.name.startswith(b.nummer) else f"{b.nummer} {f.name}"
            out.append((name, f.read_bytes()))
    return out


# ---------- the other parts ----------

def _jahresrechnung(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    from . import statements
    st = statements.year_end_statement(k.book, k.year, k.engine)
    if fmt == "pdf":
        blocks = statements.parse_anhang(statements.anhang(k.book, k.year))
        return [(f"Jahresrechnung {k.year}.pdf", k.report(pdfmod.statement_pdf, k.book, st, blocks))]
    head = ["Position", "Art", str(k.year), str(k.year - 1)]
    bilanz = [[r["label"], "Aktiven/" + r["stil"], r["aktuell"], r["vorjahr"]] for r in st["aktiven"]] + \
             [[r["label"], "Passiven/" + r["stil"], r["aktuell"], r["vorjahr"]] for r in st["passiven"]]
    erfolg = [[r["label"], r["stil"], r["aktuell"], r["vorjahr"]] for r in st["erfolg"]]
    return [(f"Bilanz {k.year}.csv", csv_bytes(head, bilanz)),
            (f"Erfolgsrechnung {k.year}.csv", csv_bytes(head, erfolg))]


def _saldenliste(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    tb = trial_balance(k.book, k.year, engine=k.engine)
    if fmt == "csv":
        return [(f"Saldenliste {k.year}.csv", csv_bytes(
            ["Konto", "Bezeichnung", "Klasse", "Eröffnung", "Soll", "Haben", "Saldo"],
            [[t["konto"], t["name"], t["klasse"], t["eroeffnung"], t["soll"], t["haben"], t["saldo"]] for t in tb]))]
    rows = [[t["konto"], t["name"], t["klasse"], pdfmod.chf(t["eroeffnung"]), pdfmod.chf(t["soll"], True),
             pdfmod.chf(t["haben"], True), pdfmod.chf(t["saldo"])] for t in tb]
    rows.append(["", "Total", "", "", pdfmod.chf(sum((t["soll"] for t in tb), ZERO)),
                 pdfmod.chf(sum((t["haben"] for t in tb), ZERO)), ""])
    with k.draft():
        data = pdfmod.report_pdf(
            k.book, f"Saldenliste {k.year}",
            [("small", "Vorzeichen wie im Hauptbuch: Aktiven und Aufwand +, Passiven und Ertrag −."),
             ("table", ["Konto", "Bezeichnung", "Klasse", "Eröffnung", "Soll", "Haben", "Saldo"], rows,
              (0.08, 0.34, 0.08, 0.125, 0.125, 0.125, 0.125), {"right": (3, 4, 5, 6), "totals": (len(rows) - 1,),
                                                               "wrap": (1,), "zebra": True})],
            subtitle=f"{k.book.settings.firma} · 01.01.–31.12.{k.year}", wide=True)
    return [(f"Saldenliste {k.year}.pdf", data)]


def _journal(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    if fmt == "pdf":
        return [(f"Journal {k.year}.pdf", k.report(pdfmod.journal_pdf, k.book, k.rows, f"Journal {k.year}"))]
    rows = [[r.datum, r.beleg, r.text, r.soll, r.haben, r.betrag, r.waehrung, r.fw if r.waehrung else "",
             format(r.kurs.normalize(), "f") if r.kurs is not None else "", r.mwst, r.quelle] for r in k.rows]
    return [(f"Journal {k.year}.csv", csv_bytes(
        ["Datum", "Beleg", "Text", "Soll", "Haben", "Betrag CHF", "Währung", "Betrag FW", "Kurs", "MWST", "Quelle"],
        rows))]


def _kontoblaetter(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    accounts = [t["konto"] for t in trial_balance(k.book, k.year, engine=k.engine)]
    ledgers = [account_ledger(k.book, nr, k.year, k.engine) for nr in accounts]
    if fmt == "csv":
        rows = []
        for led in ledgers:
            rows.append([led["konto"], led["name"], "", "", "Eröffnung", "", "", "", led["eroeffnung"]])
            rows += [[led["konto"], led["name"], z["datum"], z["beleg"], z["text"], z["gegenkonto"],
                      z["soll"], z["haben"], z["saldo"]] for z in led["zeilen"]]
        return [(f"Kontoblätter {k.year}.csv", csv_bytes(
            ["Konto", "Bezeichnung", "Datum", "Beleg", "Text", "Gegenkonto", "Soll", "Haben", "Saldo"], rows))]
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for led in ledgers:
        start = len(writer.pages)
        writer.append(PdfReader(io.BytesIO(k.report(pdfmod.ledger_pdf, k.book, led))))
        writer.add_outline_item(f"{led['konto']} {led['name']}", start)
    writer.add_metadata({"/Title": f"Kontoblätter {k.year}", "/Author": k.book.settings.firma})
    out = io.BytesIO()
    writer.write(out)
    return [(f"Kontoblätter {k.year}.pdf", out.getvalue())]


def _belege(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    if fmt == "pdf":
        return [(f"Belegordner {k.year}.pdf", belegordner_pdf(k))]
    if fmt == "csv":
        return [(f"Belegverzeichnis {k.year}.csv", belegverzeichnis_csv(k))]
    return [(f"Belege/{name}", data) for name, data in belegdateien(k)]


def _mwst(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    from . import mwst
    reports = []
    for periode in mwst.periods(k.book, k.year):
        try:
            reports.append(mwst.report(k.book, periode))
        except (BookError, ValueError):
            continue
    origins = {rep["periode"]: mwst.herkunft(k.book, rep["periode"]) for rep in reports}
    if fmt == "csv":
        rows = [[rep["periode"], z, v] for rep in reports for z, v in sorted(rep["ziffern"].items())]
        detail = [[per, g["ziffer"], z["datum"], z["beleg"], z["text"], z["konto"], z["code"], z["entgelt"], z["steuer"],
                   z["hinweis"]] for per, h in origins.items() for g in h["gruppen"] for z in g["zeilen"]]
        return [(f"MWST {k.year}/MWST-Ziffern {k.year}.csv", csv_bytes(["Periode", "Ziffer", "Wert"], rows)),
                (f"MWST {k.year}/MWST-Herkunft {k.year}.csv", csv_bytes(
                    ["Periode", "Ziffer", "Datum", "Beleg", "Text", "Konto", "Code", "Entgelt", "Steuer", "Hinweis"],
                    detail))]
    out = [(f"MWST {k.year}/MWST-Abrechnung {rep['periode']}.pdf",
            k.report(pdfmod.mwst_pdf, k.book, rep, origins[rep["periode"]])) for rep in reports]
    for rep in reports:
        try:
            out.append((f"MWST {k.year}/eMWST {rep['periode']}.xml", mwst.ech0217(k.book, rep["periode"])))
        except (BookError, ValueError):
            pass
    try:
        out.append((f"MWST {k.year}/MWST-Umsatzabstimmung {k.year}.pdf",
                    k.report(pdfmod.mwst_abstimmung_pdf, k.book, mwst.abstimmung(k.book, k.year))))
    except (BookError, ValueError):
        pass
    return out


def open_payables_at(book: Book, stichtag: date) -> dict:
    """Kreditoren open on `stichtag` (see `kreditoren.open_payables`)."""
    return kred.open_payables(book, stichtag)


def _offene_posten(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    ar = inv.aged_receivables(k.book, k.stichtag)
    ap = open_payables_at(k.book, k.stichtag)
    tag = f"{k.stichtag:%d.%m.%Y}"
    if fmt == "csv":
        return [(f"Offene Debitoren {tag}.csv", csv_bytes(
                    ["Rechnung", "Kunde", "Datum", "Alter Tage", "Offen CHF"],
                    [[p["nummer"], p.get("name") or p.get("kunde"), p["datum"], p["alter_tage"], p["offen_chf"]]
                     for p in ar["posten"]])),
                (f"Offene Kreditoren {tag}.csv", csv_bytes(
                    ["Kreditor", "Lieferant", "Rechnungsnr", "Datum", "Fällig", "Währung", "Offen", "Offen CHF"],
                    [[p["nummer"], p.get("name"), p["rechnungsnr"], p["datum"], p["faellig"], p["waehrung"],
                      p["offen"], p["offen_chf"]] for p in ap["posten"]]))]
    rows = [[p["nummer"], p.get("name") or "", p["rechnungsnr"], pdfmod.d(p["datum"]), pdfmod.d(p["faellig"]),
             pdfmod.chf(p["offen_chf"])] for p in ap["posten"]]
    rows.append(["Total", "", "", "", "", pdfmod.chf(ap["total_offen"])])
    kred_pdf = k.report(pdfmod.report_pdf, k.book, f"Offene Kreditoren per {tag}", [
        ("table", ["Kreditor", "Lieferant", "Rechnungsnr.", "Datum", "Fällig", "Offen CHF"], rows,
         (0.15, 0.33, 0.14, 0.12, 0.12, 0.14), {"right": (5,), "totals": (len(rows) - 1,), "wrap": (1,)}),
        ("small", f"Saldo Konto {ap['kreditorenkonto']}: {pdfmod.chf(ap['saldo_kreditoren'])} · "
                  f"Differenz zur Offen-Posten-Liste: {pdfmod.chf(ap['differenz'])}")])
    return [(f"Offene Debitoren {tag}.pdf", k.report(pdfmod.receivables_pdf, k.book, ar)),
            (f"Offene Kreditoren {tag}.pdf", kred_pdf)]


LOHN_COLS = [("bruttolohn", "Brutto"), ("ahv", "AHV/IV/EO"), ("alv", "ALV"), ("bvg", "BVG"), ("uvg", "NBU"),
             ("ktg", "KTG"), ("quellensteuer", "QST"), ("kinderzulagen", "Zulagen"), ("spesen", "Spesen"),
             ("nettolohn", "Netto")]


def _payslips(k: Kontext) -> list[dict]:
    return k.payslips


def _lohn(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    slips = sorted(_payslips(k), key=lambda p: (int(p["monat"]), p["mitarbeiter"]))
    values = [[f"{int(p['monat']):02d}", p["mitarbeiter"], p.get("name") or ""] +
              [Decimal(str((p.get("werte") or {}).get(key) or 0)) for key, _ in LOHN_COLS] for p in slips]
    head = ["Monat", "Nr", "Name"] + [label for _, label in LOHN_COLS]
    if fmt == "csv":
        return [(f"Lohn {k.year}/Lohnjournal {k.year}.csv", csv_bytes(head, values))]
    totals = ["", "", "Total"] + [sum((v[i] for v in values), ZERO) for i in range(3, 3 + len(LOHN_COLS))]
    rows = [r[:3] + [pdfmod.chf(x, True) for x in r[3:]] for r in values + [totals]]
    with k.draft():
        data = pdfmod.report_pdf(
            k.book, f"Lohnjournal {k.year}",
            [("table", head, rows, [0.05, 0.06, 0.15] + [0.074] * len(LOHN_COLS),
              {"right": tuple(range(3, 3 + len(LOHN_COLS))), "totals": (len(rows) - 1,), "wrap": (2,),
               "zebra": True})],
            subtitle="Abgeschlossene Lohnabrechnungen, Beträge in CHF", wide=True)
    out = [(f"Lohn {k.year}/Lohnjournal {k.year}.pdf", data)]
    from . import lohnausweis
    for nr in sorted({p["mitarbeiter"] for p in slips}):
        try:
            out.append((f"Lohn {k.year}/Lohnausweis {k.year} {nr}.pdf", lohnausweis.build_pdf(k.book, k.year, nr)))
        except Exception:
            continue
    return out


def _kontenplan(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    accts = sorted(k.book.accounts.values(), key=lambda a: natural_key(a.nr))
    if fmt == "csv":
        return [("Kontenplan.csv", csv_bytes(["Konto", "Bezeichnung", "Klasse", "Gruppe", "Währung", "Aktiv"],
                                             [[a.nr, a.name, a.klasse, a.gruppe, a.waehrung, "ja" if a.aktiv_ else "nein"]
                                              for a in accts]))]
    rows = [[a.nr, a.name, a.klasse, a.waehrung if a.is_foreign else ""] for a in accts]
    with k.draft():
        data = pdfmod.report_pdf(k.book, "Kontenplan", [("table", ["Konto", "Bezeichnung", "Klasse", "Währung"], rows,
                                                         (0.12, 0.6, 0.16, 0.12), {"wrap": (1,), "zebra": True})],
                                 subtitle=f"{k.book.settings.firma} · Stand {date.today():%d.%m.%Y}")
    return [("Kontenplan.pdf", data)]


@dataclass
class Teil:
    key: str
    nr: str
    label: str
    formate: tuple[str, ...]
    build: Callable[[Kontext, str], list[tuple[str, bytes]]]
    relevant: Callable[[Kontext], bool] = lambda k: True
    beschreibung: str = ""


def _geldfluss(k: Kontext, fmt: str) -> list[tuple[str, bytes]]:
    from datetime import date as _date

    from . import berichte, reports
    start, end = _date(k.year, 1, 1), _date(k.year, 12, 31)
    reps = [reports.geldfluss(k.book, start, end), reports.kennzahlen(k.book, start, end)]
    if fmt == "csv":
        return [(f"Geldflussrechnung {k.year}.csv", berichte.csv(reps[0])),
                (f"Kennzahlen {k.year}.csv", berichte.csv(reps[1]))]
    items = [{"bericht": r, "kommentar": None} for r in reps]
    return [(f"Geldfluss und Kennzahlen {k.year}.pdf",
             k.report(berichte.pdf, k.book, items, f"Geldfluss und Kennzahlen {k.year}"))]


def _mwst_relevant(k: Kontext) -> bool:
    from . import mwst
    return mwst.config(k.book)["methode"] != "keine"


TEILE = [
    Teil("jahresrechnung", "01", "Jahresrechnung", ("pdf", "csv"), _jahresrechnung,
         beschreibung="Bilanz, Erfolgsrechnung und Anhang"),
    Teil("saldenliste", "02", "Saldenliste", ("pdf", "csv"), _saldenliste,
         beschreibung="Eröffnung, Soll, Haben und Saldo je Konto"),
    Teil("journal", "03", "Journal", ("pdf", "csv"), _journal, beschreibung="alle Buchungen des Jahres"),
    Teil("kontoblaetter", "04", "Kontoblätter", ("pdf", "csv"), _kontoblaetter,
         beschreibung="Kontodetail aller bebuchten Konten"),
    Teil("belege", "05", "Belegordner", ("pdf", "csv", "dateien"), _belege,
         beschreibung="alle Belege, mit Belegnummer gestempelt; Verzeichnis, Lücken, Originaldateien"),
    Teil("mwst", "06", "MWST", ("pdf", "csv"), _mwst, _mwst_relevant,
         beschreibung="Abrechnungen je Periode mit Herkunft der Zahlen, eMWST-Dateien, Umsatzabstimmung"),
    Teil("offene_posten", "07", "Offene Posten 31.12.", ("pdf", "csv"), _offene_posten,
         beschreibung="Debitoren und Kreditoren per Stichtag"),
    Teil("lohn", "08", "Lohn", ("pdf", "csv"), _lohn, lambda k: bool(_payslips(k)),
         beschreibung="Lohnjournal und Lohnausweise"),
    Teil("kontenplan", "09", "Kontenplan", ("pdf", "csv"), _kontenplan),
    Teil("geldfluss", "10", "Geldfluss und Kennzahlen", ("pdf", "csv"), _geldfluss,
         beschreibung="Geldflussrechnung (indirekt, OR 961b) und KMU-Kennzahlen"),
]


def teile(book: Book, k: Kontext | None = None) -> list[Teil]:
    """Built-in parts plus those plugins contribute (numbered after the built-ins)."""
    from . import plugins
    out = list(TEILE)
    for i, t in enumerate(plugins.dossier_teile(book), len(TEILE) + 1):
        out.append(Teil(t.key, f"{i:02d}", t.label, tuple(t.formate),
                        lambda kk, fmt, t=t: t.build(kk.book, kk.year, fmt),
                        (lambda kk, t=t: t.relevant(kk.book, kk.year)) if t.relevant else (lambda kk: True),
                        t.beschreibung))
    return out


def teil(book: Book, key: str) -> Teil:
    for t in teile(book):
        if t.key == key:
            return t
    raise BookError(f"Unbekannter Teil '{key}' (vorhanden: {', '.join(t.key for t in teile(book))})")


def overview(book: Book, year: int) -> list[dict]:
    k = Kontext(book, year)
    return [{"teil": t.key, "nr": t.nr, "label": t.label, "formate": list(t.formate),
             "beschreibung": t.beschreibung, "vorhanden": t.relevant(k)} for t in teile(book)]


# ---------- one part, or everything as a ZIP ----------

def _zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def build_part(book: Book, year: int, key: str, fmt: str) -> tuple[str, bytes]:
    """One part in one format; several files (MWST, Lohn, Originalbelege) come as a ZIP."""
    t = teil(book, key)
    if fmt not in t.formate:
        raise BookError(f"{t.label}: Format '{fmt}' nicht verfügbar ({', '.join(t.formate)})")
    k = Kontext(book, year)
    files = t.build(k, fmt)
    if not files:
        raise BookError(f"{t.label} {year}: nichts vorhanden")
    if len(files) == 1:
        return Path(files[0][0]).name, files[0][1]
    return f"{book.settings.firma} {t.label} {year}.zip", _zip(files)


def _commit(book: Book) -> tuple[str | None, bool]:
    if not gitlog.is_repo(book.root):
        return None, False
    head = gitlog._git(book.root, "rev-parse", "HEAD", check=False).stdout.strip() or None
    dirty = bool(gitlog._git(book.root, "status", "--porcelain", "--", ".", ":!berichte", ":!inbox",
                             check=False).stdout.strip())
    return head, dirty


def build_zip(book: Book, year: int, keys: list[str] | None = None,
              formate: list[str] | None = None) -> tuple[str, bytes, dict]:
    """The Abschlussdossier: chosen parts (default all that apply) in the chosen formats
    (default all), a contents PDF and manifest.json with SHA-256 of every file."""
    from . import check as checks
    k = Kontext(book, year)
    chosen = [t for t in teile(book) if (keys is None or t.key in keys) and t.relevant(k)]
    if keys:
        unknown = set(keys) - {t.key for t in teile(book)}
        if unknown:
            raise BookError(f"Unbekannte Teile: {', '.join(sorted(unknown))}")
    folder = f"{book.settings.firma} Abschluss {year}"
    files: list[tuple[str, bytes]] = []
    contents = []
    for t in chosen:
        names = []
        for fmt in t.formate:
            if formate and fmt not in formate:
                continue
            for name, data in t.build(k, fmt):
                path = f"{t.nr} {name}"
                files.append((path, data))
                names.append(path)
        contents.append({"nr": t.nr, "label": t.label, "beschreibung": t.beschreibung, "dateien": names})
    issues = checks.run(book)
    commit, dirty = _commit(book)
    manifest = {
        "format": "allkvitt-abschluss", "version": 1, "allkvitt": __version__,
        "firma": book.settings.firma, "uid": book.settings.get("uid") or "", "jahr": year,
        "erstellt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "gesperrt_bis": book.settings.sperre_bis.isoformat() if book.settings.sperre_bis else None,
        "entwurf": k.entwurf, "commit": commit, "uncommittete_aenderungen": dirty,
        "pruefung": {"fehler": sum(i.level == "fehler" for i in issues),
                     "warnungen": sum(i.level == "warnung" for i in issues)},
        "luecken": k.luecken,
        "dateien": {name: sha256(data) for name, data in files},
    }
    cover = inhalt_pdf(k, contents, manifest, issues)
    files.insert(0, ("00 Inhalt.pdf", cover))
    manifest["dateien"] = {"00 Inhalt.pdf": sha256(cover), **manifest["dateien"]}
    files.append(("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")))
    return f"{folder}.zip", _zip([(f"{folder}/{n}", d) for n, d in files]), manifest


def inhalt_pdf(k: Kontext, contents: list[dict], manifest: dict, issues: list) -> bytes:
    s = k.book.settings
    lock = s.sperre_bis
    status = (f"Gesperrt bis {lock:%d.%m.%Y} — die Zahlen dieses Jahres sind unveränderlich." if not k.entwurf
              else "ENTWURF — das Jahr ist noch nicht gesperrt (allkvitt lock); Zahlen können sich ändern.")
    blocks = [("bold", status),
              ("small", f"Erstellt {manifest['erstellt']} mit allkvitt {__version__}"
                        + (f" · Stand Commit {manifest['commit'][:12]}" if manifest["commit"] else "")
                        + (" · ACHTUNG: nicht committete Änderungen im Buch" if manifest["uncommittete_aenderungen"]
                           else "")),
              ("h2", "Inhalt"),
              ("table", ["Nr", "Dokument", "Dateien"],
               [[c["nr"], f"{c['label']} — {c['beschreibung']}" if c["beschreibung"] else c["label"],
                 "\n".join(Path(n).name for n in c["dateien"] if "Belege/" not in n)
                 + (f"\n+ {sum('Belege/' in n for n in c['dateien'])} Originalbelege (Ordner Belege)"
                    if any("Belege/" in n for n in c["dateien"]) else "")]
                for c in contents],
               (0.06, 0.44, 0.5), {"wrap": (1, 2)}),
              ("h2", "Prüfprotokoll (allkvitt check)")]
    relevant = [i for i in issues if i.level in ("fehler", "warnung")]
    if relevant:
        blocks.append(("table", ["Stufe", "Ort", "Meldung"], [[i.level, i.where, i.message] for i in relevant],
                       (0.1, 0.25, 0.65), {"wrap": (1, 2)}))
    else:
        blocks.append(("text", "Keine Fehler und keine Warnungen."))
    gaps = [g for g in k.luecken if g["art"] != "reserviert"]
    blocks += [("h2", "Belegnummern"),
               ("text", f"{len(gaps)} Lücke(n) in den Nummernserien, jede mit Grund im Lückenverzeichnis "
                        "des Belegordners." if gaps else "Die Nummernserien sind lückenlos.")]
    blocks += [("page",), ("h2", "Prüfsummen (SHA-256)"),
               ("small", "Mit diesen Prüfsummen lässt sich später nachweisen, dass eine Datei unverändert ist. "
                         "Dieselben Werte stehen maschinenlesbar in manifest.json."),
               ("table", ["Datei", "SHA-256"], [[n, h] for n, h in manifest["dateien"].items()],
                (0.42, 0.58), {"wrap": (0,)})]
    with k.draft():
        return pdfmod.report_pdf(k.book, f"Abschlussunterlagen {k.year}", blocks,
                                 subtitle=f"{s.firma}{' · ' + s.get('uid') if s.get('uid') else ''} · "
                                          f"Geschäftsjahr 01.01.–31.12.{k.year}")

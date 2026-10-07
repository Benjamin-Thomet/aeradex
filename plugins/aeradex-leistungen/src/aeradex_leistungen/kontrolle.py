"""Hours control: target (Soll) against recorded hours (Ist), balance per month and year;
project budget against actual.

Soll per working day = weekly hours × pensum / 5 (from personal/, or `soll_woche` for
people without payroll); working days are Monday–Friday without the holidays in
leistungen/einstellungen.yaml, within the employment. Every recorded hour counts as
Ist — billable or not (Ferien, Krank, Intern …). Hourly-paid staff have no Soll.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from aeradex import payroll
from aeradex.book import Book, BookError
from aeradex.files import parse_date

from . import daten

ZERO = Decimal("0")


def _opt_date(value) -> date | None:
    return parse_date(value) if value not in (None, "") else None


def workdays(year: int, month: int, holidays: set[date], start: date | None = None, end: date | None = None) -> int:
    n = 0
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        if d.weekday() >= 5 or d in holidays or (start and d < start) or (end and d > end):
            continue
        n += 1
    return n


def target(p: dict, year: int, month: int, holidays: set[date], from_: date | None = None) -> Decimal | None:
    """Soll hours of the month; `from_` (start of the control) acts like a later Eintritt."""
    if p["stundenlohn"] or not p.get("woche"):
        return None
    start = max(filter(None, (_opt_date(p.get("eintritt")), from_)), default=None)
    days = workdays(year, month, holidays, start, _opt_date(p.get("austritt")))
    return (Decimal(p["woche"]) / 5 * days).quantize(Decimal("0.01"))


def month(book: Book, year: int, month_: int, wer: str = "") -> list[dict]:
    """Per person: Soll, Ist (billable / not, per category), balance of the month and of the year so far."""
    cfg = daten.settings(book)
    holidays = {parse_date(d) for d in cfg["feiertage"]}
    from_ = _opt_date(cfg["kontrolle_ab"])
    persons = daten.people(book)
    per: dict[tuple[str, int], dict] = defaultdict(lambda: {"ist": ZERO, "abrechenbar": ZERO,
                                                            "kategorien": defaultdict(lambda: ZERO)})
    for e in daten.entries(book):
        if e["art"] != "Zeit" or e["datum"].year != year or e["datum"].month > month_:
            continue
        slot = per[(e["wer"], e["datum"].month)]
        slot["ist"] += e["menge"]
        if e["abrechenbar"]:
            slot["abrechenbar"] += e["menge"]
        else:
            slot["kategorien"][e["kategorie"] or "Intern"] += e["menge"]
    out = []
    for nr, p in sorted(persons.items()):
        if wer and nr != wer.upper():
            continue
        cur = per.get((nr, month_))
        if not p["aktiv"] and cur is None:
            continue
        soll = target(p, year, month_, holidays, from_)
        ist = cur["ist"] if cur else ZERO
        saldo_jahr = None
        if soll is not None:
            saldo_jahr = Decimal(str((p.get("vortrag") or {}).get(year) or 0))
            for m in range(1, month_ + 1):
                s = target(p, year, m, holidays, from_) or ZERO
                saldo_jahr += (per[(nr, m)]["ist"] if (nr, m) in per else ZERO) - s
        out.append({"nummer": nr, "name": p["name"], "soll": soll, "ist": ist,
                    "abrechenbar": cur["abrechenbar"] if cur else ZERO,
                    "quote": (cur["abrechenbar"] / ist * 100).quantize(Decimal("1")) if cur and ist else None,
                    "kategorien": dict(sorted(cur["kategorien"].items())) if cur else {},
                    "saldo": ist - soll if soll is not None else None, "saldo_jahr": saldo_jahr,
                    "stundenlohn": p["stundenlohn"], "lohn": p["lohn"]})
    return out


def year(book: Book, year_: int, wer: str) -> list[dict]:
    """One person, month by month."""
    return [{"monat": m, **(month(book, year_, m, wer) or [{}])[0]} for m in range(1, 13)]


def project(book: Book, nr: str) -> dict:
    """Budget against actual: hours, value at billing rates, cost at cost rates, billed and open."""
    p = daten.project(book, nr)
    persons = daten.people(book)
    items = [e for e in daten.with_state(book) if e["projekt"] == p["nummer"]]
    hours = sum((e["menge"] for e in items if e["art"] == "Zeit"), ZERO)
    value = sum((daten.money(e["menge"] * e["preis"]) for e in items if e.get("preis") is not None), ZERO)
    cost = sum((daten.money(e["menge"] * (persons.get(e["wer"], {}).get("kostensatz") or ZERO))
                for e in items if e["art"] == "Zeit"), ZERO)
    cost += sum((daten.money(e["menge"] * e["preis"]) for e in items if e["art"] == "Produkt"
                 and not e["abrechenbar"]), ZERO)
    by_state: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in items:
        by_state[e["status"]] += e["betrag"]
    billed = by_state["abgerechnet"]
    if p.get("offerte"):
        from . import offerten
        try:
            q = offerten.quote(book, p["offerte"])
            billed += sum((daten.money(r["netto"]) for r in offerten.billed(book, q)), ZERO)
        except BookError:
            pass
    bh, bc = p.get("budget_stunden"), p.get("budget_chf")
    return {"nummer": p["nummer"], "name": p["name"], "kunde": p["kunde"], "abrechnung": p["abrechnung"],
            "status": p["status"], "offerte": p.get("offerte") or "",
            "budget_stunden": Decimal(str(bh)) if bh not in (None, "") else None,
            "budget_chf": daten.money(bc) if bc not in (None, "") else None,
            "stunden": hours, "wert": value, "kosten": cost, "abgerechnet": billed, "offen": by_state["offen"],
            "rest_stunden": Decimal(str(bh)) - hours if bh not in (None, "") else None,
            "auslastung": (hours / Decimal(str(bh)) * 100).quantize(Decimal("1")) if bh not in (None, "", 0) and Decimal(str(bh)) else None,
            "deckungsbeitrag": billed - cost if p["abrechnung"] == "pauschal" else value - cost}


def over_budget(book: Book) -> list[str]:
    out = []
    for nr, p in daten.projects(book).items():
        if p.get("status") != "offen":
            continue
        st = project(book, nr)
        if st["budget_stunden"] is not None and st["stunden"] > st["budget_stunden"]:
            out.append(f"Projekt {nr} {p['name']}: {st['stunden']} h von {st['budget_stunden']} h Budget")
    return out


def transfer_to_payroll(book: Book, year_: int, month_: int, wer: str = "") -> tuple[list[dict], list[Path]]:
    """Hourly-paid employees: put the month's recorded hours into the payroll run (draft payslip)."""
    rows = [r for r in month(book, year_, month_, wer) if r["stundenlohn"] and r["lohn"]]
    if not rows:
        raise BookError("Keine Stundenlöhner mit erfassten Stunden in diesem Monat")
    out, touched = [], []
    for r in rows:
        results = payroll.run(book, year_, month_, r["nummer"], {"stunden": r["ist"]})
        for meta, path in results:
            out.append({"mitarbeiter": r["nummer"], "stunden": r["ist"], "brutto": meta["werte"]["bruttolohn"]})
            touched.append(path)
    return out, touched


def month_range(year_: int, month_: int) -> tuple[date, date]:
    return date(year_, month_, 1), date(year_, month_, calendar.monthrange(year_, month_)[1])


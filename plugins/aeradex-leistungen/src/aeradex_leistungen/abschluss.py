"""Year-end proposals from the recorded work: angefangene Arbeiten and Ferien-/Überstundenguthaben per 31.12.

Both are proposals for the Abschluss page (`aeradex_abschluss_vorschlaege`); a person checks and books them.

- Angefangene Arbeiten: work up to 31.12. not invoiced yet (and the hours of lump-sum projects still running),
  per customer/project, at cost (hours × the person's Kostensatz, products as recorded). Without a Kostensatz the
  billing value is shown with a note, to be reduced to cost by hand.
- Ferien/Überstunden: per employee the holidays left and the overtime balance per 31.12., at the hourly wage
  (monthly wage × 12 / (full-time hours × 52), or the hourly wage). Hourly-paid staff get their holidays with
  the wage (Ferienzuschlag): only their overtime counts.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from aeradex import invoices, payroll
from aeradex.book import Book, BookError

from . import abrechnung, abwesenheit, daten, kontrolle

ZERO = Decimal("0")


def _money(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _chf(v) -> str:
    return f"{Decimal(str(v)):,.2f}".replace(",", "'")


def work_in_progress(book: Book, year: int) -> list[dict]:
    end = date(int(year), 12, 31)
    people = daten.people(book)
    projects = daten.projects(book)
    names = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    entries = abrechnung.select(book, bis=end) + [
        e for e in abrechnung.select(book, bis=end, status="pauschal")
        if projects.get(e["projekt"], {}).get("status") != "abgeschlossen"]
    per: dict[tuple[str, str], dict] = {}
    for e in entries:
        key = (e["kunde"], e["projekt"])
        s = per.setdefault(key, {"stunden": ZERO, "kosten": ZERO, "wert": ZERO, "ohne_satz": set()})
        s["wert"] += e["betrag"] or ZERO
        if e["art"] == "Zeit":
            s["stunden"] += e["menge"]
            rate = (people.get(e["wer"]) or {}).get("kostensatz")
            if rate is None:
                s["ohne_satz"].add(e["wer"])
                s["kosten"] += e["betrag"] or ZERO
            else:
                s["kosten"] += _money(e["menge"] * rate)
        else:
            s["kosten"] += e["betrag"] or ZERO
    out = []
    for (kunde, projekt), s in sorted(per.items()):
        if not s["kosten"]:
            continue
        label = names.get(kunde, kunde) + (f", {projects.get(projekt, {}).get('name') or projekt}" if projekt else "")
        detail = f"{s['stunden']:g} h, verrechenbar {_chf(s['wert'])}"
        if s["ohne_satz"]:
            detail += (f" — ohne Kostensatz für {', '.join(sorted(s['ohne_satz']))}: Verrechnungswert eingesetzt, "
                       "auf die Herstellkosten herabsetzen")
        out.append({"text": label, "betrag": _money(s["kosten"]), "detail": detail, "quelle": "leistungen"})
    return out


def _hourly(emp: dict, person: dict) -> Decimal | None:
    if person.get("kostensatz") is not None:
        return person["kostensatz"]
    if payroll.is_hourly(emp):
        return Decimal(str(emp.get("stundenlohn") or 0)) or None
    full = Decimal(str(emp.get("vollzeit_stunden_woche") or 0))
    wage = Decimal(str(emp.get("monatslohn") or 0))
    return _money(wage * 12 / (full * 52)) if full and wage else None


def holidays_and_overtime(book: Book, year: int) -> list[dict]:
    end = date(int(year), 12, 31)
    emps = payroll.employees(book)
    out = []
    for nr, p in daten.people(book).items():
        emp = emps.get(nr)
        if not emp or not p["lohn"]:
            continue
        if p.get("eintritt") and str(p["eintritt"]) > end.isoformat():
            continue
        if p.get("austritt") and str(p["austritt"]) < end.isoformat():
            continue
        days = ZERO if p["stundenlohn"] else abwesenheit.holiday_account(book, nr, int(year), today=end)["rest"]
        try:
            row = (kontrolle.month(book, int(year), 12, nr) or [{}])[0]
        except BookError:
            row = {}
        overtime = max(row.get("saldo_jahr") or ZERO, ZERO)
        day_hours = (p.get("woche") or ZERO) / 5
        hours = max(days, ZERO) * day_hours + overtime
        if not hours:
            continue
        rate = _hourly(emp, p)
        parts = []
        if days:
            parts.append(f"{days:g} Ferientage à {day_hours:g} h")
        if overtime:
            parts.append(f"{overtime:g} Überstunden")
        out.append({"text": p["name"], "betrag": _money(hours * rate) if rate else None, "info": rate is None,
                    "detail": " + ".join(parts) + (f" = {hours:g} h × {_chf(rate)}" if rate else
                                                   " — kein Lohn hinterlegt, Betrag von Hand"),
                    "quelle": "leistungen"})
    return out


def proposals(book: Book, year: int, art: str) -> list[dict]:
    if art == "arbeiten":
        return work_in_progress(book, year)
    if art == "ferien":
        return holidays_and_overtime(book, year)
    return []


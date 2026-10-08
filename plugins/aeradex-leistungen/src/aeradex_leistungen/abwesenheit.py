"""Absences (Ferien, Krank, Unfall …), public holidays per canton, the holiday account and the
Arbeitszeitnachweis.

An absence is a period (von–bis, a share per day: 1 or 0.5) in leistungen/abwesenheiten.yaml — not a time entry.
On each working day inside it (Mon–Fri, not a holiday, within the employment) it counts as actual hours at the
person's daily target — except «Kompensation» (overtime is taken: nothing counts, the balance goes down) and
«Unbezahlt» (the target itself is reduced). Old time entries with a category (Ferien …) keep counting as before.

Public holidays: those of the holiday canton (setting `feiertage_kanton`; default the canton of the company seat,
from its postcode), from data/feiertage.json (tools/feiertage.py), plus `feiertage` added and minus `feiertage_ohne`
removed by hand. They lower the target like a weekend."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from aeradex.book import Book, BookError
from aeradex.files import parse_date, read_yaml, write_yaml

from . import daten

ZERO = Decimal("0")
ARTEN = {"Ferien": "zählt", "Krank": "zählt", "Unfall": "zählt", "Militär/ZS": "zählt",
         "Mutterschaft/Vaterschaft": "zählt", "Weiterbildung": "zählt", "Kompensation": "kompensation",
         "Unbezahlt": "unbezahlt"}
FARBEN = {"Ferien": "ferien", "Krank": "krank", "Unfall": "krank", "Militär/ZS": "andere",
          "Mutterschaft/Vaterschaft": "andere", "Weiterbildung": "andere", "Kompensation": "kompensation",
          "Unbezahlt": "andere"}
DATA = Path(__file__).resolve().parent / "data" / "feiertage.json"


# ---------- public holidays ----------

@lru_cache(maxsize=1)
def _table() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))["kantone"]


def canton(book: Book) -> str:
    """The holiday canton: the setting, else the canton of the company seat ('' when unknown)."""
    cfg = daten.settings(book)
    if cfg["feiertage_kanton"]:
        return "" if cfg["feiertage_kanton"] == "keiner" else cfg["feiertage_kanton"]
    from aeradex import plz
    a = book.settings.adresse
    return plz.kanton(a.get("plz"), a.get("ort")) or ""


def holiday_names(book: Book, year: int) -> dict[date, str]:
    cfg = daten.settings(book)
    k = canton(book)
    out = {date.fromisoformat(d): n for d, n in _table().get(k, {}).items() if d.startswith(f"{year}-")}
    for raw in cfg["feiertage"]:
        d = parse_date(raw)
        if d.year == year:
            out.setdefault(d, "Feiertag")
    for raw in cfg["feiertage_ohne"]:
        out.pop(parse_date(raw), None)
    return dict(sorted(out.items()))


def holidays(book: Book, year: int) -> set[date]:
    return set(holiday_names(book, year))


# ---------- absences ----------

def path(book: Book) -> Path:
    return book.root / "leistungen" / "abwesenheiten.yaml"


def items(book: Book) -> list[dict]:
    p = path(book)
    raw = (read_yaml(p) or {}) if p.exists() else {}
    out = []
    for a in raw.get("abwesenheiten") or []:
        out.append({"id": str(a["id"]), "wer": str(a["wer"]), "art": str(a["art"]), "von": parse_date(a["von"]),
                    "bis": parse_date(a.get("bis") or a["von"]), "anteil": Decimal(str(a.get("anteil") or 1)),
                    "notiz": str(a.get("notiz") or "")})
    return sorted(out, key=lambda a: (a["von"], a["wer"], a["id"]))


def _write(book: Book, rows: list[dict]) -> Path:
    p = path(book)
    write_yaml(p, {"abwesenheiten": [{"id": a["id"], "wer": a["wer"], "art": a["art"], "von": a["von"].isoformat(),
                                      "bis": a["bis"].isoformat(), "anteil": a["anteil"], "notiz": a["notiz"]}
                                     for a in sorted(rows, key=lambda a: (a["von"], a["wer"], a["id"]))]})
    return p


def add(book: Book, wer: str, art: str, von, bis=None, anteil=1, notiz: str = "") -> tuple[dict, list[Path]]:
    p = daten.person(book, wer)
    if art not in ARTEN:
        raise BookError(f"Art: {', '.join(ARTEN)}")
    v = parse_date(von, "von")
    b = parse_date(bis, "bis") if bis not in (None, "") else v
    if b < v:
        raise BookError("«bis» liegt vor «von»")
    share = Decimal(str(anteil or 1))
    if share not in (Decimal("1"), Decimal("0.5")):
        raise BookError("Anteil: ganzer (1) oder halber Tag (0.5)")
    rows = items(book)
    clash = [a for a in rows if a["wer"] == p["nummer"] and a["von"] <= b and v <= a["bis"]]
    if clash:
        raise BookError(f"Überschneidet sich mit {clash[0]['art']} {clash[0]['von']:%d.%m.}–{clash[0]['bis']:%d.%m.%Y}")
    nums = [int(a["id"][2:]) for a in rows if a["id"].startswith("A-") and a["id"][2:].isdigit()]
    new = {"id": f"A-{max(nums, default=0) + 1:04d}", "wer": p["nummer"], "art": art, "von": v, "bis": b,
           "anteil": share, "notiz": notiz.strip()}
    return new, [_write(book, rows + [new])]


def delete(book: Book, aid: str) -> tuple[dict, list[Path]]:
    rows = items(book)
    rest = [a for a in rows if a["id"] != aid]
    if len(rest) == len(rows):
        raise BookError(f"Abwesenheit {aid} nicht gefunden")
    return {"id": aid}, [_write(book, rest)]


def workday(person: dict, d: date, hols: set[date]) -> bool:
    if d.weekday() >= 5 or d in hols:
        return False
    start = parse_date(person["eintritt"]) if person.get("eintritt") else None
    end = parse_date(person["austritt"]) if person.get("austritt") else None
    return not (start and d < start or end and d > end)


def daily_target(person: dict) -> Decimal | None:
    if person.get("stundenlohn") or not person.get("woche"):
        return None
    return Decimal(str(person["woche"])) / 5


def on_day(book: Book, wer: str, d: date, rows: list[dict] | None = None) -> dict | None:
    for a in rows if rows is not None else items(book):
        if a["wer"] == wer and a["von"] <= d <= a["bis"]:
            return a
    return None


def day_hours(book: Book, person: dict, d: date, hols: set[date], rows: list[dict] | None = None) -> dict:
    """What an absence contributes on day `d`: actual hours, and how much the target is reduced."""
    a = on_day(book, person["nummer"], d, rows)
    if a is None or not workday(person, d, hols):
        return {"stunden": ZERO, "soll_weniger": ZERO, "art": a["art"] if a else None, "anteil": a["anteil"] if a else ZERO}
    per_day = daily_target(person) or ZERO
    share = (per_day * a["anteil"]).quantize(Decimal("0.01"))
    kind = ARTEN[a["art"]]
    return {"stunden": share if kind == "zählt" else ZERO, "soll_weniger": share if kind == "unbezahlt" else ZERO,
            "art": a["art"], "anteil": a["anteil"]}


def month_totals(book: Book, person: dict, year: int, month: int) -> dict:
    """Per art: days and hours in the month; plus the hours counted as actual and the target reduction."""
    import calendar
    hols = holidays(book, year)
    rows = [a for a in items(book) if a["wer"] == person["nummer"]]
    out = {"ist": ZERO, "soll_weniger": ZERO, "arten": {}}
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        h = day_hours(book, person, d, hols, rows)
        if not h["art"] or not workday(person, d, hols):
            continue
        slot = out["arten"].setdefault(h["art"], {"tage": ZERO, "stunden": ZERO})
        slot["tage"] += h["anteil"]
        slot["stunden"] += h["stunden"]
        out["ist"] += h["stunden"]
        out["soll_weniger"] += h["soll_weniger"]
    return out


# ---------- holiday account ----------

def entitlement(book: Book, person: dict, year: int) -> Decimal:
    """Holiday days of the year: the person's or the general setting, pro rata to the employment within the
    year (rounded to half days)."""
    days = Decimal(str(person.get("ferien_tage") or daten.settings(book)["ferien_tage"]))
    first, last = date(year, 1, 1), date(year, 12, 31)
    start = max(first, parse_date(person["eintritt"])) if person.get("eintritt") else first
    end = min(last, parse_date(person["austritt"])) if person.get("austritt") else last
    if end < start:
        return ZERO
    share = Decimal((end - start).days + 1) / Decimal((last - first).days + 1)
    return (days * share * 2).quantize(Decimal("1")) / 2


def holiday_account(book: Book, wer: str, year: int, today: date | None = None) -> dict:
    today = today or date.today()
    p = daten.person(book, wer)
    hols = holidays(book, year)
    taken = planned = ZERO
    for a in items(book):
        if a["wer"] != p["nummer"] or a["art"] != "Ferien":
            continue
        d = max(a["von"], date(year, 1, 1))
        while d <= min(a["bis"], date(year, 12, 31)):
            if workday(p, d, hols):
                if d <= today:
                    taken += a["anteil"]
                else:
                    planned += a["anteil"]
            d += timedelta(days=1)
    carry = Decimal(str((p.get("ferien_vortrag") or {}).get(year) or (p.get("ferien_vortrag") or {}).get(str(year)) or 0))
    claim = entitlement(book, p, year)
    return {"wer": p["nummer"], "name": p["name"], "jahr": year, "anspruch": claim, "vortrag": carry,
            "bezogen": taken, "geplant": planned, "rest": claim + carry - taken - planned}


# ---------- Arbeitszeitnachweis (ArGV 1 Art. 73) ----------

def time_record(book: Book, wer: str, year: int, month: int) -> dict:
    """Day by day: target, hours worked, absence, actual, running balance of the month."""
    import calendar
    from . import kontrolle
    p = daten.person(book, wer)
    hols = holiday_names(book, year)
    rows = [a for a in items(book) if a["wer"] == p["nummer"]]
    worked: dict[date, Decimal] = {}
    for e in daten.entries(book):
        if e["art"] == "Zeit" and e["wer"] == p["nummer"] and e["datum"].year == year and e["datum"].month == month:
            worked[e["datum"]] = worked.get(e["datum"], ZERO) + e["menge"]
    days, balance = [], ZERO
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        soll = kontrolle.day_target(p, d, set(hols)) or ZERO
        absent = day_hours(book, p, d, set(hols), rows)
        soll -= absent["soll_weniger"]
        ist = worked.get(d, ZERO) + absent["stunden"]
        if daily_target(p) is not None:
            balance += ist - soll
        days.append({"datum": d, "tag": "Mo Di Mi Do Fr Sa So".split()[d.weekday()], "soll": soll,
                     "gearbeitet": worked.get(d, ZERO), "abwesenheit": absent["art"] or "",
                     "abwesend": absent["stunden"], "feiertag": hols.get(d, ""), "ist": ist, "saldo": balance})
    total = {k: sum((x[k] for x in days), ZERO) for k in ("soll", "gearbeitet", "abwesend", "ist")}
    return {"person": p, "jahr": year, "monat": month, "tage": days, "total": total,
            "saldo": total["ist"] - total["soll"] if daily_target(p) is not None else None}


def time_record_pdf(book: Book, wer: str, year: int, month: int) -> bytes:
    from aeradex import pdf
    from aeradex.book import MONTHS_DE
    r = time_record(book, wer, year, month)
    q = lambda v: f"{v:.2f}" if v else ""  # noqa: E731
    rows = [[f"{x['tag']} {x['datum']:%d.%m.}", q(x["soll"]), q(x["gearbeitet"]),
             (x["abwesenheit"] + (f" {q(x['abwesend'])}" if x["abwesend"] else "")) or x["feiertag"],
             q(x["ist"]), f"{x['saldo']:.2f}" if r["saldo"] is not None else ""] for x in r["tage"]]
    rows.append(["Total", q(r["total"]["soll"]), q(r["total"]["gearbeitet"]), q(r["total"]["abwesend"]),
                 q(r["total"]["ist"]), f"{r['saldo']:.2f}" if r["saldo"] is not None else ""])
    blocks = [("table", ["Tag", "Soll", "Gearbeitet", "Abwesenheit / Feiertag", "Ist", "Saldo"], rows,
               [0.14, 0.12, 0.14, 0.32, 0.12, 0.16], {"right": (1, 2, 4, 5), "totals": (len(rows) - 1,), "zebra": True}),
              ("small", "Arbeitszeitnachweis nach Art. 73 ArGV 1: tägliche Arbeitszeit, Abwesenheiten und Saldo. "
                        "Soll = Wochenstunden × Pensum ÷ 5 je Arbeitstag (Mo–Fr ohne Feiertage)."),
              ("text", " "), ("small", "Datum, Unterschrift Mitarbeiter/in ____________________    "
                                        "Datum, Unterschrift Arbeitgeber ____________________")]
    return pdf.report_pdf(book, f"Arbeitszeitnachweis {r['person']['name']}", blocks,
                          subtitle=f"{MONTHS_DE[month]} {year} · {r['person']['nummer']}")

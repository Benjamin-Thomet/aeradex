"""Budgets per account and month: budget/<JJJJ>.yaml.

    konten:
      "3400": {jahr: 240000}                  # spread evenly, the Rappen go to December
      "6000": {monate: [2000, 2000, …]}       # twelve months

Amounts are natural: Ertrag and Aufwand both positive. Only Erfolgskonten carry a
budget. `from_prior` builds a budget from last year's actuals (with their monthly
pattern, so seasonality is kept) plus a percentage.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .book import Book, BookError
from .files import FormatError, parse_amount, read_yaml, write_yaml

ZERO = Decimal("0")
CENT = Decimal("0.01")


def path(book: Book, year: int) -> Path:
    return book.root / "budget" / f"{int(year)}.yaml"


def exists(book: Book, year: int) -> bool:
    return path(book, year).exists()


def _money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def spread(total: Decimal) -> list[Decimal]:
    part = (total / 12).quantize(CENT, rounding=ROUND_HALF_UP)
    months = [part] * 11
    return months + [total - sum(months, ZERO)]


def raw(book: Book, year: int) -> dict:
    p = path(book, year)
    return (read_yaml(p) or {}) if p.exists() else {}


def load(book: Book, year: int) -> dict[str, list[Decimal]]:
    """Account → twelve monthly amounts."""
    out = {}
    for nr, spec in ((raw(book, year).get("konten") or {}).items()):
        nr = str(nr)
        spec = spec or {}
        if spec.get("monate") is not None:
            months = [_money(v) for v in spec["monate"]]
            if len(months) != 12:
                raise FormatError(f"budget/{year}.yaml: Konto {nr} braucht 12 Monatswerte")
        else:
            months = spread(_money(spec.get("jahr")))
        out[nr] = months
    return out


def _save(book: Book, year: int, accounts: dict[str, list[Decimal]], note: str = "") -> Path:
    data = raw(book, year)
    konten = {}
    for nr in sorted(accounts):
        months = accounts[nr]
        total = sum(months, ZERO)
        konten[nr] = {"jahr": total} if months == spread(total) else {"monate": months}
    data["konten"] = konten
    if note:
        data["notiz"] = note
    p = path(book, year)
    write_yaml(p, data)
    return p


def _check_account(book: Book, nr: str) -> None:
    acct = book.account(str(nr))
    if not acct.is_pl:
        raise BookError(f"Konto {nr} ist kein Erfolgskonto — Budgets gibt es für Ertrag und Aufwand")


def set_account(book: Book, year: int, konto: str, jahr=None, monate: list | None = None) -> tuple[dict, list[Path]]:
    """Set (or with jahr=0 and no months remove) one account's budget."""
    _check_account(book, konto)
    accounts = load(book, year)
    if monate is not None:
        months = [_money(parse_amount(v, "monat")) for v in monate]
        if len(months) != 12:
            raise BookError("Zwölf Monatswerte angeben")
    else:
        months = spread(_money(parse_amount(jahr, "jahr")))
    if any(m < 0 for m in months):
        raise BookError("Budgetwerte sind positiv (Ertrag und Aufwand)")
    if sum(months, ZERO):
        accounts[str(konto)] = months
    else:
        accounts.pop(str(konto), None)
    return {"konto": str(konto), "monate": months, "jahr": sum(months, ZERO)}, [_save(book, year, accounts)]


def set_grid(book: Book, year: int, grid: dict[str, list]) -> tuple[dict, list[Path]]:
    """Replace the budget with a grid account → twelve values (empty rows are dropped)."""
    accounts = {}
    for nr, values in grid.items():
        months = [_money(parse_amount(v, f"{nr}")) if str(v).strip() else ZERO for v in values]
        if len(months) != 12:
            raise BookError(f"Konto {nr}: zwölf Monatswerte")
        if not sum(months, ZERO):
            continue
        _check_account(book, nr)
        if any(m < 0 for m in months):
            raise BookError(f"Konto {nr}: Budgetwerte sind positiv")
        accounts[str(nr)] = months
    return {"konten": len(accounts)}, [_save(book, year, accounts)]


def from_prior(book: Book, year: int, prozent=0, basis: int | None = None,
               runden: int = 100) -> tuple[dict, list[Path]]:
    """Budget = actuals of `basis` (default: the year before) × (1 + prozent/100), keeping the monthly
    pattern; each account's year total is rounded to `runden` francs."""
    from .ledger import BalanceEngine
    basis = int(basis or year - 1)
    engine = BalanceEngine(book)
    rows = engine.year_rows(basis)
    if not rows:
        raise BookError(f"Keine Buchungen {basis} — Budget von Hand erfassen")
    factor = 1 + parse_amount(prozent, "prozent") / 100
    per: dict[str, list[Decimal]] = {}
    for r in rows:
        for nr, sign in ((r.soll, 1), (r.haben, -1)):
            acct = book.accounts.get(nr) if nr else None
            if not acct or not acct.is_pl:
                continue
            natural = sign * r.betrag * (-1 if acct.klasse == "ertrag" else 1)
            per.setdefault(nr, [ZERO] * 12)[r.datum.month - 1] += natural
    accounts = {}
    for nr, months in per.items():
        actual = sum(months, ZERO)
        if actual <= 0:
            continue
        step = Decimal(runden or 1)
        total = (actual * factor / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
        scaled = [_money(total * m / actual) for m in months]
        scaled[11] += total - sum(scaled, ZERO)
        accounts[nr] = [max(m, ZERO) for m in scaled] if all(m >= 0 for m in scaled) else spread(total)
    p = _save(book, year, accounts, note=f"aus Ist {basis} {'+' if factor >= 1 else ''}{(factor - 1) * 100:g} %")
    return {"konten": len(accounts), "basis": basis, "total_ertrag": sum(
        (sum(m, ZERO) for nr, m in accounts.items() if book.accounts[nr].klasse == "ertrag"), ZERO),
        "total_aufwand": sum((sum(m, ZERO) for nr, m in accounts.items()
                              if book.accounts[nr].klasse == "aufwand"), ZERO)}, [p]


def window(book: Book, start: date, end: date) -> dict[str, Decimal]:
    """Budget per account for a window; partly covered months count by days."""
    out: dict[str, Decimal] = {}
    cache: dict[int, dict] = {}
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if y not in cache:
            cache[y] = load(book, y)
        days = calendar.monthrange(y, m)[1]
        a = start if (y, m) == (start.year, start.month) else date(y, m, 1)
        b = end if (y, m) == (end.year, end.month) else date(y, m, days)
        share = Decimal((b - a).days + 1) / days
        for nr, months in cache[y].items():
            v = months[m - 1] if share == 1 else _money(months[m - 1] * share)
            out[nr] = out.get(nr, ZERO) + v
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def check(book: Book) -> list[tuple[str, str, str]]:
    folder = book.root / "budget"
    out = []
    for p in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        where = f"budget/{p.name}"
        if not p.stem.isdigit():
            out.append(("warnung", where, "Dateiname muss das Jahr sein (2026.yaml)"))
            continue
        try:
            accounts = load(book, int(p.stem))
        except (FormatError, ValueError, ArithmeticError) as exc:
            out.append(("fehler", where, str(exc)))
            continue
        for nr in accounts:
            acct = book.accounts.get(nr)
            if acct is None:
                out.append(("fehler", where, f"Konto {nr} fehlt im Kontenplan"))
            elif not acct.is_pl:
                out.append(("fehler", where, f"Konto {nr} ist kein Erfolgskonto"))
        for nr, spec in ((raw(book, int(p.stem)).get("konten") or {}).items()):
            spec = spec or {}
            if spec.get("monate") is not None and spec.get("jahr") is not None:
                if _money(spec["jahr"]) != sum((_money(v) for v in spec["monate"]), ZERO):
                    out.append(("fehler", where, f"Konto {nr}: Summe der Monate ≠ jahr"))
    return out


def grid(book: Book, year: int) -> list[dict]:
    """Rows for the UI: every Erfolgskonto with its months, budget and last year's actual."""
    from .ledger import BalanceEngine
    accounts = load(book, year)
    engine = BalanceEngine(book)
    prior = engine.year_movements(year - 1)
    out = []
    for nr, acct in sorted(book.accounts.items()):
        if not acct.is_pl:
            continue
        months = accounts.get(nr)
        actual = prior.get(nr, ZERO) * (-1 if acct.klasse == "ertrag" else 1)
        if months is None and not actual and not acct.aktiv_:
            continue
        out.append({"konto": nr, "name": acct.name, "klasse": acct.klasse, "monate": months or [ZERO] * 12,
                    "jahr": sum(months or [], ZERO), "vorjahr": actual})
    return out

"""Balances, account ledgers and the year chain.

Signed convention (as in Banana and the Swiss KMU practice it follows): assets
and expenses carry a positive (debit) balance, liabilities, equity and income a
negative (credit) one. An account's balance in a year is therefore

    opening + Σ Soll − Σ Haben

Years chain without storing anything per year: a balance-sheet account opens
at last year's close, a P&L account at zero, and the whole of last year's
result folds into the Gewinnvortrag account at the opening — so the books
carry forward on their own and the prior-year column is always live.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .book import Book, Row

ZERO = Decimal("0")


def movements(rows: list[Row]) -> dict[str, Decimal]:
    """Net movement per account (Σ Soll − Σ Haben)."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        if r.soll:
            out[r.soll] += r.betrag
        if r.haben:
            out[r.haben] -= r.betrag
    return dict(out)


class BalanceEngine:
    """Year-aware balances for one book, memoised."""

    def __init__(self, book: Book):
        self.book = book
        self.accounts = book.accounts
        self.first_year = book.settings.erstes_jahr
        self.retained = book.settings.konto("gewinnvortrag")
        self._by_year: dict[int, list[Row]] = defaultdict(list)
        for r in book.rows:
            self._by_year[r.datum.year].append(r)
        self._moves: dict[int, dict[str, Decimal]] = {}
        self._opening: dict[tuple[str, int], Decimal] = {}
        self._result: dict[int, Decimal] = {}

    def year_rows(self, year: int) -> list[Row]:
        return self._by_year.get(year, [])

    def year_movements(self, year: int) -> dict[str, Decimal]:
        if year not in self._moves:
            self._moves[year] = movements(self.year_rows(year))
        return self._moves[year]

    def opening(self, nr: str, year: int) -> Decimal:
        key = (nr, year)
        if key in self._opening:
            return self._opening[key]
        acct = self.accounts.get(nr)
        if acct is None:
            value = ZERO
        elif year <= self.first_year:
            value = acct.eroeffnung if year == self.first_year else ZERO
        elif acct.is_balance_sheet:
            value = self.balance(nr, year - 1)
            if nr == self.retained:
                value += self.result(year - 1)
        else:
            value = ZERO
        self._opening[key] = value
        return value

    def balance(self, nr: str, year: int) -> Decimal:
        return self.opening(nr, year) + self.year_movements(year).get(nr, ZERO)

    def balance_at(self, nr: str, when: date) -> Decimal:
        """Balance at the end of `when` (opening of its year + movements up to it)."""
        moves = movements([r for r in self.year_rows(when.year) if r.datum <= when])
        return self.opening(nr, when.year) + moves.get(nr, ZERO)

    def range_movements(self, start: date, end: date) -> dict[str, Decimal]:
        return movements([r for r in self.year_rows(start.year) if start <= r.datum <= end])

    def prior(self, nr: str, year: int) -> Decimal:
        """The Vorjahr column: the seeded figure for the first year, else last year's close."""
        if year <= self.first_year:
            acct = self.accounts.get(nr)
            return acct.vorjahr if acct and year == self.first_year else ZERO
        return self.balance(nr, year - 1)

    def result(self, year: int) -> Decimal:
        """Signed result of the year (negative = profit)."""
        if year not in self._result:
            self._result[year] = sum((self.balance(nr, year) for nr, a in self.accounts.items()
                                      if a.is_pl), ZERO)
        return self._result[year]


def month_end(year: int, month: int) -> date:
    return date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)


PERIODS = {
    "jahr": (1, 12), "h1": (1, 6), "h2": (7, 12),
    "q1": (1, 3), "q2": (4, 6), "q3": (7, 9), "q4": (10, 12),
}


def resolve_period(year: int, period: str | None) -> tuple[date, date]:
    """'jahr', 'q1'…'q4', 'h1'/'h2', a month '03', or 'JJJJ-MM-TT..JJJJ-MM-TT'."""
    period = (period or "jahr").strip().lower()
    if period in PERIODS:
        a, b = PERIODS[period]
        return date(year, a, 1), month_end(year, b)
    if period.isdigit() and 1 <= int(period) <= 12:
        m = int(period)
        return date(year, m, 1), month_end(year, m)
    if ".." in period:
        a, b = period.split("..", 1)
        return date.fromisoformat(a), date.fromisoformat(b)
    raise ValueError(f"Unbekannte Periode '{period}' (jahr, q1–q4, h1, h2, 01–12 oder von..bis)")


def trial_balance(book: Book, year: int, start: date | None = None, end: date | None = None,
                  engine: BalanceEngine | None = None) -> list[dict]:
    """Saldenliste: opening, Soll, Haben, closing per account for a window of `year`."""
    engine = engine or BalanceEngine(book)
    start = start or date(year, 1, 1)
    end = end or date(year, 12, 31)
    before = movements([r for r in engine.year_rows(year) if r.datum < start])
    soll: dict[str, Decimal] = defaultdict(lambda: ZERO)
    haben: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in engine.year_rows(year):
        if start <= r.datum <= end:
            if r.soll:
                soll[r.soll] += r.betrag
            if r.haben:
                haben[r.haben] += r.betrag
    out = []
    for nr, acct in sorted(book.accounts.items()):
        opening = engine.opening(nr, year) + before.get(nr, ZERO)
        closing = opening + soll[nr] - haben[nr]
        if not (opening or soll[nr] or haben[nr]):
            continue
        out.append({"konto": nr, "name": acct.name, "klasse": acct.klasse,
                    "eroeffnung": opening, "soll": soll[nr], "haben": haben[nr], "saldo": closing})
    return out


def account_ledger(book: Book, nr: str, year: int, engine: BalanceEngine | None = None) -> dict:
    """Kontoblatt: opening, every row with counter-account and running balance, closing."""
    engine = engine or BalanceEngine(book)
    acct = book.account(nr)
    running = opening = engine.opening(nr, year)
    total_soll = total_haben = ZERO
    out = []
    rows = engine.year_rows(year)
    by_beleg: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_beleg[r.beleg].append(r)
    for r in rows:
        if nr not in (r.soll, r.haben):
            continue
        soll = r.betrag if r.soll == nr else ZERO
        haben = r.betrag if r.haben == nr else ZERO
        counter = r.haben if r.soll == nr else r.soll
        if not counter:
            others = {x.soll or x.haben for x in by_beleg[r.beleg] if x is not r} - {nr, ""}
            counter = "div." if len(others) != 1 else others.pop()
        running += soll - haben
        total_soll += soll
        total_haben += haben
        out.append({"datum": r.datum, "beleg": r.beleg, "text": r.text, "gegenkonto": counter,
                    "soll": soll, "haben": haben, "saldo": running, "quelle": r.quelle})
    return {"konto": nr, "name": acct.name, "jahr": year, "eroeffnung": opening,
            "zeilen": out, "total_soll": total_soll, "total_haben": total_haben, "saldo": running}

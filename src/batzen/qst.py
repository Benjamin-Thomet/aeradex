"""Quellensteuer tariff lookup.

Reads the JSON tables produced by `qst_import.py` and answers the one question
payroll has: given a tariff code and a satzbestimmendes Einkommen, what
percentage applies?

A tariff code is family + children + church tax, e.g. `C0Y` — Tarif C
(verheiratet, Doppelverdiener), no children, with church tax. Tarif H starts at
one child, so `H0Y` does not exist.

Two amounts drive a Quellensteuer deduction and they are usually different:
the *satzbestimmendes Einkommen* picks the percentage from the table, and that
percentage is then applied to the actual gross. See KS 45 Ziff. 6.4/6.5 for how
the satzbestimmendes Einkommen is derived for part-time and hourly work.
"""
from __future__ import annotations

import json
import re
from bisect import bisect_left
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

TARIF_DIR = Path(__file__).resolve().parent / "data" / "qst_tarife"

CODE_RE = re.compile(r"^([ABCH])([0-5])([YN])$")
KIST_LABEL = {"Y": "mit Kirchensteuer", "N": "ohne Kirchensteuer"}


class UnknownTariff(ValueError):
    """Raised for a malformed code or a canton/year with no table on file."""


def parse_code(code: str) -> tuple[str, int, str]:
    """Split "C0Y" into ("C", 0, "Y"). Raises UnknownTariff on anything else."""
    match = CODE_RE.match((code or "").strip().upper())
    if not match:
        raise UnknownTariff(f"Ungültiger Tarifcode: {code!r}")
    family, children, kist = match.group(1), int(match.group(2)), match.group(3)
    if family == "H" and children == 0:
        raise UnknownTariff("Tarif H gilt für Alleinerziehende und beginnt bei einem Kind")
    return family, children, kist


@lru_cache(maxsize=None)
def _table(kanton: str, jahr: int) -> dict:
    path = TARIF_DIR / f"{kanton.upper()}-{jahr}.json"
    if not path.exists():
        raise UnknownTariff(f"Keine Tariftabelle für {kanton.upper()} {jahr}")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _steps(kanton: str, jahr: int, family: str, kist: str) -> tuple[tuple, ...]:
    """The tariff steps as an immutable tuple, plus a parallel list of upper
    bounds so a lookup is a bisect rather than a scan over ~580 rows."""
    block = _table(kanton, jahr)["familien"].get(family)
    if block is None:
        raise UnknownTariff(f"Tarif {family} fehlt in {kanton.upper()} {jahr}")
    rows = block["stufen"].get(kist)
    if not rows:
        raise UnknownTariff(f"Tarif {family}{kist} fehlt in {kanton.upper()} {jahr}")
    return tuple(tuple(row) for row in rows)


@lru_cache(maxsize=None)
def _uppers(kanton: str, jahr: int, family: str, kist: str) -> tuple[int, ...]:
    return tuple(row[1] for row in _steps(kanton, jahr, family, kist))


def available() -> list[tuple[str, int]]:
    """Every (Kanton, Jahr) that has a table on file, newest year first."""
    found = []
    for path in TARIF_DIR.glob("*-*.json"):
        kanton, _, jahr = path.stem.partition("-")
        if jahr.isdigit():
            found.append((kanton.upper(), int(jahr)))
    return sorted(found, key=lambda kj: (kj[0], -kj[1]))


def codes(kanton: str, jahr: int) -> list[tuple[str, str]]:
    """All valid codes for this table as (code, label), in tariff order."""
    families = _table(kanton, jahr)["familien"]
    out = []
    for family in ("A", "B", "C", "H"):
        block = families.get(family)
        if not block:
            continue
        first = block.get("erste_kinderspalte", 0)
        width = len(block["stufen"]["Y"][0]) - 2
        for children in range(first, first + width):
            for kist in ("Y", "N"):
                kids = ("ohne Kind" if children == 0
                        else "1 Kind" if children == 1 else f"{children} Kinder")
                out.append((f"{family}{children}{kist}",
                            f"{family}{children}{kist} — {block['bezeichnung']}, "
                            f"{kids}, {KIST_LABEL[kist]}"))
    return out


def label(code: str, kanton: str, jahr: int) -> str:
    """Human-readable description of a code, or the bare code if unknown."""
    return dict(codes(kanton, jahr)).get((code or "").strip().upper(), code or "")


def rate(kanton: str, jahr: int, code: str, satzbestimmend) -> Decimal:
    """The tariff percentage as a fraction — 12.34 % comes back as 0.1234.

    `satzbestimmend` is the rate-determining monthly income, not the gross.
    Amounts at or below zero give 0. Amounts above the last published step keep
    that step's rate, which is how the tables are meant to be read: the top row
    is open-ended even though it prints as a range."""
    family, children, kist = parse_code(code)
    amount = Decimal(str(satzbestimmend or 0))
    if amount <= 0:
        return Decimal("0")

    steps = _steps(kanton, jahr, family, kist)
    first = _table(kanton, jahr)["familien"][family].get("erste_kinderspalte", 0)
    column = 2 + children - first
    if not 2 <= column < len(steps[0]):
        raise UnknownTariff(f"Tarif {family} kennt keine Spalte für {children} Kind(er)")

    index = bisect_left(_uppers(kanton, jahr, family, kist), amount)
    if index >= len(steps):        # above the published table — top step applies
        index = len(steps) - 1
    return Decimal(str(steps[index][column])) / Decimal("100")


def step(kanton: str, jahr: int, code: str, satzbestimmend) -> tuple[int, int] | None:
    """The (from, to) bounds of the step a lookup landed in — for showing the
    reader which row of the printed table an amount was taken from."""
    family, children, kist = parse_code(code)
    amount = Decimal(str(satzbestimmend or 0))
    if amount <= 0:
        return None
    steps = _steps(kanton, jahr, family, kist)
    index = min(bisect_left(_uppers(kanton, jahr, family, kist), amount), len(steps) - 1)
    return steps[index][0], steps[index][1]

"""Foreign exchange: official daily rates (BAZG, the rates the ESTV uses for MWST).

Source: https://www.backend-rates.bazg.admin.ch/api/xmldaily?d=YYYYMMDD&locale=de
Rates are cached per day under .batzen/kurse/<JJJJ-MM-TT>.yaml so a book can be
re-evaluated offline and the rate used stays documented. A rate quoted per 100
units (e.g. "100 JPY") is normalised to 1 unit.

Status: rate fetching only. Foreign-currency accounts, bookings and the
revaluation at the balance-sheet date are not implemented yet.
"""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .book import Book, BookError
from .files import read_yaml, write_yaml

URL = "https://www.backend-rates.bazg.admin.ch/api/xmldaily?d={d}&locale=de"


def parse(data: bytes) -> tuple[str, dict[str, Decimal]]:
    root = ET.fromstring(data)
    ns = {"r": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    p = "r:" if ns else ""
    datum = root.findtext(f"{p}datum", namespaces=ns) or ""
    rates = {}
    for dev in root.findall(f"{p}devise", ns):
        code = (dev.get("code") or "").upper()
        unit = (dev.findtext(f"{p}waehrung", namespaces=ns) or "1").split()[0]
        kurs = dev.findtext(f"{p}kurs", namespaces=ns)
        if code and kurs:
            rates[code] = Decimal(kurs) / Decimal(unit)
    return datum, rates


def rates(book: Book, day: date, fetch=None) -> dict[str, Decimal]:
    """Rates valid for `day` (CHF per 1 unit). Weekends/holidays fall back to the last published day."""
    cache = book.root / ".batzen" / "kurse"
    for back in range(0, 8):
        d = day - timedelta(days=back)
        path = cache / f"{d.isoformat()}.yaml"
        if path.exists():
            return {k: Decimal(str(v)) for k, v in read_yaml(path).get("kurse", {}).items()}
        try:
            data = (fetch or _get)(URL.format(d=d.strftime("%Y%m%d")))
            _, found = parse(data)
        except Exception:
            found = {}
        if found:
            write_yaml(path, {"datum": d.isoformat(), "quelle": "BAZG Tageskurse", "kurse": found})
            return found
    raise BookError(f"Keine BAZG-Kurse für {day} gefunden")


def rate(book: Book, currency: str, day: date, fetch=None) -> Decimal:
    currency = currency.upper()
    if currency == "CHF":
        return Decimal(1)
    table = rates(book, day, fetch)
    if currency not in table:
        raise BookError(f"Kein BAZG-Kurs für {currency} am {day}")
    return table[currency]


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()

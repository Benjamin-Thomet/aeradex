"""The date or period of the service (Leistungsdatum / Leistungszeitraum).

Documents keep it as `leistung_von` / `leistung_bis` (bis empty = a single day); the journal keeps it in the
booking text in one fixed form, ` · Leistung 01.10.2026–30.09.2027`, so that every booking — from a supplier bill,
a receipt, an own invoice or typed by hand — can be found again for the Rechnungsabgrenzung at year end
(abgrenzung.py). Reading a document: `parse` finds a labelled period ("Leistungszeitraum", "Lieferdatum",
"Abo …") or an unlabelled date range."""
from __future__ import annotations

import calendar
import re
from datetime import date

from .book import BookError
from .files import parse_date

LABEL = "Leistung"
MONTHS = {"januar": 1, "jan": 1, "january": 1, "janvier": 1, "gennaio": 1,
          "februar": 2, "feb": 2, "february": 2, "février": 2, "fevrier": 2, "febbraio": 2,
          "märz": 3, "maerz": 3, "mär": 3, "mar": 3, "march": 3, "mars": 3, "marzo": 3,
          "april": 4, "apr": 4, "avril": 4, "aprile": 4,
          "mai": 5, "may": 5, "maggio": 5,
          "juni": 6, "jun": 6, "june": 6, "juin": 6, "giugno": 6,
          "juli": 7, "jul": 7, "july": 7, "juillet": 7, "luglio": 7,
          "august": 8, "aug": 8, "août": 8, "aout": 8, "agosto": 8,
          "september": 9, "sep": 9, "sept": 9, "septembre": 9, "settembre": 9,
          "oktober": 10, "okt": 10, "october": 10, "oct": 10, "octobre": 10, "ottobre": 10,
          "november": 11, "nov": 11, "novembre": 11,
          "dezember": 12, "dez": 12, "december": 12, "dec": 12, "décembre": 12, "decembre": 12, "dicembre": 12}
_HINT = re.compile(r"(leistungs(?:datum|zeitraum|periode)|liefer(?:datum|termin|zeitraum)|abrechnungs(?:zeitraum|periode)|"
                   r"zeitraum|periode|laufzeit|vertragsdauer|abo(?:nnement)?|service period|period of service|"
                   r"delivery date|date de (?:livraison|prestation)|période|periodo|data (?:di consegna|della prestazione))",
                   re.I)
_D = r"(\d{1,2})\.\s?(\d{1,2})\.(\s?\d{2,4})?"                      # 1.10. / 01.10.2026 / 1.10.26
_RANGE = re.compile(_D + r"\s*(?:–|-|—|bis|au|al|to|à)\s*" + _D, re.I)
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_MRANGE = re.compile(rf"\b({_MONTH_NAMES})\.?\s*(\d{{4}})?\s*(?:–|-|—|bis|au|al|to|à)\s*({_MONTH_NAMES})\.?\s*(\d{{4}})", re.I)
_MNUM = re.compile(r"\b(\d{1,2})/(\d{4})\s*(?:–|-|—|bis|to)\s*(\d{1,2})/(\d{4})\b")
_MONTH = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{4}})\b", re.I)
_SINGLE = re.compile(_D)
_IN_TEXT = re.compile(r"Leistung (\d{2})\.(\d{2})\.(\d{4})(?:–(\d{2})\.(\d{2})\.(\d{4}))?")


def _year(raw, fallback: int | None) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return fallback
    y = int(raw)
    return y + 2000 if y < 100 else y


def _range_from(text: str, default_year: int | None) -> tuple[date, date] | None:
    m = _RANGE.search(text)
    if m:
        d1, m1, y1, d2, m2, y2 = m.groups()
        y_end = _year(y2, default_year)
        y_start = _year(y1, y_end)
        if y_start and y_end:
            try:
                von, bis = date(y_start, int(m1), int(d1)), date(y_end, int(m2), int(d2))
            except ValueError:
                return None
            if y1 is None and von > bis:                  # «01.11.–31.01.2027»: the start lies in the year before
                von = von.replace(year=von.year - 1)
            return (von, bis) if von <= bis else None
    m = _MRANGE.search(text)
    if m:
        a, ya, b, yb = m.groups()
        y_end = int(yb)
        y_start = int(ya) if ya else (y_end if MONTHS[a.lower()] <= MONTHS[b.lower()] else y_end - 1)
        von = date(y_start, MONTHS[a.lower()], 1)
        last = MONTHS[b.lower()]
        return von, date(y_end, last, calendar.monthrange(y_end, last)[1])
    m = _MNUM.search(text)
    if m:
        m1, y1, m2, y2 = (int(x) for x in m.groups())
        if 1 <= m1 <= 12 and 1 <= m2 <= 12:
            return date(y1, m1, 1), date(y2, m2, calendar.monthrange(y2, m2)[1])
    return None


def parse(text: str, default_year: int | None = None) -> tuple[date, date | None] | None:
    """(von, bis) of the service from a document's text, or None. A labelled line wins (a period, a month, or a
    single date); otherwise an unlabelled date range anywhere. bis is None for a single day."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        m = _HINT.search(line)
        if not m:
            continue
        for chunk in (line[m.end():], lines[i + 1] if i + 1 < len(lines) else ""):
            found = _range_from(chunk, default_year)
            if found:
                return found
            mm = _MONTH.search(chunk)
            if mm:
                y, mo = int(mm.group(2)), MONTHS[mm.group(1).lower()]
                return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
            sm = _SINGLE.search(chunk)
            if sm:
                y = _year(sm.group(3), default_year)
                try:
                    return (date(y, int(sm.group(2)), int(sm.group(1))), None) if y else None
                except ValueError:
                    return None
    found = _range_from(text or "", default_year)
    return found


def normalize(von, bis=None) -> tuple[date | None, date | None]:
    """Form or file values → dates; bis equal to von collapses to a single day."""
    v = parse_date(von, "Leistung von") if von else None
    b = parse_date(bis, "Leistung bis") if bis else None
    if b and not v:
        raise BookError("Leistung: «von» fehlt")
    if v and b and b < v:
        raise BookError("Leistung: «bis» liegt vor «von»")
    return v, (b if b and b != v else None)


def label(von, bis=None) -> str:
    """'Leistung 01.10.2026–30.09.2027' or 'Leistung 15.03.2026'; '' without a date."""
    v, b = normalize(von, bis)
    if not v:
        return ""
    return f"{LABEL} {v:%d.%m.%Y}" + (f"–{b:%d.%m.%Y}" if b else "")


def with_text(text: str, von, bis=None) -> str:
    """The booking text with the period appended (once)."""
    tag = label(von, bis)
    if not tag or tag in (text or ""):
        return text
    return f"{text} · {tag}" if text else tag


def from_text(text: str) -> tuple[date, date] | None:
    """The period a booking text carries (von, bis; a single day gives von == bis), or None."""
    m = _IN_TEXT.search(text or "")
    if not m:
        return None
    g = m.groups()
    try:
        von = date(int(g[2]), int(g[1]), int(g[0]))
        bis = date(int(g[5]), int(g[4]), int(g[3])) if g[3] else von
    except ValueError:
        return None
    return (von, bis) if von <= bis else None

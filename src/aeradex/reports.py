"""Reports for any period: Erfolgsrechnung and Bilanz with month/quarter columns and
comparison (Vorperiode, Vorjahr, Budget), Geldflussrechnung, Kennzahlen,
Kreditoren-Alter, Umsatz. The engine computes; nothing here formats for a medium.

Every report has the same shape, so the UI, PDF, Excel and CSV render it alike:

    {"typ", "titel", "untertitel", "spalten": [{"key", "label", "art", "von", "bis"}],
     "zeilen": [{"label", "stil": line|zwischentotal|total|kopf, "werte": {key: Decimal|None},
                 "konten": [{"konto", "name", "werte"}], "einheit"?, "ampel"?, "formel"?}],
     "hinweise": [...], "diagramm"?: {...}}

Figures follow the reading convention of the Jahresrechnung (`statements.py`):
Aktiven, Passiven and Ertrag positive, Aufwand negative. The Jahresrechnung itself
stays the legally relevant document; for a full year these reports equal it.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from .book import MONTHS_DE, Book, BookError
from .ledger import BalanceEngine, month_end, movements, resolve_period
from .statements import _ANLAGE, _FK_KURZ, _FK_LANG, _UMLAUF, GROUP_LABEL, _effective_group

ZERO = Decimal("0")
CENT = Decimal("0.01")
SPALTEN = ("gesamt", "monat", "quartal")
VERGLEICHE = ("keine", "vorperiode", "vorjahr", "budget")
MONTHS_SHORT = ["", "Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


@dataclass
class Column:
    key: str
    label: str
    art: str = "ist"                 # ist | budget | vergleich | diff | pct | text
    start: date | None = None
    end: date | None = None
    ref: tuple[str, str] | None = None   # diff/pct: (ist key, comparison key)

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "art": self.art, "von": self.start, "bis": self.end}


# ---------- periods and columns ----------

def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:                                   # 29 February
        return d.replace(year=d.year + years, day=28)


def _months_span(start: date, end: date) -> int | None:
    """Whole months covered when the window runs from a 1st to a month end, else None."""
    if start.day != 1 or end != month_end(end.year, end.month):
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def previous_period(start: date, end: date) -> tuple[date, date]:
    n = _months_span(start, end)
    if n:
        a = _add_months(start, -n)
        b = _add_months(start, -1)
        return a, month_end(b.year, b.month)
    days = (end - start).days + 1
    return start - timedelta(days=days), start - timedelta(days=1)


def window_label(start: date, end: date) -> str:
    n = _months_span(start, end)
    if n == 1:
        return f"{MONTHS_SHORT[start.month]} {start.year}"
    if n == 3 and start.month % 3 == 1:
        return f"Q{(start.month + 2) // 3} {start.year}"
    if n == 12 and start.month == 1:
        return str(start.year)
    if n and start.year == end.year:
        return f"{MONTHS_SHORT[start.month]}–{MONTHS_SHORT[end.month]} {start.year}"
    return f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"


def window(year: int, periode: str = "jahr", von=None, bis=None) -> tuple[date, date]:
    from .files import parse_date
    if von or bis:
        a = parse_date(von, "von") if von else date(year, 1, 1)
        b = parse_date(bis, "bis") if bis else date(year, 12, 31)
    else:
        try:
            a, b = resolve_period(year, periode)
        except ValueError as exc:
            raise BookError(str(exc)) from None
    if b < a:
        raise BookError("Periode: «bis» liegt vor «von»")
    return a, b


def split(start: date, end: date, spalten: str) -> list[tuple[date, date]]:
    if spalten not in SPALTEN:
        raise BookError(f"Spalten: {', '.join(SPALTEN)}")
    if spalten == "gesamt":
        return [(start, end)]
    step = 1 if spalten == "monat" else 3
    out, cur = [], date(start.year, start.month, 1)
    if spalten == "quartal":
        cur = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    while cur <= end:
        nxt = _add_months(cur, step)
        a, b = max(cur, start), min(nxt - timedelta(days=1), end)
        out.append((a, b))
        cur = nxt
    return out


def columns(year: int, periode: str = "jahr", spalten: str = "gesamt", vergleich: str = "keine",
            von=None, bis=None) -> list[Column]:
    if vergleich not in VERGLEICHE:
        raise BookError(f"Vergleich: {', '.join(VERGLEICHE)}")
    start, end = window(year, periode, von, bis)
    parts = split(start, end, spalten)
    cols = [Column(f"c{i}", window_label(a, b), "ist", a, b) for i, (a, b) in enumerate(parts)]
    if len(parts) > 1:
        cols.append(Column("total", "Total", "ist", start, end))
    main = cols[-1]
    if vergleich != "keine":
        if vergleich == "vorjahr":
            a, b = _shift_year(start, -1), _shift_year(end, -1)
            cols.append(Column("ref", "Vorjahr", "vergleich", a, b))
        elif vergleich == "vorperiode":
            a, b = previous_period(start, end)
            cols.append(Column("ref", f"Vorperiode {window_label(a, b)}", "vergleich", a, b))
        else:
            cols.append(Column("ref", "Budget", "budget", start, end))
        cols.append(Column("diff", "Abweichung", "diff", ref=(main.key, "ref")))
        cols.append(Column("pct", "Abw. %", "pct", ref=(main.key, "ref")))
    return cols


# ---------- values ----------

def window_moves(engine: BalanceEngine, start: date, end: date) -> dict[str, Decimal]:
    """Net movement (Soll − Haben) per account between two dates, across years."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for y in range(start.year, end.year + 1):
        for nr, v in movements([r for r in engine.year_rows(y) if start <= r.datum <= end]).items():
            out[nr] += v
    return dict(out)


def balance_before(engine: BalanceEngine, nr: str, day: date) -> Decimal:
    """Balance at the start of `day` (the opening of its year carries last year's closing)."""
    moves = movements([r for r in engine.year_rows(day.year) if r.datum < day])
    return engine.opening(nr, day.year) + moves.get(nr, ZERO)


def _signed_budget(book: Book, start: date, end: date) -> dict[str, Decimal]:
    from . import budget
    out = {}
    for nr, amount in budget.window(book, start, end).items():
        acct = book.accounts.get(nr)
        if acct is None:
            continue
        out[nr] = -amount if acct.klasse == "ertrag" else amount
    return out


def _finish(cols: list[Column], rows: list[dict]) -> None:
    """Fill diff and pct columns, round."""
    def fill(werte: dict):
        for c in cols:
            if c.art == "diff":
                a, b = werte.get(c.ref[0]), werte.get(c.ref[1])
                werte[c.key] = (a or ZERO) - b if b is not None else None
            elif c.art == "pct":
                if werte.get(c.ref[1]) is None:
                    werte[c.key] = None
                    continue
                a, b = werte.get(c.ref[0]) or ZERO, werte.get(c.ref[1]) or ZERO
                werte[c.key] = ((a - b) / abs(b) * 100).quantize(Decimal("0.1"), ROUND_HALF_UP) if b else None
    for r in rows:
        fill(r["werte"])
        for k in r.get("konten", []):
            fill(k["werte"])


def _line(rows: list, cols: list[Column], label: str, members: dict, codes: list[str], style: str = "line",
          sign: int = 1, keep_zero: bool = False, exclude: tuple = (), detail: bool = True) -> dict:
    """One row: per column the sum of the codes' member accounts. members[col][code] = [(nr, name, value)]."""
    werte, konten = {}, {}
    for c in cols:
        if c.art in ("diff", "pct"):
            continue
        total = ZERO
        for code in codes:
            for nr, name, v in members[c.key].get(code, []):
                if nr in exclude:
                    continue
                total += v
                if detail and v:
                    konten.setdefault(nr, {"konto": nr, "name": name, "werte": {}})["werte"][c.key] = v * sign + ZERO
        werte[c.key] = total * sign + ZERO                       # + 0 turns −0 into 0
    row = {"label": label, "stil": style, "werte": werte, "_codes": codes,
           "konten": [konten[k] for k in sorted(konten)] if style == "line" else []}
    for k in row["konten"]:
        for c in cols:
            if c.art not in ("diff", "pct"):
                k["werte"].setdefault(c.key, ZERO)
    if style == "line" and not keep_zero and not any(werte.values()):
        return row
    rows.append(row)
    return row


def _sum(*rows_werte: dict) -> dict:
    out: dict = defaultdict(lambda: ZERO)
    for w in rows_werte:
        for k, v in w.items():
            out[k] += v or ZERO
    return dict(out)


def _fingerprint(report: dict) -> str:
    def plain(v):
        return str(v) if isinstance(v, (Decimal, date)) else v
    blob = json.dumps([[r["label"], {k: plain(v) for k, v in sorted(r["werte"].items())}] for r in report["zeilen"]],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _report(typ: str, titel: str, untertitel: str, cols: list[Column], rows: list[dict], **extra) -> dict:
    for r in rows:
        r.pop("_codes", None)
    out = {"typ": typ, "titel": titel, "untertitel": untertitel, "spalten": [c.as_dict() for c in cols],
           "zeilen": rows, "hinweise": extra.pop("hinweise", []), **extra}
    out["fingerprint"] = _fingerprint(out)
    return out


# ---------- Erfolgsrechnung ----------

def erfolgsrechnung(book: Book, cols: list[Column], detail: bool = True) -> dict:
    engine = BalanceEngine(book)
    members: dict[str, dict[str, list]] = {}
    for c in cols:
        if c.art in ("diff", "pct", "text"):
            continue
        values = _signed_budget(book, c.start, c.end) if c.art == "budget" else window_moves(engine, c.start, c.end)
        per: dict[str, list] = defaultdict(list)
        for nr, acct in sorted(book.accounts.items()):
            if acct.is_pl:
                per[acct.gruppe].append((nr, acct.name, values.get(nr, ZERO)))
        members[c.key] = per
    rows: list[dict] = []

    def L(label, codes, **k):
        return _line(rows, cols, label, members, codes, sign=-1, detail=detail, **k)

    L("Betriebsertrag aus Lieferungen und Leistungen", codes=["ertrag"])
    L(GROUP_LABEL["material"], codes=["material"])
    L("Bruttoergebnis", codes=["ertrag", "material"], style="zwischentotal")
    L(GROUP_LABEL["personal"], codes=["personal"])
    L(GROUP_LABEL["betrieb"], codes=["betrieb"])
    L("Betriebliches Ergebnis vor Abschreibungen, Finanzerfolg und Steuern (EBITDA)",
      codes=["ertrag", "material", "personal", "betrieb"], style="zwischentotal")
    L(GROUP_LABEL["abschreibungen"], codes=["abschreibungen"])
    L("Betriebliches Ergebnis vor Finanzerfolg und Steuern (EBIT)",
      codes=["ertrag", "material", "personal", "betrieb", "abschreibungen"], style="zwischentotal")
    for code in ("finanz", "nebenbetrieb", "betriebsfremd", "ausserordentlich", "steuern", "abschluss"):
        L(GROUP_LABEL[code], codes=[code])
    full_year = all(c.start == date(c.start.year, 1, 1) and c.end == date(c.start.year, 12, 31)
                    for c in cols if c.art == "ist")
    L("Jahresgewinn / Jahresverlust" if full_year else "Ergebnis der Periode",
      codes=["ertrag", "material", "personal", "betrieb", "abschreibungen", "finanz", "nebenbetrieb",
             "betriebsfremd", "ausserordentlich", "steuern", "abschluss"], style="total", keep_zero=True)
    for c in cols:
        if c.art == "budget":                      # a line without any budget shows «–», not a false variance
            budgeted = {nr for nr, v in _signed_budget(book, c.start, c.end).items() if v}
            for r in rows:
                if r["stil"] == "line" and not any(nr in budgeted for code in r["_codes"]
                                                    for nr, _, _ in members[c.key].get(code, [])):
                    r["werte"][c.key] = None
                for k in r["konten"]:
                    if k["konto"] not in budgeted:
                        k["werte"][c.key] = None
    _finish(cols, rows)
    ist = [c for c in cols if c.art == "ist"]
    hinweise = []
    if any(c.art == "budget" for c in cols):
        from . import budget
        if not budget.exists(book, ist[-1].start.year):
            hinweise.append(f"Kein Budget für {ist[-1].start.year} erfasst (aeradex budget vorjahr …)")
    return _report("erfolgsrechnung", "Erfolgsrechnung", window_label(ist[-1].start, ist[-1].end), cols, rows,
                   hinweise=hinweise, diagramm=_pl_chart(rows, cols))


def _pl_chart(rows: list[dict], cols: list[Column]) -> dict | None:
    ist = [c for c in cols if c.art == "ist" and c.key != "total"]
    if len(ist) < 2:
        return None
    by_label = {r["label"]: r for r in rows}
    ertrag = by_label.get("Betriebsertrag aus Lieferungen und Leistungen", {"werte": {}})["werte"]
    result = rows[-1]["werte"]
    return {"art": "balken", "labels": [c.label for c in ist],
            "reihen": [{"name": "Betriebsertrag", "werte": [ertrag.get(c.key, ZERO) for c in ist]},
                       {"name": "Ergebnis", "werte": [result.get(c.key, ZERO) for c in ist]}]}


# ---------- Bilanz ----------

def bilanz(book: Book, cols: list[Column], detail: bool = True) -> dict:
    """Balance sheet at the end of each column's window (budget columns are skipped)."""
    engine = BalanceEngine(book)
    result_acct = book.settings.konto("jahresergebnis")
    cols = [c for c in cols if c.art != "budget"] if any(c.art == "budget" for c in cols) else cols
    cols = [c for c in cols if c.art not in ("diff", "pct") or any(x.key == "ref" for x in cols)]
    for c in cols:
        if c.art not in ("diff", "pct"):
            c.label = f"{c.end:%d.%m.%Y}" if c.art == "ist" else f"{c.label} ({c.end:%d.%m.%Y})"
    members: dict[str, dict[str, list]] = {}
    results: dict[str, Decimal] = {}
    for c in cols:
        if c.art in ("diff", "pct"):
            continue
        per: dict[str, list] = defaultdict(list)
        for nr, acct in sorted(book.accounts.items()):
            if not acct.is_balance_sheet:
                continue
            v = engine.balance_at(nr, c.end)
            per[_effective_group(acct, v)].append((nr, acct.name, v))
        members[c.key] = per
        ytd = movements([r for r in engine.year_rows(c.end.year) if r.datum <= c.end])
        results[c.key] = sum((ytd.get(nr, ZERO) for nr, a in book.accounts.items() if a.is_pl), ZERO)
        members[c.key]["_ergebnis"] = [("", "Ergebnis", results[c.key])]
    rows: list[dict] = []

    def L(label, codes, style="line", sign=1, **k):
        k.setdefault("detail", detail)
        return _line(rows, cols, label, members, codes, style=style, sign=sign, **k)

    rows.append({"label": "Aktiven", "stil": "kopf", "werte": {}, "konten": []})
    for code in _UMLAUF:
        L(GROUP_LABEL[code], [code])
    L("Umlaufvermögen", _UMLAUF, "zwischentotal")
    for code in _ANLAGE:
        L(GROUP_LABEL[code], [code])
    L("Anlagevermögen", _ANLAGE, "zwischentotal")
    L("Total Aktiven", _UMLAUF + _ANLAGE, "total")
    rows.append({"label": "Passiven", "stil": "kopf", "werte": {}, "konten": []})
    for code in _FK_KURZ:
        L(GROUP_LABEL[code], [code], sign=-1)
    L("Kurzfristiges Fremdkapital", _FK_KURZ, "zwischentotal", sign=-1)
    for code in _FK_LANG:
        L(GROUP_LABEL[code], [code], sign=-1)
    L("Langfristiges Fremdkapital", _FK_LANG, "zwischentotal", sign=-1)
    L("Fremdkapital", _FK_KURZ + _FK_LANG, "zwischentotal", sign=-1)
    L("Eigenkapital ohne Ergebnis", ["eigenkapital"], sign=-1, exclude=(result_acct,))
    L("Ergebnis laufendes Jahr", ["_ergebnis"], sign=-1, keep_zero=True, detail=False)
    L("Eigenkapital", ["eigenkapital", "_ergebnis"], "zwischentotal", sign=-1, exclude=(result_acct,))
    total_p = L("Total Passiven", _FK_KURZ + _FK_LANG + ["eigenkapital", "_ergebnis"], "total", sign=-1,
                exclude=(result_acct,))
    _finish(cols, rows)
    aktiven = next(r for r in rows if r["label"] == "Total Aktiven")["werte"]
    hinweise = [f"Bilanz per {c.end:%d.%m.%Y} geht nicht auf: Differenz {aktiven[c.key] - total_p['werte'][c.key]}"
                for c in cols if c.art not in ("diff", "pct") and aktiven.get(c.key) != total_p["werte"].get(c.key)]
    last = [c for c in cols if c.art == "ist"][-1]
    return _report("bilanz", "Bilanz", f"per {last.end:%d.%m.%Y}", cols, rows, hinweise=hinweise)


# ---------- Geldflussrechnung (indirekt, OR 961b) ----------

_FIN_GROUPS = ["verb_verzinsl_kf", "verb_nahe_kf", "verb_verzinsl_lf", "verb_uebrige_lf", "verb_nahe_lf", "eigenkapital"]


def _cash_parts(book: Book, engine: BalanceEngine, start: date, end: date) -> dict[str, dict[str, Decimal]]:
    """Signed balance changes of one window inside one year, sorted into the statement's parts."""
    dividend = book.settings.konto("dividende")
    result_acct = book.settings.konto("jahresergebnis")
    moves = movements([r for r in engine.year_rows(start.year) if start <= r.datum <= end])
    parts: dict[str, dict[str, Decimal]] = {k: {} for k in ("ergebnis", "abschreibungen", "operativ", "invest",
                                                            "finanz", "liquid")}
    for nr, acct in book.accounts.items():
        v = moves.get(nr, ZERO)
        if acct.is_pl:
            if v:
                parts["ergebnis"][nr] = v
                if acct.gruppe == "abschreibungen":
                    parts["abschreibungen"][nr] = v
            continue
        delta = engine.balance_at(nr, end) - balance_before(engine, nr, start)
        if not delta:
            continue
        if acct.gruppe == "fluessige":
            parts["liquid"][nr] = delta
        elif acct.gruppe in _ANLAGE:
            parts["invest"][nr] = delta
        elif acct.gruppe in _FIN_GROUPS or nr in (dividend, result_acct):
            parts["finanz"][nr] = delta
        else:
            parts["operativ"][nr] = delta
    return parts


def geldfluss(book: Book, start: date, end: date, detail: bool = True) -> dict:
    """Cash flow, indirect method: result + depreciation ± working capital = operating; fixed assets =
    investing; interest-bearing debt and equity = financing. Checked against the change in cash."""
    engine = BalanceEngine(book)
    total: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    y = start.year
    while y <= end.year:
        a, b = max(start, date(y, 1, 1)), min(end, date(y, 12, 31))
        for part, accts in _cash_parts(book, engine, a, b).items():
            for nr, v in accts.items():
                total[part][nr] += v
        y += 1
    names = {nr: a.name for nr, a in book.accounts.items()}
    col = Column("c0", window_label(start, end), "ist", start, end)
    rows: list[dict] = []

    def row(label, value, style="line", accts=None, sign=-1):
        konten = [{"konto": nr, "name": names.get(nr, nr), "werte": {"c0": v * sign}}
                  for nr, v in sorted((accts or {}).items()) if v] if detail and style == "line" else []
        rows.append({"label": label, "stil": style, "werte": {"c0": value}, "konten": konten})

    ergebnis = -sum(total["ergebnis"].values(), ZERO)
    abschr = sum(total["abschreibungen"].values(), ZERO)
    op_delta = -sum(total["operativ"].values(), ZERO)
    operativ = ergebnis + abschr + op_delta
    invest = -sum(total["invest"].values(), ZERO) - abschr
    finanz = -sum(total["finanz"].values(), ZERO)
    liquid = sum(total["liquid"].values(), ZERO)
    rows.append({"label": "Geldfluss aus Geschäftstätigkeit", "stil": "kopf", "werte": {}, "konten": []})
    row("Ergebnis der Periode", ergebnis)
    row("Abschreibungen und Wertberichtigungen", abschr, accts=total["abschreibungen"], sign=1)
    row("Veränderung Forderungen, Vorräte, Abgrenzungen und kurzfristige Verbindlichkeiten", op_delta,
        accts=total["operativ"])
    row("Geldfluss aus Geschäftstätigkeit", operativ, "zwischentotal")
    rows.append({"label": "Geldfluss aus Investitionstätigkeit", "stil": "kopf", "werte": {}, "konten": []})
    row("Investitionen und Desinvestitionen Anlagevermögen", invest,
        accts={**total["invest"], **{k: v for k, v in total["abschreibungen"].items()}})
    row("Geldfluss aus Investitionstätigkeit", invest, "zwischentotal")
    rows.append({"label": "Geldfluss aus Finanzierungstätigkeit", "stil": "kopf", "werte": {}, "konten": []})
    row("Veränderung Finanzverbindlichkeiten und Eigenkapital (inkl. Ausschüttungen)", finanz, accts=total["finanz"])
    row("Geldfluss aus Finanzierungstätigkeit", finanz, "zwischentotal")
    row("Veränderung flüssige Mittel", operativ + invest + finanz, "total")
    row("Flüssige Mittel: Anfang", sum((balance_before(engine, nr, start) for nr, a in book.accounts.items()
                                        if a.gruppe == "fluessige"), ZERO))
    row("Flüssige Mittel: Ende", sum((engine.balance_at(nr, end) for nr, a in book.accounts.items()
                                      if a.gruppe == "fluessige"), ZERO))
    hinweise = []
    if operativ + invest + finanz != liquid:
        hinweise.append(f"Abstimmung: Geldfluss {operativ + invest + finanz} ≠ Veränderung flüssige Mittel {liquid}")
    return _report("geldfluss", "Geldflussrechnung", f"{window_label(start, end)} · indirekte Methode", [col], rows,
                   hinweise=hinweise, abstimmung=liquid - (operativ + invest + finanz))


# ---------- Kennzahlen ----------

def _groups_at(book: Book, engine: BalanceEngine, day: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for nr, acct in book.accounts.items():
        if acct.is_balance_sheet:
            v = engine.balance_at(nr, day)
            out[_effective_group(acct, v)] += v
    return out


def kennzahlen(book: Book, start: date, end: date) -> dict:
    """KMU ratios at `end` (balance sheet) and for the window (P&L). Thresholds are rules of thumb."""
    engine = BalanceEngine(book)
    g = _groups_at(book, engine, end)
    pl: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for nr, v in window_moves(engine, start, end).items():
        acct = book.accounts.get(nr)
        if acct and acct.is_pl:
            pl[acct.gruppe] -= v                                  # reading sign: Ertrag +, Aufwand −
    result_ytd = -sum((v for nr, v in movements([r for r in engine.year_rows(end.year) if r.datum <= end]).items()
                       if nr in book.accounts and book.accounts[nr].is_pl), ZERO)
    liquid = g["fluessige"]
    receivables = g["ford_ll"]
    umlauf = sum((g[c] for c in _UMLAUF), ZERO)
    anlage = sum((g[c] for c in _ANLAGE), ZERO)
    fk_kurz = -sum((g[c] for c in _FK_KURZ), ZERO)
    fk_lang = -sum((g[c] for c in _FK_LANG), ZERO)
    ek = -g["eigenkapital"] + result_ytd
    total = umlauf + anlage
    ertrag = pl["ertrag"]
    brutto = ertrag + pl["material"]
    ebit = brutto + pl["personal"] + pl["betrieb"] + pl["abschreibungen"]
    days = (end - start).days + 1
    purchases = -(pl["material"] + pl["betrieb"])

    def pct(a, b):
        return (a / b * 100).quantize(Decimal("0.1"), ROUND_HALF_UP) if b else None

    def tage(a, b):
        return (a / b * days).quantize(Decimal("1"), ROUND_HALF_UP) if b else None

    def ampel(value, good, warn, higher_is_better=True):
        if value is None:
            return ""
        if not higher_is_better:
            value, good, warn = -value, -good, -warn
        return "ok" if value >= good else ("warn" if value >= warn else "red")

    items = [
        ("Liquidität (flüssige Mittel)", liquid, "CHF", "Kasse, Post, Bank", ""),
        ("Liquiditätsgrad 1", pct(liquid, fk_kurz), "%", "flüssige Mittel ÷ kurzfristiges FK", (20, 10)),
        ("Liquiditätsgrad 2", pct(liquid + receivables, fk_kurz), "%",
         "(flüssige Mittel + Forderungen L+L) ÷ kurzfristiges FK", (100, 80)),
        ("Eigenkapitalquote", pct(ek, total), "%", "Eigenkapital (inkl. Ergebnis) ÷ Bilanzsumme", (30, 20)),
        ("Anlagedeckungsgrad 2", pct(ek + fk_lang, anlage), "%", "(EK + langfristiges FK) ÷ Anlagevermögen",
         (100, 90)),
        ("Betriebsertrag", ertrag, "CHF", "Nettoerlöse der Periode", ""),
        ("Bruttomarge", pct(brutto, ertrag), "%", "Bruttoergebnis ÷ Betriebsertrag", ""),
        ("EBIT-Marge", pct(ebit, ertrag), "%", "EBIT ÷ Betriebsertrag", (5, 0)),
        ("Personalaufwandquote", pct(-pl["personal"], ertrag), "%", "Personalaufwand ÷ Betriebsertrag", ""),
        ("Debitorenfrist (DSO)", tage(receivables, ertrag), "Tage",
         f"Forderungen L+L ÷ Betriebsertrag × {days} Tage", ("inv", 45, 60)),
        ("Kreditorenfrist (DPO)", tage(-g["verb_ll"], purchases), "Tage",
         f"Verbindlichkeiten L+L ÷ (Material- + übriger Aufwand) × {days} Tage", ""),
    ]
    cols = [Column("c0", f"per {end:%d.%m.%Y}", "ist", start, end), Column("urteil", "Beurteilung", "text")]
    words = {"ok": "gut", "warn": "beobachten", "red": "kritisch"}
    rows = []
    for label, value, unit, formel, limits in items:
        light = ""
        if limits and value is not None:
            light = (ampel(value, limits[1], limits[2], False) if limits[0] == "inv" else ampel(value, *limits))
        rows.append({"label": label, "stil": "line", "werte": {"c0": value, "urteil": words.get(light, "")},
                     "konten": [], "einheit": unit, "formel": formel, "ampel": light})
    return _report("kennzahlen", "Kennzahlen", f"Bilanz per {end:%d.%m.%Y} · Erfolg {window_label(start, end)}",
                   cols, rows, hinweise=["Schwellen sind Faustregeln für KMU, keine Branchenwerte."])


# ---------- Alter offene Posten ----------

def alter(book: Book, stichtag: date, seite: str = "debitoren") -> dict:
    """Aged receivables or payables per Stichtag, reconciled with the account."""
    from . import invoices as inv
    if seite == "debitoren":
        ar = inv.aged_receivables(book, stichtag)
        posten = [{"nummer": p["nummer"], "name": p.get("name") or p.get("kunde"), "datum": p["datum"],
                   "alter": p["alter_tage"], "offen": p["offen_chf"]} for p in ar["posten"]]
        konto, saldo, titel = ar["debitorenkonto"], ar["saldo_debitoren"], "Debitoren nach Alter"
    else:
        from .kreditoren import open_payables
        ap = open_payables(book, stichtag)
        posten = [{"nummer": p["nummer"], "name": p.get("name"), "datum": p["datum"],
                   "alter": p["alter_tage"], "offen": p["offen_chf"]} for p in ap["posten"]]
        konto, saldo, titel = ap["kreditorenkonto"], ap["saldo_kreditoren"], "Kreditoren nach Alter"
    buckets = [label for label, _, _ in inv.AGE_BUCKETS]
    cols = [Column(f"b{i}", label + " Tage", "ist") for i, label in enumerate(buckets)] + [Column("total", "Total")]
    per: dict[str, dict] = {}
    for p in posten:
        i = next(i for i, (_, lo, hi) in enumerate(inv.AGE_BUCKETS) if lo <= p["alter"] <= hi)
        r = per.setdefault(p["name"] or "—", {"label": p["name"] or "—", "stil": "line",
                                              "werte": {c.key: ZERO for c in cols}, "konten": []})
        r["werte"][f"b{i}"] += p["offen"]
        r["werte"]["total"] += p["offen"]
        r["konten"].append({"konto": p["nummer"], "name": f"{_d(p['datum']):%d.%m.%Y}",
                            "werte": {f"b{i}": p["offen"], "total": p["offen"]}})
    rows = sorted(per.values(), key=lambda r: -r["werte"]["total"])
    rows.append({"label": "Total", "stil": "total", "konten": [],
                 "werte": {c.key: sum((r["werte"][c.key] for r in rows), ZERO) for c in cols}})
    total = rows[-1]["werte"]["total"]
    hinweise = [] if total == saldo else [f"Saldo Konto {konto} {saldo} ≠ offene Posten {total}"]
    return _report(f"alter_{seite}", titel, f"per {stichtag:%d.%m.%Y}", cols, rows, hinweise=hinweise,
                   diagramm={"art": "balken", "labels": [c.label for c in cols[:-1]],
                             "reihen": [{"name": "Offen", "werte": [rows[-1]["werte"][c.key] for c in cols[:-1]]}]})


def _d(value) -> date:
    from .files import parse_date
    return parse_date(value)


# ---------- Umsatz ----------

_INVOICE_NR = re.compile(r"R-\d{4}-\d{4}")


def umsatz(book: Book, start: date, end: date, nach: str = "kunde") -> dict:
    """Revenue (Ertrag accounts, net of credit notes) by customer, account or month."""
    from . import invoices as inv
    if nach not in ("kunde", "konto", "monat"):
        raise BookError("Umsatz nach: kunde, konto oder monat")
    meta = inv.invoices(book)
    names = {k: inv.qr.invoice_name(c) for k, c in inv.customers(book).items()}
    engine = BalanceEngine(book)
    per: dict[str, Decimal] = defaultdict(lambda: ZERO)
    labels: dict[str, str] = {}
    for y in range(start.year, end.year + 1):
        for r in engine.year_rows(y):
            if not start <= r.datum <= end:
                continue
            for nr, sign in ((r.soll, -1), (r.haben, 1)):
                acct = book.accounts.get(nr) if nr else None
                if not acct or acct.klasse != "ertrag":
                    continue
                if nach == "konto":
                    key, labels[nr] = nr, f"{nr} {acct.name}"
                elif nach == "monat":
                    key = f"{r.datum.year}-{r.datum.month:02d}"
                    labels[key] = f"{MONTHS_DE[r.datum.month]} {r.datum.year}"
                else:
                    m = _INVOICE_NR.search(r.quelle or "")
                    kunde = meta.get(m.group(0), {}).get("kunde") if m else None
                    key = kunde or "_ohne"
                    labels[key] = names.get(kunde, kunde) if kunde else "ohne Rechnung (übrige Buchungen)"
                per[key] += sign * r.betrag
    col = Column("c0", window_label(start, end), "ist", start, end)
    keys = sorted(per) if nach == "monat" else sorted(per, key=lambda k: -per[k])
    rows = [{"label": labels[k], "stil": "line", "werte": {"c0": per[k]}, "konten": []} for k in keys if per[k]]
    total = sum(per.values(), ZERO)
    for r in rows:
        r["anteil"] = (r["werte"]["c0"] / total * 100).quantize(Decimal("0.1"), ROUND_HALF_UP) if total else None
    rows.append({"label": "Total", "stil": "total", "werte": {"c0": total}, "konten": []})
    chart = {"art": "balken", "labels": [r["label"] for r in rows[:-1]][:12],
             "reihen": [{"name": "Umsatz", "werte": [r["werte"]["c0"] for r in rows[:-1]][:12]}]}
    return _report(f"umsatz_{nach}", f"Umsatz nach {'Kunde' if nach == 'kunde' else nach.capitalize()}",
                   window_label(start, end), [col], rows, diagramm=chart)


# ---------- registry ----------

@dataclass
class Report:
    """A report the UI, CLI and agent can run. `build(book, params)` returns the report dict.
    `params` lists what it needs: periode (window), spalten, vergleich, stichtag, nach."""
    name: str
    label: str
    build: Callable[[Book, dict], dict]
    params: tuple[str, ...] = ("periode",)
    gruppe: str = "Finanzen"
    beschreibung: str = ""
    extra: dict = field(default_factory=dict)


def _year(p: dict) -> int:
    return int(p.get("jahr") or date.today().year)


def _window(p: dict) -> tuple[date, date]:
    return window(_year(p), p.get("periode") or "jahr", p.get("von"), p.get("bis"))


def _cols(p: dict) -> list[Column]:
    return columns(_year(p), p.get("periode") or "jahr", p.get("spalten") or "gesamt", p.get("vergleich") or "keine",
                   p.get("von"), p.get("bis"))


def _stichtag(p: dict) -> date:
    """The Stichtag: given, else the end of the window — but not later than today."""
    if p.get("stichtag"):
        return _d(p["stichtag"])
    start, end = _window(p)
    return max(start, min(end, date.today()))


CORE = [
    Report("erfolgsrechnung", "Erfolgsrechnung", lambda b, p: erfolgsrechnung(b, _cols(p), p.get("detail", True)),
           ("periode", "spalten", "vergleich"), beschreibung="OR-Gliederung, beliebige Periode, Monats-/Quartalsspalten"),
    Report("bilanz", "Bilanz", lambda b, p: bilanz(b, _cols(p), p.get("detail", True)),
           ("periode", "spalten", "vergleich"), beschreibung="per Ende jeder Spalte"),
    Report("geldfluss", "Geldflussrechnung", lambda b, p: geldfluss(b, *_window(p), p.get("detail", True)),
           ("periode",), beschreibung="indirekte Methode, abgestimmt mit den flüssigen Mitteln"),
    Report("kennzahlen", "Kennzahlen", lambda b, p: kennzahlen(b, _window(p)[0], _stichtag(p)), ("periode",),
           beschreibung="Liquidität, Eigenkapital, Margen, Fristen"),
    Report("debitoren", "Debitoren nach Alter", lambda b, p: alter(b, _stichtag(p), "debitoren"), ("stichtag",),
           gruppe="Offene Posten"),
    Report("kreditoren", "Kreditoren nach Alter", lambda b, p: alter(b, _stichtag(p), "kreditoren"), ("stichtag",),
           gruppe="Offene Posten"),
    Report("umsatz", "Umsatz", lambda b, p: umsatz(b, *_window(p), p.get("nach") or "kunde"), ("periode", "nach"),
           gruppe="Umsatz"),
]


def registry(book: Book | None) -> dict[str, Report]:
    from . import plugins
    out = {r.name: r for r in CORE}
    for r in plugins.reports(book):
        out.setdefault(r.name, r)
    return out


def run(book: Book, typ: str, **params) -> dict:
    rep = registry(book).get(typ)
    if rep is None:
        raise BookError(f"Bericht '{typ}' unbekannt: {', '.join(registry(book))}")
    out = rep.build(book, params)
    out.setdefault("typ", typ)
    out["parameter"] = {k: v for k, v in params.items() if v not in (None, "")}
    return out


"""Eröffnungsbilanz: the figures a book starts with, taken over from the previous bookkeeping.

The person enters (or imports) the closing figures of the year before the first business year, as the old
Schlussbilanz and Erfolgsrechnung show them:

- balance accounts: the balance per 31.12. → `vorjahr` (comparison column) and, folded, `eroeffnung`;
- P&L accounts: the year's figure → `vorjahr` only (comparison column of the first year).

Folding turns the closing balance into the opening balance the engine carries: accounts that close into equity
(`abschluss`, e.g. Privat) and the result account go into the Gewinnvortrag / Eigenkapital, and a result that
is not in the balance yet (balance accounts + P&L = 0) is added there too.

Figures in the UI are in reading convention (Passiven and Ertrag positive); the book stores signed amounts.
Imports (CSV, Excel, pasted text) are read column by column: account number, name, amount (or Soll/Haben);
the sign convention is detected from whether the balance adds up.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .book import Book, BookError, KLASSEN, _klasse_for
from .files import read_yaml, write_yaml

ZERO = Decimal("0")
CREDIT = ("passiv", "ertrag")


def reading_sign(klasse: str) -> int:
    return -1 if klasse in CREDIT else 1


def staged_path(book: Book) -> Path:
    return book.root / ".aeradex" / "lokal" / "eroeffnung.yaml"


# ---------- the current figures ----------

def view(book: Book) -> dict:
    """Every account with its closing figure of the prior year (reading convention), the opening balance and
    the totals of both."""
    s = book.settings
    lock = s.sperre_bis
    rows = []
    for a in sorted(book.accounts.values(), key=lambda a: a.nr):
        sign = reading_sign(a.klasse)
        schluss = a.vorjahr if a.vorjahr or not a.is_balance_sheet else a.eroeffnung
        rows.append({"konto": a.nr, "name": a.name, "klasse": a.klasse, "bilanz": a.is_balance_sheet,
                     "aktiv": a.aktiv_, "abschluss": a.abschluss, "wert": schluss * sign,
                     "eroeffnung": a.eroeffnung * sign})
    return {"jahr": s.erstes_jahr, "vorjahr": s.erstes_jahr - 1, "zeilen": rows,
            "gesperrt": bool(lock and lock.year >= s.erstes_jahr),
            "gewinnvortrag": s.konto("gewinnvortrag"), "jahresergebnis": s.konto("jahresergebnis"),
            **totals(book, {r["konto"]: r["eroeffnung"] * reading_sign(r["klasse"]) for r in rows},
                     {a.nr: a.vorjahr for a in book.accounts.values() if a.is_pl})}


def totals(book: Book, opening: dict[str, Decimal], pl: dict[str, Decimal]) -> dict:
    """Sums of a signed opening balance and signed P&L figures."""
    acc = book.accounts
    aktiven = sum((v for nr, v in opening.items() if nr in acc and acc[nr].klasse == "aktiv"), ZERO)
    passiven = -sum((v for nr, v in opening.items() if nr in acc and acc[nr].klasse == "passiv"), ZERO)
    ergebnis = -sum(pl.values(), ZERO)
    return {"aktiven": aktiven, "passiven": passiven, "differenz": aktiven - passiven, "ergebnis_vorjahr": ergebnis,
            "leer": not any(opening.values())}


# ---------- closing figures → opening balance ----------

def fold(book: Book, schluss: dict[str, Decimal], erfolg: dict[str, Decimal]) -> tuple[dict[str, Decimal], list[str]]:
    """Signed closing balances (and signed P&L figures) → signed opening balances, with notes on what moved."""
    s = book.settings
    gv, ja = s.konto("gewinnvortrag"), s.konto("jahresergebnis")
    notes = []
    opening = {nr: v for nr, v in schluss.items() if v}
    for nr in list(opening):
        acct = book.accounts.get(nr)
        target = acct.abschluss if acct else ""
        if target and target != nr:
            notes.append(f"{nr} {acct.name} ({_chf(-opening[nr])}) geht ins Konto {target}")
            opening[target] = opening.get(target, ZERO) + opening.pop(nr)
    if ja in opening and ja != gv:
        notes.append(f"Jahresergebnis auf {ja} ({_chf(-opening[ja])}) geht ins Konto {gv}")
        opening[gv] = opening.get(gv, ZERO) + opening.pop(ja)
    pl = sum(erfolg.values(), ZERO)
    rest = sum(opening.values(), ZERO)
    if rest and pl and rest + pl == 0:
        notes.append(f"Ergebnis {s.erstes_jahr - 1} von {_chf(-pl)} laut Erfolgsrechnung geht ins Konto {gv}")
        opening[gv] = opening.get(gv, ZERO) + pl
    return {nr: v for nr, v in opening.items() if v}, notes


def _chf(v: Decimal) -> str:
    return f"CHF {v:,.2f}".replace(",", "'")


# ---------- saving ----------

def apply(book: Book, werte: dict[str, Decimal], neue_konten: list[dict] | None = None) -> tuple[dict, list[Path]]:
    """Set the figures of the prior year (reading convention, {nr: amount}; accounts not named keep theirs)
    and derive the opening balance. `neue_konten`: [{"nr", "name"}] created first."""
    from . import statements
    from .book import Account
    lock = book.settings.sperre_bis
    if lock and lock.year >= book.settings.erstes_jahr:
        raise BookError("Die Eröffnungsbilanz ist gesperrt (das erste Geschäftsjahr liegt in der gesperrten Periode)")
    created = []
    for item in neue_konten or []:
        nr, name = str(item.get("nr") or "").strip(), str(item.get("name") or "").strip()
        if not nr or nr in book.accounts:
            continue
        if not re.fullmatch(r"\d{3,6}", nr):
            raise BookError(f"Kontonummer «{nr}» ungültig")
        klasse = item.get("klasse") or _klasse_for(nr)
        if klasse not in KLASSEN:
            raise BookError(f"Konto {nr}: Klasse «{klasse}» ungültig")
        book.accounts[nr] = Account(nr=nr, name=name or f"Konto {nr}", klasse=klasse,
                                    gruppe=statements.default_group(nr, klasse))
        created.append(nr)
    unknown = sorted(nr for nr in werte if nr not in book.accounts)
    if unknown:
        raise BookError(f"Konto {', '.join(unknown)} fehlt im Kontenplan — anlegen oder einem Konto zuordnen")
    signed = {nr: Decimal(str(v)) * reading_sign(book.accounts[nr].klasse) for nr, v in werte.items()}
    current = {a.nr: (a.vorjahr if a.vorjahr or not a.is_balance_sheet else a.eroeffnung)
               for a in book.accounts.values()}
    current.update(signed)
    schluss = {nr: v for nr, v in current.items() if book.accounts[nr].is_balance_sheet}
    erfolg = {nr: v for nr, v in current.items() if book.accounts[nr].is_pl and v}
    opening, notes = fold(book, schluss, erfolg)
    diff = sum(opening.values(), ZERO)
    if diff:
        t = totals(book, opening, erfolg)
        raise BookError(f"Die Eröffnungsbilanz geht nicht auf: Aktiven {_chf(t['aktiven'])}, Passiven "
                        f"{_chf(t['passiven'])}, Differenz {_chf(t['differenz'])}")
    ja = book.settings.konto("jahresergebnis")
    for a in book.accounts.values():
        if a.is_balance_sheet:
            a.vorjahr = schluss.get(a.nr, ZERO)
            if a.nr == ja and erfolg:                # the result is its own line, from the Erfolgsrechnung
                a.vorjahr = ZERO
            a.eroeffnung = opening.get(a.nr, ZERO)
        else:
            a.vorjahr = erfolg.get(a.nr, ZERO)
            a.eroeffnung = ZERO
    book.save_accounts()
    return {"angelegt": created, "hinweise": notes, **totals(book, opening, erfolg)}, [book.root / "kontenplan.yaml"]


# ---------- reading the old bookkeeping ----------

_KONTO_HEAD = ("konto", "kto", "kontonummer", "konto-nr", "kontonr", "nr", "nummer", "account", "sachkonto")
_NAME_HEAD = ("bezeichnung", "kontobezeichnung", "name", "text", "beschreibung", "kontoname", "description")
_AMOUNT_HEAD = ("saldo", "schlusssaldo", "endsaldo", "bestand", "betrag", "schluss", "aktuell", "total", "chf",
                "balance", "31.12")
_SKIP_HEAD = ("eröffnung", "eroeffnung", "anfang", "budget", "differenz", "abweichung", "%")


def _norm(cell) -> str:
    return re.sub(r"\s+", " ", str(cell or "")).strip().strip('"').lower()


def number(raw) -> Decimal | None:
    """'1'234.50', '-1.234,50', '(12.00)', '12.00-', '1 234,50 CHF' → Decimal; text → None."""
    t = re.sub(r"[\s'’  ]|chf|fr\.?", "", str(raw or ""), flags=re.I)
    if not t or not re.search(r"\d", t):
        return None
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1]
    if t.endswith("-"):
        negative, t = True, t[:-1]
    if t.startswith("-"):
        negative, t = not negative, t[1:]
    if not re.fullmatch(r"[\d.,]+", t):
        return None
    m = re.search(r"[.,](\d{1,2})$", t)
    if m:
        whole = re.sub(r"[.,]", "", t[:m.start()])
        t = f"{whole}.{m.group(1)}"
    else:
        t = re.sub(r"[.,]", "", t)
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    return -value if negative else value


def _konto(cell) -> tuple[str, str]:
    """'1020' / '1020 Bank' / '1020.0' → ('1020', 'Bank'); not an account → ('', '')."""
    text = str(cell or "").strip()
    m = re.fullmatch(r"(\d{3,6})(?:\.0)?(?:\s+[-–:]?\s*(.+))?", text)
    return (m.group(1), (m.group(2) or "").strip()) if m else ("", "")


def _table(filename: str, data: bytes) -> list[list[str]]:
    from .bankformat import EXCEL, _decode, _delimiter, rows_of
    if Path(filename).suffix.lower() in EXCEL:
        return rows_of(filename, data)
    text = _decode(data)
    if not text.strip():
        return []
    delim = _delimiter(text)
    if delim == ";" and ";" not in text and "\t" not in text:       # pasted with spaces: split before amounts
        return [re.split(r"\s{2,}|\t", ln.strip()) for ln in text.splitlines() if ln.strip()]
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def _header(rows: list[list[str]]) -> tuple[int, dict] | None:
    for i, row in enumerate(rows[:40]):
        cells = [_norm(c) for c in row]
        konto = next((j for j, c in enumerate(cells) if c in _KONTO_HEAD or c.startswith("konto")), None)
        if konto is None:
            continue
        cols = {"konto": konto}
        name = next((j for j, c in enumerate(cells) if j != konto and any(c.startswith(h) for h in _NAME_HEAD)), None)
        if name is not None:
            cols["name"] = name
        soll = next((j for j, c in enumerate(cells) if c in ("soll", "debit", "soll chf")), None)
        haben = next((j for j, c in enumerate(cells) if c in ("haben", "credit", "haben chf")), None)
        amounts = [j for j, c in enumerate(cells) if j not in (konto, name, soll, haben) and c
                   and any(h in c for h in _AMOUNT_HEAD) and not any(h in c for h in _SKIP_HEAD)]
        if amounts:
            cols["betrag"] = amounts[0]
        elif soll is not None and haben is not None:
            cols["soll"], cols["haben"] = soll, haben
        else:
            continue
        return i, cols
    return None


def read(filename: str, data: bytes) -> dict:
    """The lines of a Saldenliste / Bilanz / Erfolgsrechnung: [{konto, name, betrag}] (as the file shows the
    amount: signed if Soll/Haben columns, else as printed), plus what was skipped."""
    rows = _table(filename, data)
    if not rows:
        raise BookError("Die Datei ist leer")
    found = _header(rows)
    lines, skipped = [], 0
    for i, row in enumerate(rows[found[0] + 1:] if found else rows):
        if found:
            cols = found[1]
            nr, name = _konto(row[cols["konto"]] if cols["konto"] < len(row) else "")
            if "name" in cols and cols["name"] < len(row):
                name = str(row[cols["name"]]).strip() or name
            if "betrag" in cols:
                value = number(row[cols["betrag"]]) if cols["betrag"] < len(row) else None
            else:
                s = number(row[cols["soll"]]) if cols["soll"] < len(row) else None
                h = number(row[cols["haben"]]) if cols["haben"] < len(row) else None
                value = None if s is None and h is None else (s or ZERO) - (h or ZERO)
        else:
            nr, name, value = "", "", None
            for j, cell in enumerate(row):
                if not nr:
                    nr, name = _konto(cell)
                    if nr:
                        continue
                elif not name and number(cell) is None and str(cell).strip():
                    name = str(cell).strip()
            nums = [number(c) for c in row[1:]]
            nums = [n for n in nums if n is not None]
            value = nums[-1] if nr and nums else None
        if not nr or value is None:
            skipped += 1
            continue
        lines.append({"konto": nr, "name": name, "betrag": value})
    if not lines:
        raise BookError("Keine Konten mit Betrag gefunden — erwartet je Zeile Kontonummer, Bezeichnung und Saldo "
                        "(z.B. Saldenliste als CSV oder Excel)")
    # group numbers (1, 10, 100 …) sit beside the accounts in some exports: keep the most common length
    lengths = [len(x["konto"]) for x in lines]
    common = max(set(lengths), key=lengths.count)
    kept = [x for x in lines if len(x["konto"]) == common]
    merged: dict[str, dict] = {}
    for x in kept:
        if x["konto"] in merged:
            merged[x["konto"]]["betrag"] += x["betrag"]
        else:
            merged[x["konto"]] = dict(x)
    return {"zeilen": list(merged.values()), "uebersprungen": skipped + len(lines) - len(kept),
            "soll_haben": bool(found and "soll" in found[1])}


def interpret(book: Book, read_result: dict, datei: str = "") -> dict:
    """Read lines → a proposal in reading convention, with accounts to create and the sign convention found:
    'gelesen' (Passiven printed positive) or 'vorzeichen' (Soll +, Haben −), whichever balances."""
    lines = read_result["zeilen"]

    def klasse(nr):
        a = book.accounts.get(nr)
        return a.klasse if a else _klasse_for(nr)

    def signed(convention):
        return {x["konto"]: (x["betrag"] if convention == "vorzeichen" else x["betrag"] * reading_sign(klasse(x["konto"])))
                for x in lines}

    def balanced(values):
        bal = {nr: v for nr, v in values.items() if klasse(nr) in ("aktiv", "passiv")}
        pl = {nr: v for nr, v in values.items() if klasse(nr) in ("aufwand", "ertrag")}
        opening, _ = fold(book, bal, pl)
        return not sum(opening.values(), ZERO)

    if read_result.get("soll_haben"):
        convention = "vorzeichen"
    else:
        tries = [c for c in ("gelesen", "vorzeichen") if balanced(signed(c))]
        if tries:
            convention = tries[0]
        else:
            credit = [x["betrag"] for x in lines if klasse(x["konto"]) in CREDIT and x["betrag"]]
            negatives = sum(1 for v in credit if v < 0)
            convention = "vorzeichen" if negatives * 2 > len(credit) else "gelesen"   # most credit lines negative
    values = signed(convention)
    werte = {nr: v * reading_sign(klasse(nr)) for nr, v in values.items()}
    neu = [{"nr": x["konto"], "name": x["name"], "klasse": klasse(x["konto"])}
           for x in lines if x["konto"] not in book.accounts]
    hinweise = []
    if read_result.get("uebersprungen"):
        hinweise.append(f"{read_result['uebersprungen']} Zeile(n) ohne Konto oder Betrag übersprungen "
                        "(Titel, Gruppen, Totale)")
    hinweise.append("Passiven als positive Beträge erkannt" if convention == "gelesen"
                    else "Beträge mit Vorzeichen erkannt (Soll +, Haben −)")
    if neu:
        hinweise.append(f"{len(neu)} Konto/Konten fehlen im Kontenplan: werden angelegt, oder ein bestehendes Konto "
                        "eintragen")
    return {"datei": datei, "werte": werte, "neu": neu, "konvention": convention, "hinweise": hinweise}


def stage(book: Book, proposal: dict) -> Path:
    path = staged_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, {"datei": proposal["datei"], "konvention": proposal["konvention"],
                      "hinweise": proposal["hinweise"], "neu": proposal["neu"],
                      "werte": {nr: str(v) for nr, v in proposal["werte"].items()}})
    return path


def staged(book: Book) -> dict | None:
    path = staged_path(book)
    if not path.exists():
        return None
    data = read_yaml(path) or {}
    data["werte"] = {str(k): Decimal(str(v)) for k, v in (data.get("werte") or {}).items()}
    return data


def discard(book: Book) -> None:
    path = staged_path(book)
    if path.exists():
        path.unlink()

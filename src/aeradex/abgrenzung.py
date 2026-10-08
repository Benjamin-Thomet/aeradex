"""Rechnungsabgrenzung at year end, from the service period in the booking text (leistung.py).

For year Y every booking on an income or expense account whose text carries « · Leistung von–bis» is checked:

- booked in Y, service reaching into Y+1 → the share of Y+1 is deferred (transitorische Abgrenzung):
  expense → 1300 «Bezahlter Aufwand des Folgejahres», income → 2301 «Erhaltener Ertrag des Folgejahres»;
- booked in Y+1, service partly in Y → the share of Y is accrued (antizipative Abgrenzung):
  expense → 2300 «Noch nicht bezahlter Aufwand», income → 1301 «Noch nicht erhaltener Ertrag».

The share is pro rata by days; the amount is the net on the income/expense account (VAT stays where it was
invoiced). Accepted proposals are booked as one collective entry per 31.12. and reversed by one per 01.01. of the
next year. `abschluss/<Y>/abgrenzungen.yaml` records what was booked, so nothing is proposed or booked twice.
Nothing is booked without a person choosing it."""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .book import Book, BookError, Row
from .files import CENT, read_yaml, write_yaml
from .leistung import LABEL, from_text

ZERO = Decimal("0")
# role → (account, fallback when the chart lacks it: the Verein chart has only 1300/2300)
ROLES = {"aufwand_voraus": ("1300", "1300"), "ertrag_ausstehend": ("1301", "1300"),
         "aufwand_ausstehend": ("2300", "2300"), "ertrag_voraus": ("2301", "2300")}
ART_LABEL = {"aufwand_voraus": "Aufwand des Folgejahres (vorausbezahlt)",
             "ertrag_voraus": "Ertrag des Folgejahres (vorauserhalten)",
             "aufwand_ausstehend": "Aufwand des Jahres, noch nicht verbucht",
             "ertrag_ausstehend": "Ertrag des Jahres, noch nicht verbucht"}


def path(book: Book, year: int) -> Path:
    return book.root / "abschluss" / str(year) / "abgrenzungen.yaml"


def registry(book: Book, year: int) -> dict:
    p = path(book, year)
    return read_yaml(p) if p.exists() else {"buchungen": []}


def konto(book: Book, role: str) -> str:
    """The account for a role: aeradex.yaml konten.abgrenzung_<role>, else the Swiss default, else its fallback."""
    override = (book.settings.get("konten") or {}).get(f"abgrenzung_{role}")
    for nr in ([str(override)] if override else []) + list(ROLES[role]):
        if nr in book.accounts:
            return nr
    raise BookError(f"Für die Abgrenzung fehlt Konto {ROLES[role][0]} im Kontenplan "
                    f"(oder konten.abgrenzung_{role} in aeradex.yaml)")


def _days(a: date, b: date) -> int:
    return (b - a).days + 1 if b >= a else 0


def _money(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def _plain(text: str) -> str:
    """The booking text without its service label — the Abgrenzung rows must not be found again."""
    return text.split(f" · {LABEL} ")[0].strip() or text


def proposals(book: Book, year: int) -> list[dict]:
    """Every Abgrenzung the books of `year` call for, booked or not."""
    end, start_next = date(year, 12, 31), date(year + 1, 1, 1)
    reg = registry(book, year)
    done = {i for b in reg.get("buchungen") or [] for i in b.get("ids") or []}
    own = {b.get(k) for b in reg.get("buchungen") or [] for k in ("beleg", "aufloesung")}
    out = []
    for r in book.rows:
        if r.datum.year not in (year, year + 1) or r.beleg in own:
            continue
        period = from_text(r.text)
        if not period:
            continue
        von, bis = period
        total = _days(von, bis)
        for side, nr in (("soll", r.soll), ("haben", r.haben)):
            acct = book.accounts.get(nr)
            if not nr or acct is None or acct.klasse not in ("aufwand", "ertrag"):
                continue
            # signed effect on the result side of the account: expense +soll, income +haben
            signed = r.betrag if (acct.klasse == "aufwand") == (side == "soll") else -r.betrag
            if r.datum.year == year:
                share = _days(max(von, start_next), bis)
                art = "aufwand_voraus" if acct.klasse == "aufwand" else "ertrag_voraus"
            else:
                share = _days(von, min(bis, end))
                art = "aufwand_ausstehend" if acct.klasse == "aufwand" else "ertrag_ausstehend"
            if not share:
                continue
            amount = _money(signed * share / total)
            if not amount:
                continue
            gegen = konto(book, art)
            # the entry moves `amount` off (voraus) or onto (ausstehend) the result of year Y
            if art == "aufwand_voraus":
                soll, haben = gegen, nr
            elif art == "ertrag_voraus":
                soll, haben = nr, gegen
            elif art == "aufwand_ausstehend":
                soll, haben = nr, gegen
            else:
                soll, haben = gegen, nr
            if amount < 0:
                soll, haben, amount = haben, soll, -amount
            key = f"{r.beleg}|{r.datum}|{side}|{nr}|{r.betrag}|{von}|{bis}"
            out.append({"id": hashlib.sha1(key.encode()).hexdigest()[:10], "art": art, "art_label": ART_LABEL[art],
                        "beleg": r.beleg, "datum": r.datum, "text": _plain(r.text), "konto": nr,
                        "konto_name": acct.name, "basis": signed, "von": von, "bis": bis, "tage": total,
                        "tage_anteil": share, "betrag": amount, "soll": soll, "haben": haben,
                        "gebucht": None})
    for p in out:
        p["gebucht"] = next((b.get("beleg") for b in reg.get("buchungen") or [] if p["id"] in (b.get("ids") or [])),
                            None) if p["id"] in done else None
    return sorted(out, key=lambda p: (p["datum"], p["beleg"], p["konto"]))


def book_accruals(book: Book, year: int, ids: list[str]) -> tuple[dict, list[Path]]:
    """Book the chosen proposals: one collective entry on 31.12., its reversal on 01.01. of the next year."""
    from .journal import ensure_open, next_beleg, post
    chosen = [p for p in proposals(book, year) if p["id"] in set(ids)]
    if not chosen:
        raise BookError("Keine Abgrenzung ausgewählt")
    twice = [p["beleg"] for p in chosen if p["gebucht"]]
    if twice:
        raise BookError(f"Bereits abgegrenzt: {', '.join(twice)}")
    end, first = date(year, 12, 31), date(year + 1, 1, 1)
    ensure_open(book, end)
    ensure_open(book, first)
    beleg = next_beleg(book, year)

    def text(p, prefix):
        return (f"{prefix} {p['beleg']}: {p['text']} ({p['tage_anteil']}/{p['tage']} Tage "
                f"{p['von']:%d.%m.%Y}–{p['bis']:%d.%m.%Y})")

    rows = [Row(end, beleg, text(p, "Abgrenzung"), p["soll"], p["haben"], p["betrag"]) for p in chosen]
    touched = post(book, rows)
    reversal = next_beleg(book, year + 1)
    back = [Row(first, reversal, text(p, f"Auflösung Abgrenzung 31.12.{year} zu"), p["haben"], p["soll"], p["betrag"])
            for p in chosen]
    touched += post(book, back)
    reg = registry(book, year)
    entry = {"datum": end.isoformat(), "beleg": beleg, "aufloesung": reversal,
             "ids": [p["id"] for p in chosen], "total": str(sum((p["betrag"] for p in chosen), ZERO))}
    reg.setdefault("buchungen", []).append(entry)
    p = path(book, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(p, reg)
    return entry, touched + [p]


def open_count(book: Book, year: int) -> int:
    """Proposals of `year` not booked yet (for the change-of-year checklist)."""
    try:
        return sum(1 for p in proposals(book, year) if not p["gebucht"])
    except BookError:
        return 0

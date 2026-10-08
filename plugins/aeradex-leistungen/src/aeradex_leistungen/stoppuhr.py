"""The timer: one running timer per person, started with one click, stopped into a normal time entry.

The running state is not part of the book (no commit for every start): it lives in `.aeradex/lokal/stoppuhr.yaml`,
which the core treats as local runtime state (storage.LOCAL, not in git). Stopping writes the entry through the
usual checked, committed path. Starting another timer for the same person stops the running one first.
Rounding: `rundung` minutes in leistungen/einstellungen.yaml, rounded up (0 = to the minute)."""
from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from aeradex.book import Book, BookError
from aeradex.files import read_yaml, write_yaml
from aeradex.storage import LOCAL

from . import daten


def path(book: Book) -> Path:
    return book.root / LOCAL / "stoppuhr.yaml"


def running(book: Book) -> dict[str, dict]:
    p = path(book)
    data = (read_yaml(p) or {}) if p.exists() else {}
    return {str(k): dict(v) for k, v in data.items() if v}


def _save(book: Book, data: dict) -> None:
    p = path(book)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(p, data)


def elapsed_minutes(timer: dict, now: datetime | None = None) -> int:
    start = datetime.fromisoformat(str(timer["start"]))
    return max(0, int(((now or datetime.now()) - start).total_seconds() // 60))


def rounded_hours(minutes: int, step: int) -> Decimal:
    if step and minutes:
        minutes = math.ceil(minutes / step) * step
    return (Decimal(minutes) / 60).quantize(Decimal("0.01"))


def start(book: Book, wer: str, kunde: str = "", projekt: str = "", leistung: str = "", text: str = "",
          abrechenbar: bool = True, kategorie: str = "", now: datetime | None = None) -> dict:
    """Start the person's timer; a running one is stopped and recorded first (its result under "gestoppt").
    Called outside a write transaction — the timer state is local, only the recorded entry is committed."""
    person = daten.person(book, wer)
    kunde, projekt = daten._resolve_customer(book, kunde, projekt)
    if abrechenbar and not kunde:
        raise BookError("Für die Stoppuhr einen Kunden oder ein Projekt wählen (oder nicht abrechenbar)")
    if leistung:
        daten.product(book, leistung)
    stopped = None
    if person["nummer"] in running(book):
        stopped = stop(book, person["nummer"], now=now)
    timer = {"start": (now or datetime.now()).replace(microsecond=0).isoformat(), "kunde": kunde,
             "projekt": projekt, "leistung": str(leistung or "").upper(), "text": str(text or "").strip(),
             "abrechenbar": bool(abrechenbar), "kategorie": "" if abrechenbar else (kategorie or "Intern")}
    data = running(book)
    data[person["nummer"]] = timer
    _save(book, data)
    return {"wer": person["nummer"], **timer, "gestoppt": stopped}


def stop(book: Book, wer: str, text: str | None = None, now: datetime | None = None) -> dict:
    """Stop the person's timer and record the time (rounded up to `rundung`) as a checked, committed entry.
    Under one minute nothing is recorded. If recording fails, the timer keeps running."""
    from aeradex import api
    nr = daten.person(book, wer)["nummer"]
    data = running(book)
    timer = data.pop(nr, None)
    if timer is None:
        raise BookError("Keine laufende Stoppuhr")
    minutes = elapsed_minutes(timer, now)
    hours = min(rounded_hours(minutes, daten.settings(book)["rundung"]), Decimal(24))
    _save(book, data)
    if minutes < 1:
        return {"wer": nr, "stunden": Decimal(0), "verworfen": True, "meldung": "Stoppuhr unter einer Minute — verworfen"}
    day = datetime.fromisoformat(str(timer["start"])).date()
    try:
        res = api.write(book, f"Stoppuhr: {hours} h erfasst", daten.add_time, day, nr, hours,
                        text if text is not None else timer["text"], timer["kunde"], timer["projekt"],
                        timer["abrechenbar"], timer["kategorie"], None, timer["leistung"])
    except Exception:
        data[nr] = timer                                  # nothing lost: the timer runs on
        _save(book, data)
        raise
    return {"wer": nr, "stunden": hours, "eintrag": res["ergebnis"]["id"], "meldung": res["meldung"]}


def discard(book: Book, wer: str) -> dict:
    nr = daten.person(book, wer)["nummer"]
    data = running(book)
    if data.pop(nr, None) is None:
        raise BookError("Keine laufende Stoppuhr")
    _save(book, data)
    return {"wer": nr}


def today_minutes(timer: dict) -> int:
    return elapsed_minutes(timer) if datetime.fromisoformat(str(timer["start"])).date() == date.today() else 0

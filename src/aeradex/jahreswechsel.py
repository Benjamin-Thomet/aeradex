"""Opening a new business year, and the checklist for the change of year.

Balances need no opening entry: the balance engine carries every account's closing balance into the
next year and folds the year's result into the Gewinnvortrag. Opening a year only makes it exist
before its first booking (`aeradex.yaml → geschaeftsjahr_offen_bis`), so it shows up in every year
selection. The checklist names what is still open from the year before.

Bookings are accepted only in an open business year (`bookable`): from the first year up to the last one
opened, and always the current calendar year. A date in 2017 or two years ahead is a typo, not a booking that
should vanish into a year nobody looks at."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from .book import Book, BookError

KEY = "geschaeftsjahr_offen_bis"


def _chf(value) -> str:
    return f"{Decimal(str(value or 0)):,.2f}".replace(",", "'")


def next_year(book: Book) -> int:
    return max(book.years()) + 1


def last_open(book: Book, today: date | None = None) -> int:
    """The last year that takes bookings: the last opened year, or the current calendar year once it began."""
    today = today or date.today()
    years = book.years()
    return max(years[-1], today.year) if today.year >= years[0] else years[-1]


def year_problem(book: Book, d: date, today: date | None = None) -> str | None:
    """Why a booking dated `d` is refused because of its business year, or None."""
    first, last = book.settings.erstes_jahr, last_open(book, today)
    if d.year < first:
        return (f"{d:%d.%m.%Y} liegt vor dem ersten Geschäftsjahr {first} — Datum prüfen. Salden von früher "
                f"gehören in die Eröffnungsbilanz {first}.")
    if d.year > last:
        span = f"{first}–{last}" if first != last else str(first)
        hint = (f"zuerst das Geschäftsjahr {d.year} eröffnen (Einstellungen › Geschäftsjahre)"
                if d.year == last + 1 else "Datum prüfen")
        return f"{d:%d.%m.%Y} liegt in keinem eröffneten Geschäftsjahr (offen: {span}) — {hint}."
    return None


def set_first_year(book: Book, jahr: int, today: date | None = None) -> list[Path]:
    """Move the first business year, while that changes no booking: nothing before it, nothing locked."""
    today = today or date.today()
    jahr = int(jahr)
    if not 1990 <= jahr <= today.year + 1:
        raise BookError(f"Erstes Geschäftsjahr {jahr}: erlaubt sind 1990 bis {today.year + 1}")
    old = book.settings.erstes_jahr
    if jahr == old:
        raise BookError(f"Das erste Geschäftsjahr ist bereits {jahr}")
    before = sorted({r.datum.year for r in book.rows if r.datum.year < jahr})
    if before:
        raise BookError(f"Es gibt Buchungen in {', '.join(map(str, before))} — das erste Geschäftsjahr kann "
                        f"nicht nach {before[0]} liegen")
    lock = book.settings.sperre_bis
    if lock and lock.year >= min(jahr, old):
        raise BookError(f"Die Bücher sind bis {lock:%d.%m.%Y} gesperrt — das erste Geschäftsjahr ist fest")
    book.settings.data["erstes_jahr"] = jahr
    if int(book.settings.get(KEY) or 0) <= jahr:
        book.settings.data.pop(KEY, None)
    book.save_settings()
    return [book.root / "aeradex.yaml"]


def overview(book: Book, today: date | None = None) -> dict:
    """The business years for the settings page: status, bookings, what can be opened or moved."""
    today = today or date.today()
    lock = book.settings.sperre_bis
    first, last = book.settings.erstes_jahr, last_open(book, today)
    belege: dict[int, set] = {}
    for r in book.rows:
        belege.setdefault(r.datum.year, set()).add(r.beleg)
    years = []
    for y in range(first, last + 1):
        if lock and lock >= date(y, 12, 31):
            status, text = "gesperrt", "abgeschlossen und gesperrt"
        elif lock and lock.year == y:
            status, text = "teilweise", f"gesperrt bis {lock:%d.%m.%Y}"
        else:
            status, text = "offen", "offen für Buchungen"
        years.append({"jahr": y, "status": status, "text": text, "buchungen": len(belege.get(y, ())),
                      "aktuell": y == today.year})
    nxt = last + 1
    rows = bool(book.rows)
    return {"erstes": first, "letztes": last, "jahre": list(reversed(years)), "naechstes": nxt,
            "eroeffnen": nxt <= today.year + 1, "sperre_bis": lock,
            "erstes_aenderbar": not (lock and lock.year >= first),
            "erstes_bis": min((r.datum.year for r in book.rows), default=today.year + 1),
            "hat_buchungen": rows}


def open_year(book: Book, jahr: int | None = None, today: date | None = None) -> tuple[int, list[Path]]:
    target = next_year(book)
    jahr = int(jahr or target)
    if jahr in book.years():
        raise BookError(f"Das Geschäftsjahr {jahr} ist bereits eröffnet")
    if jahr != target:
        raise BookError(f"Als Nächstes lässt sich {target} eröffnen — Jahre werden der Reihe nach eröffnet")
    today = today or date.today()
    if jahr > today.year + 1:
        raise BookError(f"{jahr} liegt mehr als ein Jahr in der Zukunft")
    book.settings.data[KEY] = jahr
    book.save_settings()
    return jahr, [book.root / "aeradex.yaml"]


def checklist(book: Book, jahr: int) -> list[dict]:
    """For year `jahr`: what the change from `jahr - 1` still needs. Items: titel, status (ok/offen/hinweis),
    text, link."""
    from . import mwst, payroll, statements
    prev = jahr - 1
    if prev < book.settings.erstes_jahr:
        return []
    items = []

    def add(status, titel, text, link=""):
        items.append({"status": status, "titel": titel, "text": text, "link": link})

    st = statements.year_end_statement(book, prev)
    total = next((r["aktuell"] for r in st["aktiven"] if r["stil"] == "total"), 0)
    add("ok", f"Eröffnungsbilanz per 01.01.{jahr}",
        f"Die Salden der Schlussbilanz {prev} werden automatisch vorgetragen (Total Aktiven CHF {_chf(total)}); "
        f"das Jahresergebnis {prev} von CHF {_chf(st['jahresergebnis'])} steht im Gewinn- bzw. Verlustvortrag.",
        f"/abschluss?jahr={prev}")

    lock = book.settings.sperre_bis
    if lock and lock >= date(prev, 12, 31):
        add("ok", f"Geschäftsjahr {prev} gesperrt", f"Gesperrt bis {lock:%d.%m.%Y}.", f"/abschluss?jahr={prev}")
    else:
        add("offen", f"Geschäftsjahr {prev} abschliessen und sperren",
            "Nach den letzten Abschlussbuchungen (Abgrenzungen, Abschreibungen, Bewertung) die Periode "
            f"bis 31.12.{prev} sperren — danach ist sie unveränderlich.", f"/abschluss?jahr={prev}")

    from . import abgrenzung
    offen = abgrenzung.open_count(book, prev)
    if offen:
        add("offen", f"Rechnungsabgrenzungen {prev}", f"{offen} Buchung(en) mit Leistungszeitraum über den Jahreswechsel "
            "noch nicht abgegrenzt.", f"/abschluss/buchungen?jahr={prev}#abgrenzungen")
    if st.get("gewinnverwendung") is not None:
        g = st["gewinnverwendung"]
        if g["gebucht"]:
            add("ok", f"Gewinnverwendung {prev} gebucht", "Nach dem Beschluss der Generalversammlung gebucht.",
                f"/abschluss?jahr={prev}")
        else:
            add("offen", f"Gewinnverwendung {prev}",
                "Nach dem Beschluss der Generalversammlung (innert sechs Monaten) Dividende und Reserven buchen.",
                f"/abschluss?jahr={prev}")

    if mwst.config(book)["methode"] != "keine":
        missing = [p for p in mwst.periods(book, prev) if mwst.load(book, p) is None]
        if missing:
            add("offen", f"MWST-Abrechnungen {prev}", f"Noch nicht verbucht: {', '.join(missing)}.", f"/mwst?jahr={prev}")
        else:
            add("ok", f"MWST-Abrechnungen {prev}", "Alle Perioden verbucht.", f"/mwst?jahr={prev}")
        add("hinweis", f"MWST-Jahresabstimmung {prev}",
            "Umsatz und Steuer des Jahres mit den Abrechnungen abstimmen; Korrekturen innert 240 Tagen nach "
            "Geschäftsjahresende einreichen.", f"/mwst/abstimmung?jahr={prev}")

    emps = [e for e in payroll.employees(book).values() if e.get("aktiv", True)]
    if emps:
        add("hinweis", f"Lohnausweise {prev}", "Für alle Mitarbeitenden erstellen, sobald die Monate abgeschlossen sind.",
            f"/lohn/mitarbeiter?jahr={prev}")
        add("hinweis", f"Lohnsätze {jahr} prüfen",
            "AHV/IV/EO, ALV, UVG, KTG und BVG gelten für alle Jahre gleich — bei Änderungen der Ausgleichskasse oder "
            "der Policen vor dem ersten Lohnlauf anpassen.", "/einstellungen#lohn")
        if any(payroll.qst_pflichtig(e)[0] for e in emps):
            add("hinweis", f"Quellensteuer {jahr}", "Die ESTV-Tarife des neuen Jahres lädt der erste Lohnlauf automatisch; "
                "Tarifcodes (Zivilstand, Kinder) der Mitarbeitenden prüfen.", "/lohn/mitarbeiter")
    return items

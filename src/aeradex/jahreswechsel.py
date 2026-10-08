"""Opening a new business year, and the checklist for the change of year.

Balances need no opening entry: the balance engine carries every account's closing balance into the
next year and folds the year's result into the Gewinnvortrag. Opening a year only makes it exist
before its first booking (`aeradex.yaml → geschaeftsjahr_offen_bis`), so it shows up in every year
selection. The checklist names what is still open from the year before."""
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
        if any(e.get("qst") for e in emps):
            add("hinweis", f"Quellensteuertarife {jahr}", "Die Tarife des neuen Jahres vor dem ersten Lohnlauf laden.",
                "/lohn")
    return items

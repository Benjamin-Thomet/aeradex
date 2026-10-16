"""What the office sees on the Team-App page: the team at a glance and the entries from the app still to check."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from aeradex import invoices, payroll
from aeradex.book import Book
from aeradex_leistungen import abrechnung, abwesenheit
from aeradex_leistungen import daten as leistungen

from . import abbild, daten, verbindung

LONG_DAY = Decimal(10)


def pensum_now(book: Book, p: dict, today: date) -> dict:
    """The Pensum in force today (employees, %) or the weekly hours (people without payroll), and its history."""
    history = [{"ab": d, "wert": v} for d, v in sorted((leistungen_date(k), Decimal(str(v)))
                                                      for k, v in (p.get("pensum_ab") or {}).items())]
    if p["lohn"]:
        current = next((h["wert"] for h in reversed(history) if h["ab"] <= today), None)
        if current is None:
            current = Decimal(str(payroll.employee(book, p["nummer"]).get("pensum") or 100))
        return {"wert": current, "einheit": "%", "verlauf": history}
    week = leistungen.week_hours(p, today)
    return {"wert": week, "einheit": "h/Woche", "verlauf": history}


def leistungen_date(raw) -> date:
    return raw if isinstance(raw, date) else date.fromisoformat(str(raw))


def team(book: Book, today: date | None = None) -> list[dict]:
    today = today or date.today()
    people = leistungen.people(book)
    accounts = daten.accounts(book)
    rows_abw = abwesenheit.items(book)
    hols = abbild.holidays_around(book, today)
    ledger = daten.imported(book)
    active_keys = set(verbindung.state(book).get("aktiviert") or [])
    open_requests = defaultdict(int)
    for r in daten.requests(book):
        if r["status"] == "offen":
            open_requests[r["wer"]] += 1
    last_app: dict[str, str] = {}
    for item in ledger.values():
        if item.get("buero") and not item.get("geaendert"):
            continue                       # recorded in the office, not used in the app (yet)
        when = item.get("geaendert") or item.get("am") or ""
        if when > last_app.get(item.get("person"), ""):
            last_app[item["person"]] = when
    to_check = defaultdict(int)
    for item in check_list(book, today):
        to_check[item["wer"]] += 1
    out = []
    for nr, p in sorted(people.items()):
        if not p["aktiv"] and nr not in accounts:
            continue
        bal = abbild.balances(book, p, today, rows_abw, hols)["monat"]
        holiday = abwesenheit.holiday_account(book, nr, today.year, today)
        acc = accounts.get(nr)
        app = ("kein Konto" if not acc else "gesperrt" if not acc["aktiv"]
               else "aktiv" if nr in active_keys else "nicht aktiviert")
        out.append({"nummer": nr, "name": p["name"], "lohn": p["lohn"], "stundenlohn": p["stundenlohn"],
                    "pensum": pensum_now(book, p, today), "woche": leistungen.week_hours(p, today),
                    "ist": bal.get("ist"), "soll": bal.get("soll"), "saldo": bal.get("saldo"),
                    "saldo_jahr": bal.get("saldo_jahr"), "ferien_rest": holiday["rest"],
                    "antraege": open_requests[nr], "zu_pruefen": to_check[nr], "letzte_app": last_app.get(nr, ""),
                    "app": app, "konto": acc})
    return out


def check_list(book: Book, today: date | None = None) -> list[dict]:
    """Entries from the app the office has not checked yet (or that changed after it checked them), each with
    the things worth a second look."""
    today = today or date.today()
    uuid_of = daten.by_id(book)
    ledger = daten.imported(book)
    done = daten.checked(book)
    people = leistungen.people(book)
    projs = leistungen.projects(book)
    prods = leistungen.products(book)
    custs = invoices.customers(book)
    entries = leistungen.entries(book)
    absences = abwesenheit.items(book)
    per_day: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for e in entries:
        if e["art"] == "Zeit":
            per_day[(e["wer"], e["datum"])] += e["menge"]
    # the invoice lists the days with their description: a day nobody described would be missing there
    undescribed = ({i for d in abrechnung.undescribed_days(book, [e for e in entries if not e.get("rechnung")])
                    for i in d["ids"]} if leistungen.settings(book)["rechnung_tage"] else set())
    absent_days = {(a["wer"], a["von"].toordinal() + i) for a in absences
                   for i in range((a["bis"] - a["von"]).days + 1)}
    hols: dict[int, set] = {}

    def holiday(d: date) -> bool:
        if d.year not in hols:
            hols[d.year] = abwesenheit.holidays(book, d.year)
        return d in hols[d.year]

    out = []
    for e in entries:
        uuid = uuid_of.get(e["id"])
        if not uuid or done.get(e["id"], {}).get("fp") == daten.fingerprint(e):
            continue
        if ledger[uuid].get("buero") and int(ledger[uuid].get("version") or 0) == 0:
            continue                       # the office's own entry, untouched by the employee
        flags = []
        if e["art"] == "Zeit" and per_day[(e["wer"], e["datum"])] > LONG_DAY:
            flags.append(f"{per_day[(e['wer'], e['datum'])]:.2f} h an diesem Tag".replace(".", ","))
        if e["datum"].weekday() >= 5:
            flags.append("Wochenende")
        elif holiday(e["datum"]):
            flags.append("Feiertag")
        if e["datum"] > today:
            flags.append("in der Zukunft")
        if e["art"] == "Zeit" and (e["wer"], e["datum"].toordinal()) in absent_days:
            flags.append("an einem Abwesenheitstag")
        if e["abrechenbar"] and not e.get("preis"):
            flags.append("ohne Stundensatz" if e["art"] == "Zeit" else "ohne Preis")
        if e["projekt"] and projs.get(e["projekt"], {}).get("status") == "abgeschlossen":
            flags.append(f"{leistungen.settings(book)['projekt_wort']} abgeschlossen")
        if e["id"] in undescribed:
            flags.append("Tag ohne Beschreibung")
        original = ledger[uuid].get("original_text") or ""
        proj = projs.get(e["projekt"], {}) if e["projekt"] else {}
        prod = prods.get(e["produkt"], {}) if e["produkt"] else {}
        customer = (custs.get(e["kunde"], {}).get("firma") or custs.get(e["kunde"], {}).get("name") or e["kunde"]) if e["kunde"] else ""
        where = " · ".join(filter(None, [customer, str(proj.get("name") or "")]))
        was = ((where or f"Intern: {e['kategorie']}") if e["art"] == "Zeit"
               else " · ".join(filter(None, [prod.get("text") or e["produkt"], where])))
        out.append({"id": e["id"], "datum": e["datum"], "wer": e["wer"], "name": people.get(e["wer"], {}).get("name", e["wer"]),
                    "was": was, "text": e["text"] if e["text"] != prod.get("text") else "",
                    "menge": e["menge"], "einheit": "h" if e["art"] == "Zeit" else prod.get("einheit") or "",
                    "flags": flags, "art": e["art"], "original": original if original != e["text"] else "",
                    "geaendert": bool(ledger[uuid].get("geaendert") or ledger[uuid].get("buero")) or e["id"] in done})
    for a in absences:
        uuid = uuid_of.get(a["id"])
        if not uuid or done.get(a["id"], {}).get("fp") == daten.fingerprint(a):
            continue
        out.append({"id": a["id"], "datum": a["von"], "bis": a["bis"], "wer": a["wer"],
                    "name": people.get(a["wer"], {}).get("name", a["wer"]), "was": a["art"], "text": a["notiz"],
                    "menge": None, "einheit": "", "flags": [], "art": "Abwesenheit",
                    "geaendert": bool(ledger[uuid].get("geaendert")) or a["id"] in done})
    return sorted(out, key=lambda x: (x["datum"], x["name"], x["id"]))


def mark(book: Book, ids: list[str], von: str = "") -> tuple[dict, list]:
    """Mark these entries (L-…, A-…) as checked in their current state."""
    wanted = set(ids)
    items = [e for e in leistungen.entries(book) if e["id"] in wanted]
    items += [a for a in abwesenheit.items(book) if a["id"] in wanted]
    return daten.mark_checked(book, items, von)

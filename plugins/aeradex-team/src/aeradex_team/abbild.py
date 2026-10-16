"""The snapshot the portal shows: master data to pick from, and per person their own balances and entries.

Only what an employee may see: project names, Leistungsarten, materials with units, own hours, own absences.
No prices, rates, salaries or customer addresses.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from aeradex import api, invoices
from aeradex.book import Book, BookError
from aeradex_leistungen import abwesenheit, kontrolle
from aeradex_leistungen import daten as leistungen

from . import daten

PAST_DAYS = 70
FUTURE_DAYS = 60


def _s(value) -> str | None:
    return None if value is None else f"{Decimal(str(value)):.2f}"


def _customer_short(c: dict | None) -> str:
    if not c:
        return ""
    return str(c.get("firma") or c.get("name") or "").strip()


def shared(book: Book, today: date) -> dict:
    cfg = leistungen.settings(book)
    texts = daten.texts(book)
    custs = invoices.customers(book)
    projects = [{"nummer": nr, "name": str(p.get("name") or nr), "kunde_nr": str(p.get("kunde") or ""),
                 "kunde": _customer_short(custs.get(str(p.get("kunde"))))}
                for nr, p in sorted(leistungen.projects(book).items()) if p.get("status", "offen") == "offen"]
    # names only (no addresses): employees pick the customer they worked for
    customers = sorted(({"nummer": nr, "name": _customer_short(c) or nr} for nr, c in custs.items()
                        if c.get("aktiv", True) is not False), key=lambda c: c["name"].lower())
    # one fixed service (e.g. always the same hourly work): the app asks nothing, the book fills it in on import
    services = [] if daten.fixed_service(book) else [{"nummer": nr, "text": str(p.get("text") or nr)}
                                                     for nr, p in sorted(leistungen.services(book).items())]
    material = [{"nummer": nr, "text": str(p.get("text") or nr), "einheit": str(p.get("einheit") or "")}
                for nr, p in sorted(leistungen.materials(book).items()) if str(p.get("text")) != daten.TEXT_PRODUCT]
    absences_as_categories = {"Ferien", "Krank", "Feiertag", "Unfall"}
    holidays = {}
    for y in (today.year, today.year + 1):
        holidays.update({d.isoformat(): name for d, name in abwesenheit.holiday_names(book, y).items()})
    return {"firma": book.settings.firma, "kunden": customers, "projekte": projects, "leistungen": services, "material": material,
            "projekt_wort": cfg["projekt_wort"], "projekt_pflicht": cfg["projekt_pflicht"],
            "rechnung_tage": cfg["rechnung_tage"],
            "kategorien": [k for k in cfg["kategorien"] if k not in absences_as_categories],
            "abwesenheiten": list(abwesenheit.ARTEN), "feiertage": holidays,
            "text_beispiel": texts["text_beispiel"], "arztzeugnis_ab_tag": texts["arztzeugnis_ab_tag"]}


def _month_row(r: dict) -> dict:
    return {"soll": _s(r.get("soll")), "ist": _s(r.get("ist")), "saldo": _s(r.get("saldo"))}


def holidays_around(book: Book, today: date) -> set[date]:
    return abwesenheit.holidays(book, today.year - 1) | abwesenheit.holidays(book, today.year) \
        | abwesenheit.holidays(book, today.year + 1)


def balances(book: Book, p: dict, today: date, rows_abw: list[dict], hols: set[date]) -> dict:
    """Month by month of the year up to today. The running month counts its target only up to today: days
    still ahead are not missing hours. Used by the app and by the team overview, so both show the same."""
    from_ = kontrolle.opt_date(leistungen.settings(book)["kontrolle_ab"])
    months = kontrolle.year(book, today.year, p["nummer"])
    cur = dict(months[today.month - 1])
    if cur.get("soll") is not None:
        ahead = Decimal(0)
        d = today + timedelta(days=1)
        while d.month == today.month:
            t = kontrolle.day_target(p, d, hols, from_) or Decimal(0)
            ahead += t - abwesenheit.day_hours(book, p, d, hols, rows_abw)["soll_weniger"]
            d += timedelta(days=1)
        cur["soll"] -= ahead
        cur["saldo"] = cur["ist"] - cur["soll"]
        cur["saldo_jahr"] += ahead
    return {"monate": [*months[:today.month - 1], cur], "monat": cur}


def entry_data(e: dict, text_products: set[str] = frozenset()) -> tuple[str, dict]:
    """A booked Leistungen entry as the app's form fields (to change it from the phone). `text_products`: the
    book's «Textposition» numbers, shown in the app as «Anderes»."""
    if e["art"] == "Zeit":
        return "zeit", {"datum": e["datum"].isoformat(), "stunden": _s(e["menge"]), "kunde": e["kunde"],
                        "projekt": e["projekt"],
                        "leistung": e["produkt"], "kategorie": "" if e["abrechenbar"] else e["kategorie"],
                        "text": e["text"]}
    return "material", {"datum": e["datum"].isoformat(), "kunde": e["kunde"], "projekt": e["projekt"],
                        "produkt": daten.TEXT_PRODUCT_CODE if e["produkt"] in text_products else e["produkt"],
                        "menge": _s(e["menge"]), "text": e["text"]}


def person(book: Book, nr: str, today: date, entries: list[dict], rows_abw: list[dict]) -> dict:
    p = leistungen.person(book, nr)
    hols_now = holidays_around(book, today)
    from_ = kontrolle.opt_date(leistungen.settings(book)["kontrolle_ab"])
    bal = balances(book, p, today, rows_abw, hols_now)
    months, cur = bal["monate"], bal["monat"]
    holiday = abwesenheit.holiday_account(book, nr, today.year, today)
    per_day = abwesenheit.daily_target(p)
    closed = daten.closed_until(book)
    ledger = daten.imported(book)
    uuid_of = daten.by_id(book)
    status, pauschal = leistungen.context_maps(book)

    def lock(day: date, billed: bool = False) -> str:
        if billed:
            return "verrechnet"
        if closed and day <= closed:
            return "abgeschlossen"
        return ""

    days = {}
    d = today - timedelta(days=PAST_DAYS)
    while d <= today + timedelta(days=FUTURE_DAYS):
        t = kontrolle.day_target(p, d, hols_now, from_)
        if t:
            absent = abwesenheit.day_hours(book, p, d, hols_now, rows_abw)
            t -= absent["soll_weniger"]
            days[d.isoformat()] = _s(t)
        d += timedelta(days=1)

    prods = leistungen.products(book)
    projs = leistungen.projects(book)
    text_products = {nr for nr, p in prods.items() if str(p.get("text") or "") == daten.TEXT_PRODUCT}
    custs = invoices.customers(book)
    start = today - timedelta(days=PAST_DAYS)
    own = []
    for e in entries:
        if e["wer"] != nr or e["datum"] < start:
            continue
        prod = prods.get(e["produkt"], {}) if e["produkt"] else {}
        proj = projs.get(e["projekt"], {}) if e["projekt"] else {}
        customer = _customer_short(custs.get(e["kunde"])) if e["kunde"] else ""
        if e["art"] == "Zeit":
            was = (" · ".join(filter(None, [customer, str(proj.get("name") or "")])) if e["abrechenbar"] or e["kunde"]
                   else f"Intern: {e['kategorie']}")
        else:
            was = prod.get("text") or e["produkt"]
        text = e["text"] if e["text"] != prod.get("text") else ""
        if e["art"] == "Produkt":
            text = " · ".join(filter(None, [customer, str(proj.get("name") or ""), text]))
        item = {"id": e["id"], "datum": e["datum"].isoformat(), "art": e["art"], "menge": _s(e["menge"]),
                "einheit": "h" if e["art"] == "Zeit" else str(prod.get("einheit") or ""), "was": str(was or ""),
                "text": text, "projekt": e["projekt"]}
        uuid = uuid_of.get(e["id"])
        if uuid:   # recorded in the app: the employee may change it until it is locked
            kind, data = entry_data(e, text_products)
            reason = lock(e["datum"], leistungen.state(e, status, pauschal) == "abgerechnet")
            item.update(uuid=uuid, version=ledger[uuid]["version"], kind=kind, data=data,
                        aenderbar=not reason, gesperrt=reason)
        own.append(item)

    absences = []
    for a in rows_abw:
        if a["wer"] != nr or a["bis"] < today - timedelta(days=400):
            continue
        item = {"id": a["id"], "art": a["art"], "von": a["von"].isoformat(), "bis": a["bis"].isoformat(),
                "anteil": _s(a["anteil"]), "notiz": a["notiz"],
                "stunden_pro_tag": _s((abwesenheit.daily_target(p, a["von"]) or 0) * a["anteil"])
                if abwesenheit.ARTEN[a["art"]] == "zählt" else "0.00"}
        uuid = uuid_of.get(a["id"])
        if uuid:
            reason = lock(a["von"])
            item.update(uuid=uuid, version=ledger[uuid]["version"], kind="abwesenheit", aenderbar=not reason,
                        gesperrt=reason, data={"art": a["art"], "von": a["von"].isoformat(),
                                               "bis": a["bis"].isoformat(), "halb": a["anteil"] == Decimal("0.5"),
                                               "notiz": a["notiz"]})
        absences.append(item)
    requests = []
    for r in daten.requests(book):
        if r["wer"] != nr or r["bis"] < today - timedelta(days=120) or r["status"] == "zurueckgezogen":
            continue
        item = {"id": r["id"], "uuid": r.get("uuid", ""), "art": r["art"], "von": r["von"].isoformat(),
                "bis": r["bis"].isoformat(), "halb": r["halb"], "status": r["status"], "grund": r["grund"]}
        if r.get("uuid") in ledger:
            item.update(version=ledger[r["uuid"]]["version"], kind="abwesenheit",
                        aenderbar=r["status"] == "offen", gesperrt="" if r["status"] == "offen" else r["status"],
                        data={"art": r["art"], "von": r["von"].isoformat(), "bis": r["bis"].isoformat(),
                              "halb": r["halb"], "notiz": r["notiz"]})
        requests.append(item)

    return {"name": p["name"], "stand": datetime.now().astimezone().isoformat(timespec="seconds"),
            "abgeschlossen_bis": closed.isoformat() if closed else "",
            "konto": {"jahr": today.year, "stundenlohn": bool(p["stundenlohn"] or per_day is None),
                      "monat": {"jahr": today.year, "monat": today.month, **_month_row(cur)},
                      "saldo_jahr": _s(cur.get("saldo_jahr")),
                      "monate": [{"monat": m["monat"], **_month_row(m)} for m in months[:today.month]],
                      "ferien": {k: _s(holiday[k]) for k in ("anspruch", "vortrag", "bezogen", "geplant", "rest")}},
            "tage": days, "eintraege": sorted(own, key=lambda x: (x["datum"], x["id"])),
            "abwesenheiten": absences, "antraege": requests}


def office_entries(book: Book, today: date | None = None) -> list[tuple[str, str, str]]:
    """Recent Leistungen entries of people with an active app account that have no app id yet."""
    today = today or date.today()
    start = today - timedelta(days=PAST_DAYS)
    active = {nr for nr, acc in daten.accounts(book).items() if acc["aktiv"]}
    known = daten.by_id(book)
    return [(e["id"], e["wer"], "zeit" if e["art"] == "Zeit" else "material")
            for e in leistungen.entries(book)
            if e["wer"] in active and e["datum"] >= start and e["id"] not in known]


def build(book: Book, today: date | None = None) -> dict:
    today = today or date.today()
    entries = leistungen.entries(book)
    rows_abw = abwesenheit.items(book)
    people = {}
    for nr, acc in daten.accounts(book).items():
        if not acc["aktiv"]:
            continue
        try:
            people[nr] = person(book, nr, today, entries, rows_abw)
        except BookError:
            continue   # person removed from the book: the portal deactivates the account with the roster
    return api.jsonable({"stand": datetime.now().astimezone().isoformat(timespec="seconds"),
                         "shared": shared(book, today), "people": people})

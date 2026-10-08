"""The week view: a timesheet per person (rows = customer/project/Leistungsart, columns = days), the last used
combinations, favourites, and the day/week target against what is recorded.

A cell is the sum of the person's time entries of that row and day. Typing a number sets it: no entry yet → one
is recorded; exactly one entry → its hours change (0 or empty deletes it); several entries, or a billed one →
the cell is locked and edited in the list below."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from aeradex import invoices
from aeradex.book import Book, BookError
from aeradex.files import parse_date

from . import daten

ZERO = Decimal("0")
DAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def row_key(e: dict) -> tuple:
    return (e.get("kunde") or "", e.get("projekt") or "", (e.get("produkt") or "") if e.get("art", "Zeit") == "Zeit" else "",
            bool(e.get("abrechenbar", True)), e.get("kategorie") or "")


def _label(key: tuple, names: dict, projs: dict, prods: dict) -> dict:
    kunde, projekt, leistung, abrechenbar, kategorie = key
    if not abrechenbar:
        return {"titel": kategorie or "Intern", "unter": "nicht abrechenbar"}
    titel = projs.get(projekt, {}).get("name") if projekt else names.get(kunde, kunde)
    unter = names.get(kunde, kunde) if projekt else ""
    art = prods.get(leistung, {}).get("text") if leistung else ""
    return {"titel": titel or kunde, "unter": " · ".join(x for x in (unter, art) if x)}


def grid(book: Book, wer: str, start: date, with_previous: bool = False) -> dict:
    """The person's week from `start` (a Monday)."""
    from . import abwesenheit, kontrolle
    start = monday(start)
    days = [start + timedelta(days=i) for i in range(7)]
    person = daten.person(book, wer)
    status, pauschal = daten.context_maps(book)
    names = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    projs, prods = daten.projects(book), daten.products(book)
    rows: dict[tuple, dict] = {}

    def row(key):
        if key not in rows:
            rows[key] = {"key": key, "dom": re.sub(r"[^A-Za-z0-9]", "_", "|".join(str(x) for x in key)),
                         "kunde": key[0], "projekt": key[1], "leistung": key[2], "abrechenbar": key[3],
                         "kategorie": key[4], **_label(key, names, projs, prods), "text": "",
                         "zellen": [{"datum": d, "stunden": ZERO, "ids": [], "gesperrt": False} for d in days],
                         "total": ZERO, "budget": None}
        return rows[key]

    mine = [e for e in daten.entries(book) if e["art"] == "Zeit" and e["wer"] == person["nummer"]]
    for e in mine:
        if start <= e["datum"] < start + timedelta(days=7):
            r = row(row_key(e))
            cell = r["zellen"][(e["datum"] - start).days]
            cell["stunden"] += e["menge"]
            cell["ids"].append(e["id"])
            if daten.state(e, status, pauschal) == "abgerechnet":
                cell["gesperrt"] = True
            r["total"] += e["menge"]
            r["text"] = e["text"] or r["text"]
    if with_previous:
        for e in mine:
            if start - timedelta(days=7) <= e["datum"] < start:
                row(row_key(e))["text"] = row(row_key(e))["text"] or e["text"]
    for r in rows.values():
        for c in r["zellen"]:
            c["gesperrt"] = c["gesperrt"] or len(c["ids"]) > 1
        if r["projekt"]:
            st = kontrolle.project(book, r["projekt"])
            if st.get("auslastung") is not None:
                r["budget"] = st["auslastung"]
    holidays = set(abwesenheit.holidays(book, start.year) | abwesenheit.holidays(book, (start + timedelta(days=6)).year))
    soll = [kontrolle.day_target(person, d, holidays) for d in days]
    absent = [abwesenheit.day_hours(book, person, d, holidays) for d in days]
    worked = [sum((r["zellen"][i]["stunden"] for r in rows.values()), ZERO) for i in range(7)]
    ist = [worked[i] + absent[i]["stunden"] for i in range(7)]
    ordered = sorted(rows.values(), key=lambda r: (not r["abrechenbar"], r["titel"].lower(), r["unter"].lower()))
    total_soll = sum((s or ZERO for s in soll), ZERO)
    return {"wer": person["nummer"], "person": person, "start": start, "tage": days, "zeilen": ordered,
            "gearbeitet": worked, "abwesend": absent, "ist": ist, "soll": soll,
            "total": sum(worked, ZERO), "total_ist": sum(ist, ZERO), "total_soll": total_soll,
            "saldo": (sum(ist, ZERO) - total_soll) if person.get("woche") and not person.get("stundenlohn") else None,
            "feiertage": {d for d in days if d in holidays}}


def set_cell(book: Book, wer: str, datum, stunden, kunde: str = "", projekt: str = "", leistung: str = "",
             abrechenbar: bool = True, kategorie: str = "", text: str = "") -> tuple[dict, list[Path]]:
    """Set the hours of a cell (see the module doc)."""
    d = parse_date(datum, "datum")
    raw = str(stunden or "").strip().replace(",", ".")
    hours = Decimal(raw) if raw else ZERO
    if hours < 0 or hours > 24:
        raise BookError("Stunden: 0 bis 24")
    p = daten.person(book, wer)
    key = (str(kunde or "").upper(), str(projekt or "").upper(), str(leistung or "").upper(), bool(abrechenbar),
           "" if abrechenbar else (kategorie or "Intern"))
    if key[1] and not key[0]:
        key = (daten.project(book, key[1])["kunde"],) + key[1:]
    here = [e for e in daten.entries(book) if e["art"] == "Zeit" and e["wer"] == p["nummer"] and e["datum"] == d
            and row_key(e) == key]
    if len(here) > 1:
        raise BookError("Mehrere Einträge an diesem Tag — in der Liste darunter ändern")
    if here:
        e = here[0]
        if not hours:
            return daten.delete_entry(book, e["id"])
        return daten.update_entry(book, e["id"], menge=hours)
    if not hours:
        return {"id": ""}, []
    label = text or ""
    if not label and key[3] and not key[2]:
        label = daten.project(book, key[1])["name"] if key[1] else "Arbeit"
    return daten.add_time(book, d, p["nummer"], hours, label, key[0], key[1], key[3], key[4], None, key[2])


def recent(book: Book, wer: str, n: int = 8) -> list[dict]:
    """The person's last used combinations (newest first), for one-click repeat or timer start."""
    seen, out = set(), []
    for e in sorted((e for e in daten.entries(book) if e["art"] == "Zeit" and e["wer"] == wer.upper()),
                    key=lambda e: (e["datum"], e["id"]), reverse=True):
        key = row_key(e) + (e["text"],)
        if key in seen:
            continue
        seen.add(key)
        out.append(combo(book, key))
        if len(out) >= n:
            break
    return out


def combo(book: Book, key: tuple) -> dict:
    kunde, projekt, leistung, abrechenbar, kategorie, text = key
    names = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    lab = _label(key[:5], names, daten.projects(book), daten.products(book))
    quick = " ".join(x for x in (projekt or kunde, leistung, "" if abrechenbar else (kategorie or "intern"), text) if x)
    return {"kunde": kunde, "projekt": projekt, "leistung": leistung, "abrechenbar": abrechenbar,
            "kategorie": kategorie, "text": text, **lab, "schnell": quick,
            "id": "|".join(str(x) for x in key)}


def favourites(book: Book, wer: str) -> list[dict]:
    keys = daten.settings(book)["favoriten"].get(wer.upper(), [])
    out = []
    for raw in keys:
        parts = str(raw).split("|")
        if len(parts) == 6:
            out.append(combo(book, (parts[0], parts[1], parts[2], parts[3] == "True", parts[4], parts[5])))
    return out


def toggle_favourite(book: Book, wer: str, combo_id: str) -> tuple[dict, list[Path]]:
    cfg = daten.settings(book)
    nr = daten.person(book, wer)["nummer"]
    favs = list(cfg["favoriten"].get(nr, []))
    if combo_id in favs:
        favs.remove(combo_id)
    else:
        favs.append(combo_id)
    cfg["favoriten"][nr] = favs
    return {"favorit": combo_id in favs}, [daten.save_settings(book, cfg)]

"""The Wochenrapport taken over: a person's week becomes working time, checked against the Regie-Rapporte.

The Wochenrapport is the truth for the hours worked (every hour, also on flat-rate objects and in the workshop);
the Regie-Rapport, signed by the customer, is the truth for what is billed. So a day's line for a customer that a
Regie-Rapport already brought in is not recorded twice — it is compared («abgleich»), a difference goes on the list.
Other lines become time entries: on a flat-rate object for the project control, on a customer by the hour as billable
work with the note that the Regie-Rapport is missing, internal work under its category. Travel time is its own
category (not billable; the GAV working-time control counts it). Ferien, krank … in a line become absences.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from aeradex.book import Book, BookError
from aeradex.files import parse_date, read_yaml, write_yaml

from . import abwesenheit, daten

ZERO = Decimal("0")
TRAVEL = "Reisezeit"
ABSENCE_WORDS = [(r"ferien|urlaub", "Ferien"), (r"krank", "Krank"), (r"unfall", "Unfall"),
                 (r"milit|zivil|schutzdienst", "Militär/ZS"), (r"vaterschaft|mutterschaft", "Mutterschaft/Vaterschaft"),
                 (r"kompens|gleitzeit|\bkomp\b", "Kompensation"), (r"unbezahlt", "Unbezahlt")]
SCHOOL_WORDS = r"schule|berufsschule|\bbs\b|\bük\b|kurs"


def _h(value) -> Decimal:
    from .rapporte import amount
    text = amount(value)
    return Decimal(text) if text else ZERO


def absence_of(line: dict) -> str:
    """«Ferien», «krank» … in the line's text or remark → the absence kind; else ''."""
    text = f"{line.get('text') or ''} {line.get('bemerkung') or ''}".lower()
    return next((art for pattern, art in ABSENCE_WORDS if re.search(pattern, text)), "")


def category_of(book: Book, line: dict) -> str:
    """Internal work (Werkstatt, Büro, Schule …) → one of the book's categories; else ''."""
    from .rapporte import _best
    cats = [c for c in daten.settings(book)["kategorien"] if c not in ("Ferien", "Krank", "Feiertag", TRAVEL)]
    text = line.get("text") or ""
    if re.search(SCHOOL_WORDS, text.lower()):
        school = [c for c in cats if re.search(SCHOOL_WORDS, c.lower())]
        if school:
            return school[0]
    return _best(text, {c: [c] for c in cats}, 0.7)


def kind_of(book: Book, line: dict) -> str:
    """kunde | absenz | intern | '' (unclear: the person chooses). What was chosen (or matched) wins; the words of
    the line («Ferien», «Werkstatt») only decide a line without a customer or object."""
    if line.get("absenz"):
        return "absenz"
    if line.get("kategorie"):
        return "intern"
    if line.get("kunde") or line.get("projekt"):
        return "kunde"
    if absence_of(line):
        return "absenz"
    return "intern" if category_of(book, line) else ""


def problems(book: Book, meta: dict) -> list[str]:
    out = []
    if not meta.get("person"):
        out.append(f"Person wählen («{meta.get('gelesen') or 'Name unleserlich'}»)")
    if not meta.get("tage"):
        out.append("Keine Tage erfasst")
    for t in meta.get("tage") or []:
        if not t.get("datum"):
            out.append("Ein Tag ohne Datum")
            continue
        label = parse_date(t["datum"]).strftime("%a %d.%m.")
        for line in t.get("zeilen") or []:
            k = kind_of(book, line)
            if not k:
                out.append(f"{label}: «{line.get('text')}» — Kunde/Objekt oder Kategorie wählen")
            elif k in ("kunde", "intern") and not _h(line.get("stunden")):
                out.append(f"{label}: «{line.get('text')}» ohne Stunden")
    return out


def _ensure_travel_category(book: Book) -> list[Path]:
    cfg = daten.settings(book)
    if TRAVEL in cfg["kategorien"]:
        return []
    path = daten.settings_path(book)
    raw = (read_yaml(path) or {}) if path.exists() else {}
    raw["kategorien"] = list(raw.get("kategorien") or daten.KATEGORIEN) + [TRAVEL]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, raw)
    return [path]


def regie_ids(book: Book) -> set[str]:
    """Entries that came from Regie-Rapporte."""
    from .rapporte import reports
    return {i for r in reports(book).values() if r.get("art") == "regie" and r.get("status") == "uebernommen"
            for i in r.get("eintraege") or []}


def _regie_hours(book: Book, person: str, d: date, kunde: str, projekt: str, ids: set[str]) -> list[dict]:
    """The person's hours on that day for that customer (and object) that a Regie-Rapport brought."""
    return [e for e in daten.entries(book)
            if e["id"] in ids and e["wer"] == person and e["datum"] == d and e["kunde"] == kunde
            and (not projekt or e["projekt"] == projekt)]


def take_over(book: Book, meta: dict) -> tuple[dict, list[Path]]:
    from .rapporte import rate_or_zero, regie_service
    person = daten.person(book, meta["person"])["nummer"]
    touched = _ensure_travel_category(book)
    ids, abgleich, absences, ohne_regie = [], [], [], []
    pauschal = {nr for nr, p in daten.projects(book).items() if p.get("abrechnung") == "pauschal"}
    leistung = regie_service(book)
    by_day = daten.settings(book)["rechnung_tage"]
    from_regie = regie_ids(book)
    for t in meta["tage"]:
        d = parse_date(t["datum"])
        travel = _h(t.get("reisezeit"))
        if travel:
            e, paths = daten.add_time(book, d, person, travel, TRAVEL, "", "", False, TRAVEL)
            ids.append(e["id"])
            touched += paths
        work: dict[tuple, dict] = {}            # the day's lines per customer/object, added up
        for line in t.get("zeilen") or []:
            k = kind_of(book, line)
            hours = _h(line.get("stunden"))
            if k == "absenz":
                art = line.get("absenz") or absence_of(line)
                share = Decimal("0.5") if hours and hours <= Decimal("4.5") else 1
                a, paths = abwesenheit.add(book, person, art, d, None, share, line.get("bemerkung") or "")
                absences.append(a["id"])
                touched += paths
                continue
            text = line.get("bemerkung") or line.get("text") or "Arbeit"
            if k == "intern":
                cat = line.get("kategorie") or category_of(book, line)
                e, paths = daten.add_time(book, d, person, hours, text, "", "", False, cat)
                ids.append(e["id"])
                touched += paths
                continue
            projekt = line.get("projekt") or ""
            kunde = line.get("kunde") or (daten.project(book, projekt)["kunde"] if projekt else "")
            w = work.setdefault((kunde, projekt), {"stunden": ZERO, "texte": []})
            w["stunden"] += hours
            if line.get("bemerkung"):
                w["texte"].append(line["bemerkung"])
        for (kunde, projekt), w in work.items():
            hours = w["stunden"]
            regie = _regie_hours(book, person, d, kunde, projekt, from_regie) if projekt not in pauschal else []
            if regie:
                billed = sum((e["menge"] for e in regie), ZERO)
                abgleich.append({"datum": d.isoformat(), "kunde": kunde, "projekt": projekt, "woche": str(hours),
                                 "regie": str(billed), "differenz": str(hours - billed)})
                if hours > billed:                  # worked longer than billed: it is still working time
                    e, paths = _extra_time(book, person, d, hours - billed)
                    ids.append(e["id"])
                    touched += paths
                continue
            text = "; ".join(w["texte"]) or ("" if by_day else "Arbeit")
            e, paths = daten.add_time(book, d, person, hours, text, kunde, projekt, True, "",
                                      rate_or_zero(book, person, kunde, projekt, leistung), leistung)
            ids.append(e["id"])
            touched += paths
            if projekt not in pauschal:
                ohne_regie.append({"datum": d.isoformat(), "kunde": kunde, "projekt": projekt, "stunden": str(hours),
                                   "eintrag": e["id"]})
    return {"eintraege": ids, "abgleich": abgleich, "abwesenheiten": absences, "ohne_regie": ohne_regie}, touched


def week_of(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return iso[0], iso[1]


def overview(book: Book, jahr: int, kw: int) -> list[dict]:
    """Per person: the Wochenrapport of the week (status, hours, travel) against the Regie hours of that week, and
    the differences found when it was taken over."""
    from .rapporte import reports
    monday = date.fromisocalendar(jahr, kw, 1)
    days = {monday + timedelta(days=i) for i in range(7)}
    from_regie = regie_ids(book)
    entries = [e for e in daten.entries(book) if e["datum"] in days and e["art"] == "Zeit"]
    weeks = [r for r in reports(book).values() if r.get("art") == "woche" and r.get("status") != "verworfen"
             and int(r.get("jahr") or 0) == jahr and int(r.get("kw") or 0) == kw]
    out = []
    for nr, p in daten.people(book).items():
        if not p.get("aktiv", True):
            continue
        mine = [r for r in weeks if r.get("person") == nr]
        rep = mine[-1] if mine else None
        stunden = reise = ZERO
        if rep:
            for t in rep.get("tage") or []:
                reise += _h(t.get("reisezeit"))
                stunden += sum((_h(l.get("stunden")) for l in t.get("zeilen") or [] if kind_of(book, l) != "absenz"),
                               ZERO)
        regie = sum((e["menge"] for e in entries if e["wer"] == nr and e["id"] in from_regie), ZERO)
        out.append({"person": nr, "name": p["name"], "rapport": rep["nummer"] if rep else "",
                    "status": rep.get("status") if rep else "fehlt", "stunden": stunden, "reisezeit": reise,
                    "regie": regie, "abgleich": [a for a in (rep or {}).get("abgleich") or [] if Decimal(a["differenz"])],
                    "ohne_regie": (rep or {}).get("ohne_regie") or []})
    return out


DIFFERENCE_TEXT = "Mehr Stunden laut Wochenrapport als im Regie-Rapport"


def _extra_time(book: Book, person: str, d: date, hours: Decimal) -> tuple[dict, list[Path]]:
    """Hours a person worked on a Regie job beyond what the customer signed: working time, not billed."""
    cats = daten.settings(book)["kategorien"]
    cat = "Intern" if "Intern" in cats else cats[0]
    return daten.add_time(book, d, person, hours, DIFFERENCE_TEXT, "", "", False, cat)


def replace_week_entries(book: Book, person: str, d: date, kunde: str, projekt: str, regie_hours: Decimal
                         ) -> list[Path]:
    """A Regie-Rapport taken over after the Wochenrapport: the week's billable placeholder for that day and customer
    («ohne Regie-Rapport») goes, the hours are compared instead — so nothing is billed twice."""
    from .rapporte import reports
    touched: list[Path] = []
    for r in reports(book).values():
        if r.get("art") != "woche" or r.get("status") != "uebernommen" or r.get("person") != person:
            continue
        keep, changed = [], False
        for o in r.get("ohne_regie") or []:
            if (o["datum"] == d.isoformat() and o["kunde"] == kunde and (o.get("projekt") or "") == projekt):
                try:
                    _, paths = daten.delete_entry(book, o["eintrag"])
                    touched += paths
                except BookError:
                    keep.append(o)                  # already billed: stays, the check list shows it
                    continue
                r.setdefault("abgleich", []).append(
                    {"datum": o["datum"], "kunde": kunde, "projekt": projekt, "woche": o["stunden"],
                     "regie": str(regie_hours), "differenz": str(Decimal(o["stunden"]) - regie_hours)})
                r["eintraege"] = [i for i in r.get("eintraege") or [] if i != o["eintrag"]]
                if Decimal(o["stunden"]) > regie_hours:
                    e, paths = _extra_time(book, person, d, Decimal(o["stunden"]) - regie_hours)
                    r["eintraege"].append(e["id"])
                    touched += paths
                changed = True
            else:
                keep.append(o)
        if changed:
            path = r.pop("_pfad")
            r.pop("nummer", None)
            r["ohne_regie"] = keep
            write_yaml(path, r)
            touched.append(path)
    return touched

"""What the team plugin keeps in the book (all in git, no secrets):

* ``team/einstellungen.yaml``: who has an app account (person → benutzer, aktiv) and up to which day the time
  recording is closed (``abgeschlossen_bis``): entries up to then can no longer be changed from the app. Optional:
  ``branche`` and ``text_beispiel`` (for the spelling correction and the app's placeholder) and
  ``arztzeugnis_ab_tag`` (the app reminds of a doctor's certificate from that day of an illness on).
* ``team/importiert.yaml``: every entry taken over from the app (uuid → aeradex id, version), so nothing is booked
  twice and a later change from the app replaces exactly that entry.
* ``team/antraege.yaml``: absence requests (Ferien …) waiting for the office, and its decisions.
* ``team/geprueft.yaml``: entries the office has checked, with a fingerprint: a later change makes them «zu prüfen» again.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

from aeradex.book import Book, BookError
from aeradex.files import parse_date, read_yaml, write_yaml
from aeradex_leistungen import abwesenheit
from aeradex_leistungen import daten as leistungen

ANTRAG_ARTEN = ("Ferien", "Kompensation", "Unbezahlt", "Weiterbildung")
DIREKT_ARTEN = ("Krank", "Unfall")


def _path(book: Book, name: str) -> Path:
    return book.root / "team" / name


def _read(book: Book, name: str) -> dict:
    p = _path(book, name)
    return (read_yaml(p) or {}) if p.exists() else {}


# ---------- accounts ----------

def accounts(book: Book) -> dict[str, dict]:
    raw = _read(book, "einstellungen.yaml").get("personen") or {}
    return {str(k): {"benutzer": str(v.get("benutzer") or ""), "aktiv": v.get("aktiv", True) is not False}
            for k, v in raw.items()}


def _save_settings(book: Book, **changes) -> Path:
    p = _path(book, "einstellungen.yaml")
    data = dict(_read(book, "einstellungen.yaml"))
    data.update(changes)
    write_yaml(p, {k: data[k] for k in ("personen", "abgeschlossen_bis", "textkorrektur", "leistungsart", *TEXTE)
                   if data.get(k) not in (None, "")})
    return p


TEXTE = ("branche", "text_beispiel", "arztzeugnis_ab_tag", "stunden_in_app")


def texts(book: Book) -> dict:
    """What the app and the spelling correction say about the trade, set by hand in team/einstellungen.yaml."""
    raw = _read(book, "einstellungen.yaml")
    try:
        sick_note = max(0, int(raw.get("arztzeugnis_ab_tag") or 0))
    except (TypeError, ValueError):
        sick_note = 0
    return {"branche": str(raw.get("branche") or "Handwerksbetrieb"),
            "text_beispiel": str(raw.get("text_beispiel") or ""), "arztzeugnis_ab_tag": sick_note,
            # false: hours come from paper (scanned Wochenrapporte); the app keeps requests, account and documents
            "stunden_in_app": str(raw.get("stunden_in_app", True)).strip().lower() not in ("false", "nein", "0", "no")}


def set_texts(book: Book, values: dict) -> tuple[dict, list[Path]]:
    path = _save_settings(book, **{k: values[k] for k in TEXTE if k in values})
    return texts(book), [path]


def text_correction(book: Book) -> bool:
    """Spelling and style of the employees' texts corrected by the book's agent (on unless switched off)."""
    return _read(book, "einstellungen.yaml").get("textkorrektur", True) is not False


def set_hours_in_app(book: Book, on: bool) -> tuple[dict, list[Path]]:
    """False: hours and material come on paper (scanned reports); the app keeps requests, account and documents."""
    return {"stunden_in_app": bool(on)}, [_save_settings(book, stunden_in_app=bool(on))]


def set_text_correction(book: Book, on: bool) -> tuple[dict, list[Path]]:
    return {"textkorrektur": bool(on)}, [_save_settings(book, textkorrektur=bool(on))]


TEXT_PRODUCT_CODE = "TEXT"          # what the app sends for «Anderes (eigener Text)»
TEXT_PRODUCT = "Textposition"       # the book's product for it: price 0 until the office sets one


def text_product(book: Book) -> tuple[str, list[Path]]:
    """The book's «Textposition» product (created on first use): material the catalog does not have."""
    for nr, p in leistungen.materials(book).items():
        if str(p.get("text") or "") == TEXT_PRODUCT and p.get("aktiv", True) is not False:
            return nr, []
    res, paths = leistungen.add_product(book, TEXT_PRODUCT, "0", "Stk")
    return res["nummer"], list(paths)


def fixed_service(book: Book) -> str:
    """The one service (Leistungsart) every hour from the app is booked as; "" = employees choose it."""
    return str(_read(book, "einstellungen.yaml").get("leistungsart") or "")


def set_fixed_service(book: Book, nummer: str) -> tuple[dict, list[Path]]:
    nummer = str(nummer or "").strip()
    if nummer and nummer not in leistungen.services(book):
        raise BookError(f"Leistungsart {nummer} gibt es nicht (Leistungen › Stammdaten)")
    return {"leistungsart": nummer}, [_save_settings(book, leistungsart=nummer)]


def _save_accounts(book: Book, items: dict) -> Path:
    return _save_settings(book, personen={k: items[k] for k in sorted(items)})


def closed_until(book: Book) -> date | None:
    raw = _read(book, "einstellungen.yaml").get("abgeschlossen_bis")
    return parse_date(raw) if raw else None


def close_until(book: Book, bis) -> tuple[dict, list[Path]]:
    """Close the time recording up to a day: the app can no longer add, change or delete entries up to then.
    Empty reopens everything. In aeradex itself the office can still change them."""
    day = parse_date(bis, "bis") if str(bis or "").strip() else None
    return ({"abgeschlossen_bis": day.isoformat() if day else ""},
            [_save_settings(book, abgeschlossen_bis=day.isoformat() if day else "")])


def suggest_user(name: str, taken: set[str]) -> str:
    first = (name.split() or ["mitarbeiter"])[0]
    base = unicodedata.normalize("NFKD", first.lower()).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]", "", base) or "mitarbeiter"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}{n}", n + 1
    return out


def set_account(book: Book, person: str, benutzer: str = "", aktiv: bool = True) -> tuple[dict, list[Path]]:
    """Give a person an app account (or switch it off). The username defaults to the first name."""
    p = leistungen.person(book, person)
    items = accounts(book)
    taken = {a["benutzer"] for k, a in items.items() if k != p["nummer"]}
    user = (benutzer or items.get(p["nummer"], {}).get("benutzer") or suggest_user(p["name"], taken)).strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,40}", user):
        raise BookError("Benutzername: 2–40 Zeichen, Kleinbuchstaben, Ziffern, Punkt, Bindestrich")
    if user in taken:
        raise BookError(f"Benutzername {user} ist schon vergeben")
    items[p["nummer"]] = {"benutzer": user, "aktiv": bool(aktiv)}
    return {"person": p["nummer"], **items[p["nummer"]]}, [_save_accounts(book, items)]


# ---------- import ledger ----------

def imported(book: Book) -> dict[str, dict]:
    return {str(k): dict(v) for k, v in (_read(book, "importiert.yaml").get("eintraege") or {}).items()}


def by_id(book: Book) -> dict[str, str]:
    """aeradex id → uuid of the app entry it came from."""
    return {v["id"]: k for k, v in imported(book).items() if not v.get("geloescht")}


def adopt(book: Book, items: list[tuple[str, str, str]]) -> tuple[dict, list[Path]]:
    """Give entries the office recorded for a person an app id (version 0), so the person can change
    them in the app like their own. `items`: (aeradex id, person, kind)."""
    import uuid as uuidlib
    rows = imported(book)
    taken = {v["id"] for v in rows.values() if not v.get("geloescht")}
    added = 0
    for aeradex_id, person, kind in items:
        if aeradex_id in taken:
            continue
        rows[str(uuidlib.uuid4())] = {"id": aeradex_id, "version": 0, "person": person, "art": kind,
                                     "am": date.today().isoformat(), "buero": True}
        added += 1
    p = _path(book, "importiert.yaml")
    write_yaml(p, {"eintraege": {k: rows[k] for k in sorted(rows)}})
    return {"freigegeben": added}, [p]


def remember(book: Book, uuid: str, aeradex_id: str, version: int, person: str, kind: str,
             geloescht: bool = False, original: str = "") -> Path:
    items = imported(book)
    before = items.get(uuid, {})
    first = before.get("am") or date.today().isoformat()
    items[uuid] = {"id": aeradex_id, "version": int(version), "person": person, "art": kind, "am": first,
                   **({"buero": True} if before.get("buero") else {})}
    if items[uuid]["am"] != date.today().isoformat():
        items[uuid]["geaendert"] = date.today().isoformat()
    if geloescht:
        items[uuid]["geloescht"] = True
    if original:                       # the employee's own words, before the spelling/style correction
        items[uuid]["original_text"] = original
    p = _path(book, "importiert.yaml")
    write_yaml(p, {"eintraege": {k: items[k] for k in sorted(items)}})
    return p


# ---------- absence requests ----------

def requests(book: Book) -> list[dict]:
    out = []
    for a in _read(book, "antraege.yaml").get("antraege") or []:
        out.append({**a, "id": str(a["id"]), "von": parse_date(a["von"]), "bis": parse_date(a["bis"]),
                    "halb": bool(a.get("halb")), "grund": str(a.get("grund") or ""),
                    "abwesenheit": str(a.get("abwesenheit") or ""), "notiz": str(a.get("notiz") or "")})
    return out


def _save_requests(book: Book, rows: list[dict]) -> Path:
    p = _path(book, "antraege.yaml")
    write_yaml(p, {"antraege": [{**r, "von": r["von"].isoformat(), "bis": r["bis"].isoformat()}
                                for r in sorted(rows, key=lambda r: r["id"])]})
    return p


def add_request(book: Book, uuid: str, wer: str, art: str, von, bis, halb: bool, notiz: str) -> tuple[dict, list[Path]]:
    p = leistungen.person(book, wer)
    if art not in ANTRAG_ARTEN:
        raise BookError(f"Antrag: {', '.join(ANTRAG_ARTEN)}")
    v, b = parse_date(von, "von"), parse_date(bis or von, "bis")
    if b < v:
        raise BookError("«bis» liegt vor «von»")
    rows = requests(book)
    nums = [int(r["id"][4:]) for r in rows if r["id"][4:].isdigit()]
    new = {"id": f"ANT-{max(nums, default=0) + 1:04d}", "uuid": uuid, "wer": p["nummer"], "art": art, "von": v,
           "bis": b, "halb": bool(halb), "notiz": str(notiz or "").strip(), "status": "offen", "grund": "",
           "abwesenheit": "", "eingegangen": date.today().isoformat()}
    return new, [_save_requests(book, rows + [new])]


def decide(book: Book, rid: str, bewilligen: bool, grund: str = "") -> tuple[dict, list[Path]]:
    """Approve (→ the absence goes into the calendar) or refuse a request."""
    rows = requests(book)
    found = next((r for r in rows if r["id"] == rid), None)
    if found is None:
        raise BookError(f"Antrag {rid} nicht gefunden")
    if found["status"] != "offen":
        raise BookError(f"Antrag {rid} ist schon {found['status']}")
    touched = []
    if bewilligen:
        absence, paths = abwesenheit.add(book, found["wer"], found["art"], found["von"], found["bis"],
                                         "0.5" if found["halb"] else 1, found["notiz"] or f"App-Antrag {rid}")
        found.update(status="bewilligt", abwesenheit=absence["id"], grund=str(grund or "").strip())
        touched += paths
    else:
        if not str(grund or "").strip():
            raise BookError("Bitte einen Grund angeben, er erscheint in der App")
        found.update(status="abgelehnt", grund=str(grund).strip())
    found["entschieden"] = date.today().isoformat()
    touched.append(_save_requests(book, rows))
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in found.items()}, touched


def withdraw_request(book: Book, rid: str) -> tuple[dict, list[Path]]:
    rows = requests(book)
    found = next((r for r in rows if r["id"] == rid), None)
    if found is None or found["status"] != "offen":
        raise BookError(f"Antrag {rid} ist schon entschieden" if found else f"Antrag {rid} nicht gefunden")
    found.update(status="zurueckgezogen", entschieden=date.today().isoformat())
    return {"id": rid}, [_save_requests(book, rows)]


def change_request(book: Book, rid: str, art: str, von, bis, halb: bool, notiz: str) -> tuple[dict, list[Path]]:
    rows = requests(book)
    found = next((r for r in rows if r["id"] == rid), None)
    if found is None or found["status"] != "offen":
        raise BookError(f"Antrag {rid} ist schon entschieden" if found else f"Antrag {rid} nicht gefunden")
    if art not in ANTRAG_ARTEN:
        raise BookError(f"Antrag: {', '.join(ANTRAG_ARTEN)}")
    v, b = parse_date(von, "von"), parse_date(bis or von, "bis")
    if b < v:
        raise BookError("«bis» liegt vor «von»")
    found.update(art=art, von=v, bis=b, halb=bool(halb), notiz=str(notiz or "").strip())
    return {"id": rid}, [_save_requests(book, rows)]


# ---------- checked entries ----------

def fingerprint(item: dict) -> str:
    keep = {k: item.get(k) for k in ("datum", "art", "wer", "kunde", "projekt", "produkt", "menge", "text",
                                     "abrechenbar", "kategorie", "von", "bis", "anteil", "notiz")}
    return hashlib.sha256(json.dumps(keep, default=str, sort_keys=True).encode()).hexdigest()[:16]


def checked(book: Book) -> dict[str, dict]:
    return {str(k): dict(v) for k, v in (_read(book, "geprueft.yaml").get("eintraege") or {}).items()}


def mark_checked(book: Book, items: list[dict], von: str = "") -> tuple[dict, list[Path]]:
    """Mark entries (Leistungen L-…, absences A-…) as checked, with their current fingerprint."""
    done = checked(book)
    for item in items:
        done[item["id"]] = {"fp": fingerprint(item), "am": date.today().isoformat(), **({"von": von} if von else {})}
    p = _path(book, "geprueft.yaml")
    write_yaml(p, {"eintraege": {k: done[k] for k in sorted(done)}})
    return {"geprueft": len(items)}, [p]

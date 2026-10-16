"""Take over entries from the app into the book: each one in its own checked, committed write, exactly once.

An entry keeps its uuid for life. A higher version of a known uuid is a change from the app (or, with
``_loeschen``, a deletion): it replaces the booked entry, unless the book says it is locked (time recording closed
up to that day, already invoiced, request already decided). The book decides, not the phone.
"""
from __future__ import annotations

from datetime import date

from aeradex import api, gitlog
from aeradex.book import Book, BookError
from aeradex.files import FormatError, parse_date
from aeradex_leistungen import abwesenheit
from aeradex_leistungen import daten as leistungen

from . import daten, verbindung


def _day(d: dict) -> date | None:
    raw = d.get("datum") or d.get("von")
    return parse_date(raw) if raw else None


def lock_reason(book: Book, aeradex_id: str, closed: date | None = None) -> str:
    """Why the app may no longer change this booked entry ('' = it may)."""
    closed = closed if closed is not None else daten.closed_until(book)
    if aeradex_id.startswith("L-"):
        try:
            e = leistungen.entry(book, aeradex_id)
        except BookError:
            return ""
        status, pauschal = leistungen.context_maps(book)
        if leistungen.state(e, status, pauschal) == "abgerechnet":
            return f"bereits verrechnet ({e['rechnung']})"
        if closed and e["datum"] <= closed:
            return f"Zeiterfassung bis {closed:%d.%m.%Y} abgeschlossen"
    elif aeradex_id.startswith("A-"):
        found = next((a for a in abwesenheit.items(book) if a["id"] == aeradex_id), None)
        if found and closed and found["von"] <= closed:
            return f"Zeiterfassung bis {closed:%d.%m.%Y} abgeschlossen"
    elif aeradex_id.startswith("ANT-"):
        found = next((r for r in daten.requests(book) if r["id"] == aeradex_id), None)
        if found and found["status"] != "offen":
            return f"Antrag ist schon {found['status']}"
    return ""


def _remove(book: Book, aeradex_id: str) -> list:
    """Take a booked app entry out of the book (it may already be gone: the office deleted it)."""
    try:
        if aeradex_id.startswith("L-"):
            return leistungen.delete_entry(book, aeradex_id)[1]
        if aeradex_id.startswith("A-"):
            return abwesenheit.delete(book, aeradex_id)[1]
        if aeradex_id.startswith("ANT-"):
            return daten.withdraw_request(book, aeradex_id)[1]
    except BookError as exc:
        if "nicht gefunden" not in str(exc):
            raise
    return []


def _add(book: Book, uuid: str, kind: str, d: dict, wer: str) -> tuple[dict, list]:
    if kind == "zeit":
        kunde, projekt = str(d.get("kunde") or ""), str(d.get("projekt") or "")
        billable = bool(kunde or projekt)
        service = (d.get("leistung") or daten.fixed_service(book)) if billable else ""
        args = (book, d["datum"], wer, d["stunden"], d.get("text") or "", kunde, projekt, billable,
                "" if billable else d.get("kategorie") or "Intern")
        try:
            result, paths = leistungen.add_time(*args, None, service)
        except BookError as exc:
            # a missing billing rate is the office's to fix, not the employee's: take the hours over at 0 and
            # flag them; after setting the rate, «neu bewerten» in Leistungen prices them
            if "Stundensatz" not in str(exc):
                raise
            result, paths = leistungen.add_time(*args, "0", service)
            result["ohne_satz"] = True
        return result, paths
    if kind == "material":
        produkt, extra = d["produkt"], []
        if produkt == daten.TEXT_PRODUCT_CODE:      # «Anderes»: free text, priced by the office (flag «ohne Preis»)
            produkt, extra = daten.text_product(book)
            if not str(d.get("text") or "").strip():
                raise BookError("Textposition: beschreiben, was verwendet wurde")
        result, paths = leistungen.add_material(book, d["datum"], d.get("kunde") or "", produkt, d["menge"],
                                                d.get("projekt") or "", None, d.get("text") or "", wer)
        return result, list(extra) + list(paths)
    if kind == "abwesenheit" and d.get("art") in daten.DIREKT_ARTEN:
        return abwesenheit.add(book, wer, d["art"], d["von"], d.get("bis") or d["von"],
                               "0.5" if d.get("halb") else 1, d.get("notiz") or "per App gemeldet")
    if kind == "abwesenheit":
        return daten.add_request(book, uuid, wer, d["art"], d["von"], d.get("bis") or d["von"],
                                 bool(d.get("halb")), d.get("notiz") or "")
    raise BookError(f"Unbekannte Art {kind}")


def _book_it(book: Book, entry: dict) -> tuple[dict, list]:
    """Inside the transaction: book (or change, or delete) the entry and remember its uuid together."""
    uuid, kind, d, wer = entry["uuid"], entry["kind"], entry["data"], entry["person"]
    version = int(entry.get("version") or 1)
    known = daten.imported(book).get(uuid)
    if known and int(known.get("version", 1)) >= version:
        return {"id": known["id"], "wiederholt": True, "geloescht": bool(known.get("geloescht"))}, []
    closed = daten.closed_until(book)
    paths: list = []
    if known and not known.get("geloescht"):
        if known["id"].startswith("L-"):
            try:
                if leistungen.entry(book, known["id"])["wer"] != wer:
                    raise BookError("Dieser Eintrag gehört nicht (mehr) dir. Bitte im Büro melden.")
            except BookError as exc:
                if "nicht gefunden" not in str(exc):
                    raise
        reason = lock_reason(book, known["id"], closed)
        if reason:
            raise BookError(f"Nicht mehr änderbar: {reason}")
        if d.get("_loeschen"):
            paths += _remove(book, known["id"])
            paths.append(daten.remember(book, uuid, known["id"], version, wer, kind, geloescht=True))
            return {"id": known["id"], "geloescht": True}, paths
        new_day = _day(d)
        if closed and new_day and new_day <= closed:
            raise BookError(f"Zeiterfassung bis {closed:%d.%m.%Y} abgeschlossen")
        still_request = (known["id"].startswith("ANT-") and kind == "abwesenheit"
                         and d.get("art") in daten.ANTRAG_ARTEN)
        if still_request:   # an open request is changed in place, keeping its number
            _, changed = daten.change_request(book, known["id"], d["art"], d["von"], d.get("bis") or d["von"],
                                              bool(d.get("halb")), d.get("notiz") or "")
            paths += changed + [daten.remember(book, uuid, known["id"], version, wer, kind)]
            return {"id": known["id"]}, paths
        paths += _remove(book, known["id"])
    elif d.get("_loeschen"):
        return {"id": "", "geloescht": True}, [daten.remember(book, uuid, "", version, wer, kind, geloescht=True)]
    else:
        day = _day(d)
        if closed and day and day <= closed:
            raise BookError(f"Zeiterfassung bis {closed:%d.%m.%Y} abgeschlossen. Bitte im Büro melden.")
    result, added = _add(book, uuid, kind, d, wer)
    paths += list(added) + [daten.remember(book, uuid, result["id"], version, wer, kind,
                                           original=entry.get("_original") or "")]
    return {"id": result["id"], "ohne_satz": bool(result.get("ohne_satz")), "geaendert": bool(known)}, paths


def _corrected(book: Book, entries: list[dict], roster: dict, known: dict) -> list[dict]:
    """The texts of new and changed time/material entries, corrected for spelling and style in one call."""
    from . import korrektur
    todo = {}
    for entry in entries:
        mine = known.get(entry["uuid"])
        if mine and int(mine.get("version", 1)) >= int(entry.get("version") or 1):
            continue
        if entry["kind"] in ("zeit", "material") and not entry["data"].get("_loeschen") \
                and roster.get(entry["person"], {}).get("aktiv"):
            todo[entry["uuid"]] = str(entry["data"].get("text") or "")
    fixed = korrektur.polish(book, todo)
    out = []
    for entry in entries:
        new = fixed.get(entry["uuid"])
        if new:
            entry = {**entry, "data": {**entry["data"], "text": new}, "_original": entry["data"]["text"]}
        out.append(entry)
    return out


def _message(entry: dict, name: str, change: bool) -> str:
    d = entry["data"]
    if d.get("_loeschen"):
        return f"App: {name} löscht einen Eintrag"
    prefix = "ändert " if change else ""
    if entry["kind"] == "zeit":
        return f"App: {name} {prefix}{d.get('stunden')} h am {d.get('datum')}"
    if entry["kind"] == "material":
        return f"App: {name} {prefix}Material {d.get('produkt')} am {d.get('datum')}"
    what = "ändert" if change else ("meldet" if d.get("art") in daten.DIREKT_ARTEN else "beantragt")
    return f"App: {name} {what} {d.get('art')} {d.get('von')}–{d.get('bis') or d.get('von')}"


def take_over(book: Book, entries: list[dict]) -> list[dict]:
    """Acks for the portal, one per entry. An entry the book refuses is acked as `abgelehnt` with the reason,
    so the employee can correct it; a book that cannot be written at all stops the whole run (no ack)."""
    if entries:
        api.guard(book)   # a book with errors refuses every write: stop here instead of refusing each entry
    roster = daten.accounts(book)
    known = daten.imported(book)
    entries = _corrected(book, entries, roster, known)
    out = []
    for entry in entries:
        uuid, version = entry["uuid"], int(entry.get("version") or 1)
        mine = known.get(uuid)
        if mine and int(mine.get("version", 1)) >= version:   # a resend: answer as before
            out.append({"uuid": uuid, "version": version,
                        "status": "geloescht" if mine.get("geloescht") else "uebernommen",
                        "aeradex_id": mine["id"], "reason": ""})
            continue
        account = roster.get(entry["person"])
        if not account or not account["aktiv"]:
            out.append({"uuid": uuid, "version": version, "status": "abgelehnt", "aeradex_id": "",
                        "reason": "Kein aktives App-Konto"})
            continue
        try:
            name = leistungen.person(book, entry["person"])["name"]
        except BookError:
            name = entry["person"]
        token = gitlog.AUTHOR.set(f"{name} (App) <{entry['person'].lower()}@{verbindung.mail_domain()}>")
        try:
            res = api.write(Book(book.root), _message(entry, name, bool(mine)), _book_it, entry)["ergebnis"]
            out.append({"uuid": uuid, "version": version,
                        "status": "geloescht" if res.get("geloescht") else "uebernommen",
                        "aeradex_id": res["id"], "reason": "",
                        **({"ohne_satz": True} if res.get("ohne_satz") else {})})
        except (BookError, FormatError, KeyError, ValueError) as exc:
            out.append({"uuid": uuid, "version": version, "status": "abgelehnt",
                        "aeradex_id": mine["id"] if mine else "", "reason": str(exc).split("\n")[0][:300]})
        finally:
            gitlog.AUTHOR.reset(token)
        known = daten.imported(Book(book.root))
    return out

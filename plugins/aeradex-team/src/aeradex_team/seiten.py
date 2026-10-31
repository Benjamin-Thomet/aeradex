"""Team page in aeradex: app accounts, invitation codes, absence requests, sync status."""
from __future__ import annotations

from datetime import date, datetime

from aeradex import api
from aeradex.book import Book, BookError
from aeradex.plugins import Page
from aeradex_leistungen import daten as leistungen

from . import abgleich, daten, korrektur, uebersicht, verbindung


def _context(book: Book, query: dict) -> dict:
    people = leistungen.people(book)
    accounts = daten.accounts(book)
    st = verbindung.state(book)
    reqs = daten.requests(book)
    pending_invites = {i["person"] for i in st.get("einladungen") or []}
    unpriced = [e["id"] for e in leistungen.entries(book)
                if e["id"] in set(st.get("ohne_satz") or []) and e["abrechenbar"] and not e.get("preis")]
    active = set(st.get("aktiviert") or [])
    when = ""
    if st.get("letzter_abgleich"):
        when = datetime.fromisoformat(st["letzter_abgleich"]).strftime("%d.%m.%Y %H:%M")
    return {"personen": [{**p, "konto": accounts.get(nr), "einladung_offen": nr in pending_invites,
                          "aktiviert": nr in active}
                         for nr, p in sorted(people.items()) if p["aktiv"] or nr in accounts],
            "antraege": [r for r in reqs if r["status"] == "offen"],
            "entschieden": sorted([r for r in reqs if r["status"] != "offen"], key=lambda r: r["id"], reverse=True)[:15],
            "namen": {nr: p["name"] for nr, p in people.items()},
            "zustand": st, "zuletzt": when, "portal": verbindung.config()["url"], "ohne_satz": unpriced,
            "team": uebersicht.team(book), "pruefen": uebersicht.check_list(book),
            "abgeschlossen_bis": daten.closed_until(book), "heute": date.today(),
            "textkorrektur": daten.text_correction(book), "agent": korrektur.backend(book),
            "stunden_in_app": daten.texts(book)["stunden_in_app"],
            "leistungsart": daten.fixed_service(book),
            "leistungsarten": {nr: str(p.get("text") or nr) for nr, p in sorted(leistungen.services(book).items())}}


def _konto(book: Book, f: dict) -> dict:
    aktiv = f.get("aktiv") != "0"
    res = api.write(book, f"Team: App-Konto {f.get('person')} {'aktiv' if aktiv else 'gesperrt'}", daten.set_account,
                    f.get("person") or "", f.get("benutzer") or "", aktiv)
    try:
        abgleich.push_accounts(Book(book.root), verbindung.transport())
    except (verbindung.PortalError, BookError):
        res["meldung"] = res.get("meldung", "") + " (Portal nicht erreicht, wird beim nächsten Abgleich übertragen)"
    return res


def _einladen(book: Book, f: dict) -> dict:
    out = abgleich.invite(book, f.get("person") or "")
    return {"fragment": "code", "query": {**out, "portal": verbindung.config()["url"]},
            "meldung": "Code erstellt"}


def _code_ctx(book: Book, query: dict) -> dict:
    return {"einladung": query, "namen": {nr: p["name"] for nr, p in leistungen.people(book).items()}}


def _entscheiden(book: Book, f: dict) -> dict:
    ja = f.get("entscheid") == "bewilligen"
    res = api.write(book, f"Team: Antrag {f.get('id')} {'bewilligt' if ja else 'abgelehnt'}", daten.decide,
                    f.get("id") or "", ja, f.get("grund") or "")
    try:
        abgleich.run(Book(book.root))
    except (verbindung.PortalError, BookError):
        pass   # the decision is in the book; the app learns it with the next sync
    return res


def _sync(book: Book, f: dict) -> dict:
    try:
        out = abgleich.run(book)
    except verbindung.PortalError as exc:
        raise BookError(str(exc)) from None
    msg = f"Abgeglichen: {out['uebernommen']} übernommen, {out['hochgeladen']} Dokument(e) aktualisiert"
    if out["abgelehnt"]:
        msg += f", {len(out['abgelehnt'])} abgelehnt (die Person sieht den Grund in der App)"
    return {"meldung": msg}


def _sync_quietly(book: Book) -> str:
    try:
        abgleich.run(Book(book.root))
        return ""
    except (verbindung.PortalError, BookError):
        return " (Portal nicht erreicht, die App erfährt es beim nächsten Abgleich)"


def _ids(f: dict) -> list[str]:
    raw = f.get("ids") or []
    return [raw] if isinstance(raw, str) else list(raw)


def _geprueft(book: Book, f: dict) -> dict:
    ids = [i["id"] for i in uebersicht.check_list(book)] if f.get("alle") else _ids(f)
    if not ids:
        raise BookError("Keine Einträge ausgewählt")
    res = api.write(book, f"Team: {len(ids)} App-Eintrag/Einträge geprüft", uebersicht.mark, ids)
    return {**res, "meldung": f"{len(ids)} als geprüft markiert"}


def _eintrag_loeschen(book: Book, f: dict) -> dict:
    from aeradex_leistungen import abwesenheit
    eid = f.get("id") or ""
    fn = abwesenheit.delete if eid.startswith("A-") else leistungen.delete_entry
    res = api.write(book, f"Team: App-Eintrag {eid} gelöscht", fn, eid)
    return {**res, "meldung": f"{eid} gelöscht" + _sync_quietly(book)}


def _abschliessen(book: Book, f: dict) -> dict:
    bis = f.get("bis") or ""
    res = api.write(book, f"Team: Zeiterfassung abgeschlossen bis {bis}" if bis else "Team: Zeiterfassung wieder offen",
                    daten.close_until, bis)
    return {**res, "meldung": (f"Abgeschlossen bis {date.fromisoformat(bis):%d.%m.%Y}" if bis else "Wieder offen")
            + _sync_quietly(book)}


def _textkorrektur(book: Book, f: dict) -> dict:
    on = f.get("an") == "1"
    res = api.write(book, f"Team: Textkorrektur {'an' if on else 'aus'}", daten.set_text_correction, on)
    return {**res, "meldung": "Textkorrektur eingeschaltet" if on else "Textkorrektur ausgeschaltet"}


def _stunden(book: Book, f: dict) -> dict:
    on = f.get("an") == "1"
    res = api.write(book, f"Team: Stunden {'in der App' if on else 'auf Papier'}", daten.set_hours_in_app, on)
    return {**res, "meldung": ("Stunden werden wieder in der App erfasst" if on else
                               "Stunden kommen auf Papier: die App zeigt nur noch Abwesenheiten, Konto, Kunden und Lohn")
            + _sync_quietly(book)}


def _leistungsart(book: Book, f: dict) -> dict:
    nr = str(f.get("leistungsart") or "").strip()
    res = api.write(book, f"Team: Leistungsart {nr or 'wählbar'}", daten.set_fixed_service, nr)
    return {**res, "meldung": (f"Alle Stunden aus der App: {nr}" if nr else "Mitarbeitende wählen die Leistungsart")
            + _sync_quietly(book)}


def _pensum(book: Book, f: dict) -> dict:
    entfernen = f.get("entfernen") == "1"
    res = api.write(book, f"Leistungen: Pensum {f.get('wer')} ab {f.get('ab')}", leistungen.set_pensum,
                    f.get("wer") or "", f.get("ab") or "", f.get("wert"), entfernen)
    note = "" if entfernen else " Lohn unter Lohn › Mitarbeitende separat anpassen."
    return {**res, "meldung": ("Pensumwechsel entfernt" if entfernen else "Pensum gespeichert.") + note
            + _sync_quietly(book)}


PAGES = [Page("team", "Team-App", "team.html", _context,
              {"konto": _konto, "einladen": _einladen, "entscheiden": _entscheiden, "sync": _sync,
               "geprueft": _geprueft, "eintrag_loeschen": _eintrag_loeschen, "abschliessen": _abschliessen,
               "pensum": _pensum, "textkorrektur": _textkorrektur, "stunden": _stunden,
               "leistungsart": _leistungsart},
              bereich="lohn", fragments={"code": ("team/_code.html", _code_ctx)})]

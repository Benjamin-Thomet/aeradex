"""One sync run: accounts → take over entries → snapshot → documents. Safe to run as often as wanted."""
from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timedelta

from aeradex import api
from aeradex.book import Book, BookError

from . import abbild, daten, dokumente, krypto, uebernahme, verbindung

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_DAYS = 14


def book_head(book: Book) -> str:
    """The book's last commit (every change in aeradex is one): cheap to read, changes with every write."""
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(book.root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def waiting(send) -> int:
    return int(send("GET", "/status").get("offen") or 0)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def roster(book: Book) -> list[dict]:
    from aeradex_leistungen import daten as leistungen
    people = leistungen.people(book)
    out = []
    for nr, acc in sorted(daten.accounts(book).items()):
        p = people.get(nr)
        out.append({"person": nr, "benutzer": acc["benutzer"], "name": p["name"] if p else nr,
                    "aktiv": bool(acc["aktiv"] and p and p["aktiv"])})
    return out


def push_accounts(book: Book, send) -> dict:
    st = verbindung.state(book)
    invites = list(st.get("einladungen") or [])
    res = send("PUT", "/accounts", {"accounts": roster(book), "invites": invites})
    if invites:
        st = verbindung.state(book)
        delivered = {i["code_sha256"] for i in invites}
        st["einladungen"] = [i for i in st.get("einladungen") or [] if i["code_sha256"] not in delivered]
        verbindung.save_state(book, st)
    return res


def invite(book: Book, person: str, send=None) -> dict:
    """A one-time code for the employee (shown once, never stored in clear). Resets an existing account:
    new password, new key; documents are encrypted again once the employee has activated."""
    if person not in daten.accounts(book) or not daten.accounts(book)[person]["aktiv"]:
        api.write(book, f"Team: App-Konto für {person}", daten.set_account, person)
        book = Book(book.root)
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    expires = (date.today() + timedelta(days=CODE_DAYS)).isoformat()
    st = verbindung.state(book)
    st["einladungen"] = [i for i in st.get("einladungen") or [] if i["person"] != person] + [
        {"person": person, "code_sha256": hashlib.sha256(raw.encode()).hexdigest(), "expires": expires}]
    verbindung.save_state(book, st)
    delivered = True
    try:
        push_accounts(book, send or verbindung.transport())
    except (verbindung.PortalError, BookError):
        delivered = False
    acc = daten.accounts(book)[person]
    return {"person": person, "benutzer": acc["benutzer"], "code": code, "gueltig_bis": expires,
            "zugestellt": delivered}


def push_docs(book: Book, send, keys: list[dict]) -> dict:
    accounts = daten.accounts(book)
    keys = {k["person"]: k for k in keys if k["person"] in accounts and accounts[k["person"]]["aktiv"]}
    listing = send("GET", "/docs")
    kennung = str(listing.get("kennung") or "aeradex")     # the phones derive their keys with the portal's Kennung
    have = {(d["person"], d["doc_id"]): d for d in listing.get("docs", [])}
    up = down = 0
    keep = set()
    for person, key in sorted(keys.items()):
        for doc in dokumente.wanted(book, person):
            keep.add((person, doc.doc_id))
            cur = have.get((person, doc.doc_id))
            if cur and cur["sha256"] == doc.sha256 and cur["key_version"] == key["key_version"]:
                continue
            envelope = krypto.encrypt(key["pubkey"], person, doc.doc_id, doc.content(), kennung)
            send("PUT", f"/docs/{person}/{doc.doc_id}", {"title": doc.title, "kind": doc.kind, "period": doc.period,
                                                          "sha256": doc.sha256, "key_version": key["key_version"],
                                                          **envelope})
            up += 1
    for (person, doc_id) in have:
        if (person, doc_id) not in keep and (person not in accounts or not accounts[person]["aktiv"] or person in keys):
            send("DELETE", f"/docs/{person}/{doc_id}")
            down += 1
    return {"hochgeladen": up, "entfernt": down}


def run(book: Book, send=None) -> dict:
    """The whole sync. Errors are recorded in the local state (shown on the Team page) and raised."""
    send = send or verbindung.transport()
    st = verbindung.state(book)
    st["letzter_versuch"] = _now()
    verbindung.save_state(book, st)
    try:
        push_accounts(book, send)
        pending = send("GET", "/pending")
        acks = uebernahme.take_over(book, pending.get("entries") or [])
        if acks:
            send("POST", "/ack", {"results": [{k: v for k, v in a.items() if k != "ohne_satz"} for a in acks]})
        st = verbindung.state(book)
        st["aktiviert"] = sorted({k["person"] for k in pending.get("keys") or []})
        verbindung.save_state(book, st)
        unpriced = [a["aeradex_id"] for a in acks if a.get("ohne_satz")]
        if unpriced:
            st = verbindung.state(book)
            st["ohne_satz"] = sorted(set(st.get("ohne_satz") or []) | set(unpriced))
            verbindung.save_state(book, st)
        book = Book(book.root)
        office = abbild.office_entries(book)
        if office:   # entries the office recorded for someone: changeable in the app like their own
            api.write(book, f"Team: {len(office)} Büro-Eintrag/Einträge in der App änderbar", daten.adopt, office)
            book = Book(book.root)
        send("PUT", "/snapshot", abbild.build(book))
        docs = push_docs(book, send, pending.get("keys") or [])
    except Exception as exc:
        st = verbindung.state(book)
        st["fehler"] = str(exc).split("\n")[0][:300]
        verbindung.save_state(book, st)
        raise
    out = {"uebernommen": sum(1 for a in acks if a["status"] == "uebernommen" and not a["reason"]),
           "abgelehnt": [a for a in acks if a["status"] == "abgelehnt"], **docs}
    st = verbindung.state(book)
    st.update(letzter_abgleich=_now(), fehler="", ergebnis={k: v if not isinstance(v, list) else len(v)
                                                              for k, v in out.items()})
    verbindung.save_state(book, st)
    return out

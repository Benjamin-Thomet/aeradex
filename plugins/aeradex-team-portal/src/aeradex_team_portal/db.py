"""SQLite storage of the portal: accounts, the entry mailbox, snapshots and encrypted documents.

The portal holds no book data of its own. Entries wait here until the book takes them over; snapshots and
documents are what the PC last pushed. Every function takes an open connection; `connect` makes one per call
site (SQLite in WAL mode is fine for a handful of employees).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAIM_TIMEOUT = timedelta(minutes=10)
RETENTION = timedelta(days=90)
KINDS = ("zeit", "material", "abwesenheit")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  person TEXT PRIMARY KEY,
  benutzer TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  aktiv INTEGER NOT NULL DEFAULT 1,
  invite_sha256 TEXT,
  invite_expires TEXT,
  pw_hash TEXT,
  pubkey TEXT,
  wrapped_key TEXT,
  key_version INTEGER NOT NULL DEFAULT 0,
  session_gen INTEGER NOT NULL DEFAULT 0,
  updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
  uuid TEXT PRIMARY KEY,
  person TEXT NOT NULL,
  kind TEXT NOT NULL,
  data TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'offen',
  claimed TEXT,
  aeradex_id TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_person ON entries(person, status);
CREATE TABLE IF NOT EXISTS snapshot (
  key TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
  person TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  period TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  key_version INTEGER NOT NULL,
  envelope TEXT NOT NULL,
  updated TEXT NOT NULL,
  PRIMARY KEY (person, doc_id)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


# ---------- accounts ----------

def account(con, person: str = "", benutzer: str = "") -> dict | None:
    if benutzer:
        row = con.execute("SELECT * FROM accounts WHERE benutzer = ?", (benutzer.strip().lower(),)).fetchone()
    else:
        row = con.execute("SELECT * FROM accounts WHERE person = ?", (person,)).fetchone()
    return dict(row) if row else None


def sync_accounts(con, accounts: list[dict], invites: list[dict]) -> dict:
    """Upsert the roster from the PC; persons missing from it are deactivated. An invite resets the account."""
    seen = set()
    con.execute("BEGIN IMMEDIATE")
    try:
        for a in accounts:
            person, benutzer = str(a["person"]), str(a["benutzer"]).strip().lower()
            seen.add(person)
            clash = con.execute("SELECT person FROM accounts WHERE benutzer = ? AND person != ?",
                                (benutzer, person)).fetchone()
            if clash:
                raise ValueError(f"Benutzername {benutzer} ist schon vergeben")
            if account(con, person):
                con.execute("UPDATE accounts SET benutzer = ?, name = ?, aktiv = ?, updated = ? WHERE person = ?",
                            (benutzer, a["name"], 1 if a.get("aktiv", True) else 0, stamp(), person))
            else:
                con.execute("INSERT INTO accounts (person, benutzer, name, aktiv, updated) VALUES (?, ?, ?, ?, ?)",
                            (person, benutzer, a["name"], 1 if a.get("aktiv", True) else 0, stamp()))
            if not a.get("aktiv", True):
                con.execute("UPDATE accounts SET session_gen = session_gen + 1 WHERE person = ?", (person,))
        for person in {r["person"] for r in con.execute("SELECT person FROM accounts WHERE aktiv = 1")} - seen:
            con.execute("UPDATE accounts SET aktiv = 0, session_gen = session_gen + 1, updated = ? WHERE person = ?",
                        (stamp(), person))
        for inv in invites:
            if not account(con, inv["person"]):
                raise ValueError(f"Einladung für unbekannte Person {inv['person']}")
            con.execute("UPDATE accounts SET invite_sha256 = ?, invite_expires = ?, pw_hash = NULL, pubkey = NULL, "
                        "wrapped_key = NULL, session_gen = session_gen + 1, updated = ? WHERE person = ?",
                        (inv["code_sha256"], inv["expires"], stamp(), inv["person"]))
            con.execute("DELETE FROM docs WHERE person = ?", (inv["person"],))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"accounts": len(accounts), "invites": len(invites)}


def activate(con, person: str, pw_hash: str, pubkey: str, wrapped_key: dict) -> int:
    con.execute("UPDATE accounts SET pw_hash = ?, pubkey = ?, wrapped_key = ?, key_version = key_version + 1, "
                "invite_sha256 = NULL, invite_expires = NULL, session_gen = session_gen + 1, updated = ? "
                "WHERE person = ?", (pw_hash, pubkey, json.dumps(wrapped_key), stamp(), person))
    return account(con, person)["session_gen"]


def change_password(con, person: str, pw_hash: str, wrapped_key: dict) -> int:
    """Same key pair, re-wrapped under the new password; other sessions end."""
    con.execute("UPDATE accounts SET pw_hash = ?, wrapped_key = ?, session_gen = session_gen + 1, updated = ? "
                "WHERE person = ?", (pw_hash, json.dumps(wrapped_key), stamp(), person))
    return account(con, person)["session_gen"]


def public_keys(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT person, pubkey, key_version FROM accounts WHERE aktiv = 1 AND pubkey IS NOT NULL ORDER BY person")]


# ---------- entries ----------

def _entry(row) -> dict:
    out = dict(row)
    out["data"] = json.loads(out["data"])
    return out


def entries_of(con, person: str, since: str = "") -> list[dict]:
    q = "SELECT * FROM entries WHERE person = ?"
    args: list = [person]
    if since:
        q += " AND (status IN ('offen', 'in_uebernahme') OR updated >= ?)"
        args.append(since)
    return [_entry(r) for r in con.execute(q + " ORDER BY created", args)]


LOCKED = "Dieser Eintrag ist abgeschlossen und kann nur noch im Büro geändert werden"


def put_entry(con, person: str, uuid: str, kind: str, data: dict, booked: dict | None = None) -> dict:
    """Create or change an entry. Resending the same UUID with the same data is harmless.

    `booked` is what the office's last snapshot says about this uuid ({"version", "aenderbar"}) once it is in the
    book: a change is then allowed only while the book still marks it changeable, and goes out as a new version."""
    if kind not in KINDS:
        raise ValueError("Unbekannte Art")
    row = con.execute("SELECT * FROM entries WHERE uuid = ?", (uuid,)).fetchone()
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if row is not None and row["person"] != person:
        raise PermissionError("Fremder Eintrag")
    in_book = bool(booked) or (row is not None and (row["status"] == "uebernommen" or row["aeradex_id"]))
    if in_book and not (booked or {}).get("aenderbar"):
        raise ValueError(LOCKED)
    floor = int((booked or {}).get("version") or 0)
    if row is None:
        con.execute("INSERT INTO entries (uuid, person, kind, data, version, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (uuid, person, kind, blob, floor + 1, stamp(), stamp()))
    elif row["data"] != blob:
        if row["status"] == "in_uebernahme":
            raise ValueError("Wird gerade vom Büro übernommen, bitte in ein paar Minuten nochmals")
        # a refused entry that is corrected, or a booked one that is changed, goes back into the mailbox
        con.execute("UPDATE entries SET data = ?, kind = ?, version = ?, status = 'offen', reason = '', "
                    "updated = ? WHERE uuid = ?", (blob, kind, max(row["version"], floor) + 1, stamp(), uuid))
    return _entry(con.execute("SELECT * FROM entries WHERE uuid = ?", (uuid,)).fetchone())


def delete_entry(con, person: str, uuid: str, booked: dict | None = None) -> None:
    """Delete an entry. One that is not in the book yet just disappears; a booked one (still changeable) is sent
    to the office as a deletion, a refused change of a booked one is simply discarded."""
    row = con.execute("SELECT * FROM entries WHERE uuid = ?", (uuid,)).fetchone()
    if row is not None and row["person"] != person:
        raise PermissionError("Fremder Eintrag")
    if row is not None and row["status"] == "in_uebernahme":
        raise ValueError("Wird gerade vom Büro übernommen, bitte in ein paar Minuten nochmals")
    if row is not None and (row["status"] == "abgelehnt" or not booked and not row["aeradex_id"]
                            and row["status"] == "offen"):
        con.execute("DELETE FROM entries WHERE uuid = ?", (uuid,))
        return
    if not booked:
        if row is None:
            return
        raise ValueError(LOCKED)
    if not booked.get("aenderbar"):
        raise ValueError(LOCKED)
    data = {**(json.loads(row["data"]) if row else booked.get("data") or {}), "_loeschen": True}
    put_entry(con, person, uuid, row["kind"] if row else booked["kind"], data, booked)


def claim_pending(con) -> list[dict]:
    """Open entries (and claims that were never acknowledged) for the PC; they are locked against edits."""
    limit = stamp(now() - CLAIM_TIMEOUT)
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE entries SET status = 'offen', claimed = NULL WHERE status = 'in_uebernahme' AND claimed < ?",
                    (limit,))
        rows = [_entry(r) for r in con.execute(
            "SELECT * FROM entries WHERE status IN ('offen', 'in_uebernahme') ORDER BY created")]
        con.execute("UPDATE entries SET status = 'in_uebernahme', claimed = ? WHERE status = 'offen'", (stamp(),))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return rows


def waiting(con) -> int:
    """How many entries wait for the office (a cheap check; nothing is claimed)."""
    return con.execute("SELECT COUNT(*) FROM entries WHERE status = 'offen'").fetchone()[0]


def acknowledge(con, results: list[dict]) -> int:
    n = 0
    for r in results:
        if r["status"] not in ("uebernommen", "abgelehnt", "geloescht"):
            raise ValueError(f"Status {r['status']}")
        if r["status"] == "geloescht":
            cur = con.execute("DELETE FROM entries WHERE uuid = ? AND version = ?", (r["uuid"], int(r.get("version") or 1)))
            n += cur.rowcount
            continue
        cur = con.execute("UPDATE entries SET status = ?, aeradex_id = ?, reason = ?, claimed = NULL, updated = ? "
                          "WHERE uuid = ? AND version = ?",
                          (r["status"], r.get("aeradex_id") or "", r.get("reason") or "", stamp(), r["uuid"],
                           int(r.get("version") or 1)))
        n += cur.rowcount
    return n


def purge(con) -> int:
    cur = con.execute("DELETE FROM entries WHERE status IN ('uebernommen', 'abgelehnt') AND updated < ?",
                      (stamp(now() - RETENTION),))
    return cur.rowcount


# ---------- snapshot ----------

def put_snapshot(con, snap: dict) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM snapshot")
        con.execute("INSERT INTO snapshot VALUES ('_shared', ?, ?)",
                    (json.dumps({**snap.get("shared", {}), "stand": snap.get("stand")}, ensure_ascii=False), stamp()))
        for person, data in (snap.get("people") or {}).items():
            con.execute("INSERT INTO snapshot VALUES (?, ?, ?)", (person, json.dumps(data, ensure_ascii=False), stamp()))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (stamp(),))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def snapshot(con, key: str) -> dict:
    row = con.execute("SELECT data FROM snapshot WHERE key = ?", (key,)).fetchone()
    return json.loads(row["data"]) if row else {}


def touch_sync(con) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (stamp(),))


def last_sync(con) -> str:
    row = con.execute("SELECT value FROM meta WHERE key = 'last_sync'").fetchone()
    return row["value"] if row else ""


# ---------- documents ----------

def doc_index(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT person, doc_id, sha256, key_version FROM docs ORDER BY person, doc_id")]


def put_doc(con, person: str, doc_id: str, meta: dict) -> None:
    acc = account(con, person)
    if not acc or not acc["pubkey"]:
        raise ValueError(f"{person} hat noch keinen Schlüssel")
    if int(meta["key_version"]) != acc["key_version"]:
        raise ValueError("Veralteter Schlüssel")
    envelope = json.dumps({k: meta[k] for k in ("epk", "iv", "ct")})
    con.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (person, doc_id, meta["title"], meta.get("kind") or "", meta.get("period") or "", meta["sha256"],
                 int(meta["key_version"]), envelope, stamp()))


def delete_doc(con, person: str, doc_id: str) -> None:
    con.execute("DELETE FROM docs WHERE person = ? AND doc_id = ?", (person, doc_id))


def docs_of(con, person: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT doc_id, title, kind, period, updated FROM docs WHERE person = ? ORDER BY period DESC, title",
        (person,))]


def doc(con, person: str, doc_id: str) -> dict | None:
    row = con.execute("SELECT * FROM docs WHERE person = ? AND doc_id = ?", (person, doc_id)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["envelope"] = json.loads(out["envelope"])
    return out

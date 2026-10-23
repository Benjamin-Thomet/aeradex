"""Customers as a small CRM: the dossier of one customer (invoices, revenue, payment behaviour, timeline), the
contact log with follow-ups (Wiedervorlagen) and the parts plugins add (objects, quotes, open work).

The log lives in the customer's frontmatter (`log:` list); the Markdown body stays the «Feste Infos»."""
from __future__ import annotations

import copy
import os
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import gitlog, invoices
from .book import Book, BookError
from .files import parse_date, read_frontmatter, write_frontmatter

ZERO = Decimal("0")
LOG_ARTEN = {"anruf": "Anruf", "besuch": "Besuch", "email": "E-Mail", "notiz": "Notiz"}
KATEGORIEN = ("Privat", "Verwaltung", "Architekt", "Firma", "Öffentlich")


# ---------- revenue ----------

def customer_of_row(row, metas: dict) -> str | None:
    """The customer a journal row belongs to, via the invoice number in its Quelle."""
    nr = invoices.invoice_nr_in(row.quelle, metas)
    return metas[nr].get("kunde") if nr else None


def revenue_by_year(book: Book, metas: dict | None = None) -> dict[str, dict[int, Decimal]]:
    """Revenue (Ertrag accounts, net of discounts and credit notes) per customer and year — the same rule as the
    report «Umsatz nach Kunde». Akontorechnungen book on Anzahlungen and count only with the Schlussrechnung."""
    metas = invoices.invoices(book) if metas is None else metas
    out: dict[str, dict[int, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in book.rows:
        if not invoices.invoice_nr_in(r.quelle, metas):
            continue
        for nr, sign in ((r.soll, -1), (r.haben, 1)):
            acct = book.accounts.get(nr) if nr else None
            if acct and acct.klasse == "ertrag":
                kunde = customer_of_row(r, metas)
                if kunde:
                    out[kunde][r.datum.year] += sign * r.betrag
    return out


# ---------- the log ----------

def _who() -> str:
    """Who writes a log entry: the person (or agent) the commit is attributed to."""
    author = gitlog.AUTHOR.get() or os.environ.get("AERADEX_AUTHOR") or ""
    name = author.split("<", 1)[0].strip()
    return name.replace("aeradex Agent", "Agent").strip()


def _git_user(root: Path) -> str:
    try:
        return gitlog._git(root, "config", "user.name", check=False).stdout.strip()
    except OSError:
        return ""


def log_entries(cust: dict) -> list[dict]:
    """The log with its index (the position in the file, used to mark an entry done)."""
    out = []
    for i, e in enumerate(cust.get("log") or []):
        if isinstance(e, dict):
            out.append({**e, "index": i, "datum": str(e.get("datum") or ""), "zeit": str(e.get("zeit") or ""),
                        "nachfassen": str(e.get("nachfassen") or ""), "erledigt": str(e.get("erledigt") or ""),
                        "art_label": LOG_ARTEN.get(str(e.get("art")), str(e.get("art") or ""))})
    return out


def _load(book: Book, nr: str) -> tuple[Path, dict, str]:
    path = invoices.customer(book, nr)["_pfad"]
    meta, body = read_frontmatter(path)
    return path, copy.deepcopy(meta), body


def log_add(book: Book, nr: str, text: str, art: str = "notiz", wer: str = "", datum=None,
            nachfassen=None) -> tuple[dict, Path]:
    text = (text or "").strip()
    if not text:
        raise BookError("Log-Eintrag: Text fehlt")
    art = (art or "notiz").strip().lower()
    if art not in LOG_ARTEN:
        raise BookError(f"Art: {', '.join(LOG_ARTEN)}")
    path, meta, body = _load(book, nr)
    now = datetime.now()
    d = parse_date(datum, "datum") if datum else now.date()
    entry = {"datum": d.isoformat(), "zeit": now.strftime("%H:%M") if d == now.date() else "",
             "wer": (wer or _who() or _git_user(book.root)).strip(), "art": art, "text": text}
    if not entry["zeit"]:
        del entry["zeit"]
    if nachfassen:
        entry["nachfassen"] = parse_date(nachfassen, "nachfassen").isoformat()
    meta["log"] = list(meta.get("log") or []) + [entry]
    write_frontmatter(path, meta, body)
    return entry, path


def log_done(book: Book, nr: str, index: int, erledigt: bool = True) -> tuple[dict, Path]:
    path, meta, body = _load(book, nr)
    log = list(meta.get("log") or [])
    if not 0 <= int(index) < len(log):
        raise BookError(f"Log-Eintrag {index} bei {nr} nicht gefunden")
    entry = dict(log[int(index)])
    if erledigt:
        entry["erledigt"] = date.today().isoformat()
    else:
        entry.pop("erledigt", None)
    log[int(index)] = entry
    meta["log"] = log
    write_frontmatter(path, meta, body)
    return entry, path


def wiedervorlagen(book: Book, bis: date | None = None, kunde: str | None = None) -> list[dict]:
    """Follow-ups not yet done; with `bis` only those due by then. Oldest first."""
    out = []
    for nr, c in invoices.customers(book).items():
        if kunde and nr != kunde:
            continue
        for e in log_entries(c):
            if e["nachfassen"] and not e["erledigt"] and (bis is None or e["nachfassen"] <= bis.isoformat()):
                out.append({**e, "kunde": nr, "name": c.get("firma") or c.get("name") or nr})
    return sorted(out, key=lambda e: (e["nachfassen"], e["kunde"]))


def check_log(book: Book) -> list[tuple[str, str]]:
    """(where, problem) for log entries a person cannot have written through aeradex."""
    out = []
    for nr, c in invoices.customers(book).items():
        log = c.get("log")
        if log is None:
            continue
        if not isinstance(log, list):
            out.append((nr, "log muss eine Liste sein"))
            continue
        for i, e in enumerate(log, 1):
            where = f"{nr} log #{i}"
            if not isinstance(e, dict) or not str(e.get("text") or "").strip():
                out.append((where, "Eintrag ohne Text"))
                continue
            if str(e.get("art") or "notiz") not in LOG_ARTEN:
                out.append((where, f"unbekannte Art «{e.get('art')}» ({', '.join(LOG_ARTEN)})"))
            for key in ("datum", "nachfassen", "erledigt"):
                if e.get(key) in (None, "") and key != "datum":
                    continue
                try:
                    parse_date(e.get(key), key)
                except (BookError, ValueError, TypeError):
                    out.append((where, f"{key} ist kein Datum: {e.get(key)!r}"))
    return out


# ---------- payment behaviour ----------

def zahlungsmoral(states: list[dict], paid: dict, reminders: dict[str, list]) -> dict:
    """How punctually a customer pays: days from the due date to the last payment of each paid invoice."""
    tage = []
    for st in states:
        if st["status"] != "bezahlt":
            continue
        pays = [r.datum for r in paid.get(st["nummer"], []) if r.quelle.startswith("zahlung:")]
        if pays:
            tage.append((max(pays) - parse_date(st["faellig"], "faellig")).days)
    n_mahn = sum(len(reminders.get(st["nummer"], [])) for st in states)
    stufe = max((int(m.get("stufe") or 0) for st in states for m in reminders.get(st["nummer"], [])), default=0)
    avg = (Decimal(sum(tage)) / len(tage)).quantize(Decimal("0.1"), ROUND_HALF_UP) if tage else None
    punct = (Decimal(sum(1 for t in tage if t <= 0)) * 100 / len(tage)).quantize(Decimal("1"), ROUND_HALF_UP) if tage else None
    if len(tage) < 2:
        ampel, label = None, "zu wenig Daten"
    elif avg > 30 or stufe >= 2:
        ampel, label = "red", "zahlt schlecht"
    elif avg <= 5 and not n_mahn:
        ampel, label = "ok", "zahlt pünktlich"
    else:
        ampel, label = "warn", "zahlt verspätet"
    return {"bezahlte": len(tage), "tage_schnitt": avg, "puenktlich_prozent": punct, "mahnungen": n_mahn,
            "hoechste_stufe": stufe, "ampel": ampel, "label": label}


# ---------- the dossier ----------

def _kind(meta: dict) -> str:
    if invoices.is_akonto(meta):
        return "Akontorechnung"
    return "Schlussrechnung" if meta.get("art") == "schluss" else "Rechnung"


def dossier(book: Book, nr: str, heute: date | None = None) -> dict:
    """Everything about one customer for the detail page, the agent and the CLI."""
    from . import mahnungen, plugins
    heute = heute or date.today()
    cust = invoices.customer(book, nr)
    metas = invoices.invoices(book)
    mine = {k: m for k, m in metas.items() if m.get("kunde") == nr}
    paid = invoices.settlements(book)
    credited_in = invoices.angerechnet(book, metas)
    reminders = {k: mahnungen.history(book, k) for k in mine}
    rechnungen = []
    for k, m in sorted(mine.items(), key=lambda kv: (str(kv[1].get("datum")), kv[0]), reverse=True):
        st = invoices.invoice_state(book, m, paid.get(k, []))
        rem = reminders.get(k) or []
        rechnungen.append({**st, "art_label": _kind(m), "angerechnet_in": credited_in.get(k, ""),
                           "abgezogen": sorted({p["akonto"] for p in m.get("positionen") or [] if p.get("akonto")}),
                           "mahnstufe": max((int(x.get("stufe") or 0) for x in rem), default=0),
                           "ueberfaellig": st["status"] in ("offen", "teilbezahlt") and st["faellig"] < heute.isoformat()})
    active = [r for r in rechnungen if r["status"] != "storniert"]
    umsatz = revenue_by_year(book, metas).get(nr, {})
    kennzahlen = {
        "umsatz_jahr": umsatz.get(heute.year, ZERO), "umsatz_vorjahr": umsatz.get(heute.year - 1, ZERO),
        "umsatz_total": sum(umsatz.values(), ZERO), "jahr": heute.year,
        "offen": sum((r["offen_chf"] for r in active), ZERO),
        "ueberfaellig": sum((r["offen_chf"] for r in active if r["ueberfaellig"]), ZERO),
        "anzahl": len(active), "letzte": active[0]["datum"] if active else "",
        "kunde_seit": min((r["datum"] for r in active), default=""),
    }
    akonto = invoices.open_akonto(book, nr)
    kennzahlen["akonto_offen"] = sum((a["total"] for a in akonto), ZERO)
    log = log_entries(cust)
    zeit = [{"datum": e["datum"], "zeit": e["zeit"], "art": e["art"], "titel": e["art_label"], "text": e["text"],
             "wer": e.get("wer") or "", "nachfassen": e["nachfassen"], "erledigt": e["erledigt"], "log": e["index"]}
            for e in log]
    for r in rechnungen:
        zeit.append({"datum": r["datum"], "art": "rechnung", "titel": f"{r['art_label']} {r['nummer']}",
                     "text": f"{r['waehrung']} {r['total']:,.2f}".replace(",", "'")
                     + (" · storniert" if r["status"] == "storniert" else ""), "link": f"/debitoren/rechnung/{r['nummer']}"})
        for p in paid.get(r["nummer"], []):
            kind = "Zahlung" if p.quelle.startswith("zahlung:") else "Gutschrift"
            zeit.append({"datum": p.datum.isoformat(), "art": kind.lower(), "titel": f"{kind} {r['nummer']}",
                         "text": f"CHF {p.betrag:,.2f}".replace(",", "'"), "link": f"/debitoren/rechnung/{r['nummer']}"})
        for m in reminders.get(r["nummer"]) or []:
            zeit.append({"datum": str(m.get("datum")), "art": "mahnung", "titel": f"{m.get('bezeichnung') or 'Mahnung'} {r['nummer']}",
                         "text": f"offen CHF {Decimal(str(m.get('offen') or 0)):,.2f}".replace(",", "'"),
                         "link": f"/debitoren/rechnung/{r['nummer']}"})
    teile = plugins.kunde_teile(book, nr)
    for _plugin, teil in teile:
        zeit += [dict(e) for e in teil.zeitachse]
    zeit.sort(key=lambda e: (str(e.get("datum") or ""), str(e.get("zeit") or "")), reverse=True)
    return {"kunde": cust, "nummer": nr, "rechnungen": rechnungen, "akonto_offen": akonto,
            "kennzahlen": kennzahlen, "zahlungsmoral": zahlungsmoral(active, paid, reminders),
            "log": log, "wiedervorlagen": [e for e in log if e["nachfassen"] and not e["erledigt"]],
            "zeitachse": zeit, "teile": teile}

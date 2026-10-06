"""Optional: account suggestions for bank transactions with TypeSafe's Jev.

Jev is a "System One" model: it does not write text, it answers typed
questions (here: a Choice among the chart of accounts) with calibrated
probabilities and a confidence. That fits allkvitt's rule exactly — code does
the arithmetic, the model makes one narrow decision, and the confidence
decides whether a person has to look:

    confidence ≥ threshold  →  a proposal (Buchhaltung › Vorschläge) (never booked directly)
    below                   →  only a hint on the bank transaction

The same applies to supplier bill drafts (Kreditoren upload): Jev picks the
expense account; below the threshold the draft goes to the agent.

Off by default. Enable per book (allkvitt.yaml → jev: {aktiv: true}) and set
TYPESAFE_API_KEY in the environment. Sent per transaction: counterparty,
payment message, amount, direction, the account list and up to five earlier
bookings with the same counterparty. Transactions with an employee as
counterparty are never sent.

API: https://docs.typesafe.ai/api (POST /v1/systemone).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from .book import Book, BookError
from .files import parse_amount

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULTS = {"aktiv": False, "schwelle": 0.7, "modell": "jev-latest"}


def config(book: Book) -> dict:
    cfg = {**DEFAULTS, **(book.settings.get("jev") or {})}
    cfg["schwelle"] = float(cfg["schwelle"])
    cfg["schluessel"] = bool(os.environ.get("TYPESAFE_API_KEY"))
    cfg["bereit"] = bool(cfg["aktiv"]) and cfg["schluessel"]
    return cfg


# ---------- transport ----------

def _post(payload: dict, key: str, timeout: float = 30) -> dict:
    request = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def system_one(state, questions: dict, model: str) -> dict:
    """One evaluation call, retried with backoff on 429/529 as TypeSafe recommends."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise BookError("TYPESAFE_API_KEY ist nicht gesetzt")
    payload = {"state": state, "model": model, "questions": questions}
    for attempt in range(4):
        try:
            return _post(payload, key)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 529) and attempt < 3:
                time.sleep(1.5 * 2 ** attempt)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 401:
                raise BookError("TypeSafe lehnt den API-Schlüssel ab (401)") from None
            raise BookError(f"TypeSafe-Fehler {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if attempt < 3:
                time.sleep(1.5 * 2 ** attempt)
                continue
            raise BookError(f"TypeSafe nicht erreichbar: {exc.reason}") from None
    raise BookError("TypeSafe überlastet — später erneut versuchen")


# ---------- bank account suggestions ----------

def candidate_accounts(book: Book, credit: bool, bank_account: str) -> dict[str, str]:
    """Options for the Choice: P&L accounts of the right side plus balance-sheet accounts
    (transfers, loans, VAT payments …), never the bank account itself. At most 255."""
    from .statements import GROUP_LABEL
    side = "ertrag" if credit else "aufwand"
    used = {r.soll for r in book.rows} | {r.haben for r in book.rows}
    out = {}
    for nr, a in sorted(book.accounts.items()):
        if not a.aktiv_ or nr == bank_account:
            continue
        if a.klasse == side or a.is_balance_sheet or (a.is_pl and nr in used):
            out[nr] = f"{a.name} ({GROUP_LABEL.get(a.gruppe, a.gruppe)})"
    if len(out) > 255:   # keep P&L of the right side and accounts already in use
        out = {k: v for k, v in out.items() if book.accounts[k].klasse == side or k in used}
        out = dict(list(out.items())[:255])
    return out


def history(book: Book, counterparty: str, limit: int = 5) -> list[dict]:
    """Earlier bookings with the same counterparty — the strongest hint there is."""
    from . import bank
    name = (counterparty or "").strip().lower()
    if len(name) < 3:
        return []
    by_beleg = {}
    for r in book.rows:
        by_beleg.setdefault(r.beleg, []).append(r)
    out = []
    for t in reversed(bank.transactions(book)):
        if t.get("Status") == "gebucht" and t.get("Gegenpartei", "").strip().lower() == name and t.get("Beleg") in by_beleg:
            for r in by_beleg[t["Beleg"]]:
                counter = r.haben if r.soll == t["Konto"] else r.soll
                if counter and counter != t["Konto"]:
                    out.append({"text": r.text, "konto": counter,
                                "kontoname": book.accounts[counter].name if counter in book.accounts else "",
                                "betrag": f"{r.betrag:.2f}"})
                    break
        if len(out) >= limit:
            break
    if len(out) < limit:
        for r in reversed(book.rows):
            if name in r.text.lower() and r.soll and r.haben and not r.quelle:
                counter = r.soll if r.haben in ("1000", "1020") else r.haben
                out.append({"text": r.text, "konto": counter,
                            "kontoname": book.accounts[counter].name if counter in book.accounts else "",
                            "betrag": f"{r.betrag:.2f}"})
            if len(out) >= limit:
                break
    return out[:limit]


def _employee_names(book: Book) -> set[str]:
    from . import payroll
    names = set()
    for e in payroll.employees(book).values():
        names.add(f"{e.get('vorname', '')} {e.get('nachname', '')}".strip().lower())
        names.add(f"{e.get('nachname', '')} {e.get('vorname', '')}".strip().lower())
    return {n for n in names if n}


def suggest_one(book: Book, tx: dict, cfg: dict) -> dict:
    amount = parse_amount(tx["Betrag"])
    credit = amount > 0
    options = candidate_accounts(book, credit, tx["Konto"])
    state = {
        "firma": book.settings.firma,
        "bewegung": {"datum": tx["Datum"], "betrag_chf": f"{abs(amount):.2f}",
                     "richtung": "Gutschrift: Geld kommt auf das Bankkonto" if credit else
                                 "Belastung: Geld geht vom Bankkonto weg",
                     "gegenpartei": tx.get("Gegenpartei", ""), "mitteilung": tx.get("Text", ""),
                     "referenz": tx.get("Referenz", "")},
        "fruehere_buchungen_gleiche_gegenpartei": history(book, tx.get("Gegenpartei", "")),
    }
    questions = {"gegenkonto": {
        "type": "choice",
        "instructions": ("Diese `bewegung` steht auf dem Bankkonto einer Schweizer KMU. Auf welches Gegenkonto "
                         "aus dem Kontenplan gehört sie? Wenn `fruehere_buchungen_gleiche_gegenpartei` Einträge "
                         "hat, ist dort das bisher verwendete Konto der stärkste Hinweis."),
        "criteria": options,
    }}
    answer = system_one(state, questions, cfg["modell"])["answers"]["gegenkonto"]
    probs = sorted(((k, float(v)) for k, v in (answer.get("probabilities") or {}).items()), key=lambda kv: -kv[1])
    return {"id": tx["ID"], "konto": answer.get("choice"), "konfidenz": float(answer.get("confidence") or 0),
            "alternativen": probs[1:4], "wahrscheinlichkeit": probs[0][1] if probs else 0.0}


def bill_history(book: Book, name: str, limit: int = 5) -> list[dict]:
    """Accounts earlier bills of the same supplier went to."""
    from . import kreditoren as kred
    key = (name or "").strip().lower()
    if len(key) < 3:
        return []
    out = []
    for meta in reversed(list(kred.bills(book).values())):
        if str(meta.get("name", "")).strip().lower() == key and meta.get("konto"):
            konto = str(meta["konto"])
            out.append({"rechnungsnr": meta.get("rechnungsnr", ""), "betrag": f"{meta.get('betrag')}", "konto": konto,
                        "kontoname": book.accounts[konto].name if konto in book.accounts else ""})
        if len(out) >= limit:
            break
    return out


def suggest_bill(book: Book, meta: dict, text: str, cfg: dict) -> dict:
    """Expense account for a supplier bill draft. Sent: supplier name, amount, invoice number,
    VAT rate, the first 1500 characters of the bill's text, the chart and earlier bills of that supplier."""
    from . import erfassung
    options = {k: v for k, v in candidate_accounts(book, False, "").items()
               if book.accounts[k].klasse in ("aufwand", "aktiv")}
    name = erfassung.value(meta, "name")
    state = {
        "firma": book.settings.firma,
        "rechnung": {"lieferant": name, "betrag_chf": erfassung.value(meta, "betrag"),
                     "rechnungsnr": erfassung.value(meta, "rechnungsnr"),
                     "mwst_satz": erfassung.value(meta, "mwst_satz"), "text": (text or "")[:1500]},
        "fruehere_rechnungen_gleicher_lieferant": bill_history(book, name),
    }
    questions = {"aufwandkonto": {
        "type": "choice",
        "instructions": ("Diese `rechnung` ist eine Lieferantenrechnung an eine Schweizer KMU. Auf welches Konto "
                         "gehört der Aufwand (bei Anschaffungen von Anlagen ein Aktivkonto)? Wenn "
                         "`fruehere_rechnungen_gleicher_lieferant` Einträge hat, ist das bisher verwendete Konto "
                         "der stärkste Hinweis."),
        "criteria": options,
    }}
    answer = system_one(state, questions, cfg["modell"])["answers"]["aufwandkonto"]
    probs = sorted(((k, float(v)) for k, v in (answer.get("probabilities") or {}).items()), key=lambda kv: -kv[1])
    return {"konto": answer.get("choice"), "konfidenz": float(answer.get("confidence") or 0),
            "alternativen": probs[1:4]}


def suggest_bank(book: Book, ids: list[str] | None = None, schwelle: float | None = None) -> dict:
    """Suggest counter accounts for open bank transactions; propose the confident ones."""
    from . import api, bank
    cfg = config(book)
    if not cfg["aktiv"]:
        raise BookError("Jev ist für dieses Buch nicht eingeschaltet (Einstellungen → Jev)")
    if not cfg["schluessel"]:
        raise BookError("TYPESAFE_API_KEY ist nicht gesetzt")
    threshold = cfg["schwelle"] if schwelle is None else float(schwelle)
    employees = _employee_names(book)
    proposed_banks = {p.get("Bank") for p in api.proposals(book)}
    todo = [t for t in bank.transactions(book) if t["Status"] == "offen" and (not ids or t["ID"] in ids)
            and t["ID"] not in proposed_banks]
    skipped = [t["ID"] for t in todo if t.get("Gegenpartei", "").strip().lower() in employees]
    todo = [t for t in todo if t["ID"] not in skipped]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda t: _safe(suggest_one, book, t, cfg), todo))
    summary = {"vorgeschlagen": [], "unsicher": [], "fehler": [], "uebersprungen": skipped}
    hints = {}
    for tx, res in zip(todo, results):
        if "fehler" in res:
            summary["fehler"].append({"id": tx["ID"], "fehler": res["fehler"]})
            continue
        konto = res["konto"]
        name = book.accounts[konto].name if konto in book.accounts else konto
        alts = ", ".join(f"{k} {p:.0%}" for k, p in res["alternativen"] if p >= 0.01)
        reason = f"Jev (Konfidenz {res['konfidenz']:.2f}): {konto} {name}" + (f" · Alternativen: {alts}" if alts else "")
        if res["konfidenz"] >= threshold:
            amount = parse_amount(tx["Betrag"])
            soll, haben = (tx["Konto"], konto) if amount > 0 else (konto, tx["Konto"])
            text = " ".join(t for t in (tx.get("Gegenpartei"), tx.get("Text")) if t)[:120] or "Bankbewegung"
            out = api.propose(book, tx["Datum"], soll, haben, str(abs(amount)), text, reason, "", "", tx["ID"])
            book.reload()
            summary["vorgeschlagen"].append({"id": tx["ID"], "konto": konto, "konfidenz": res["konfidenz"],
                                             "vorschlag": out.get("vorschlag", {}).get("ID")})
        else:
            hints[tx["ID"]] = f"Jev unsicher ({res['konfidenz']:.2f}): {konto}" + (f" · {alts}" if alts else "")
            summary["unsicher"].append({"id": tx["ID"], "konto": konto, "konfidenz": res["konfidenz"]})
    touched = []
    for tid, hint in hints.items():
        touched += bank._set(book, tid, Hinweis=hint)
    return {**summary, "_touched": touched}


def _safe(fn, *args):
    try:
        return fn(*args)
    except BookError as exc:
        return {"fehler": str(exc)}
    except Exception as exc:  # network etc. — reported per transaction, never fatal
        return {"fehler": f"{type(exc).__name__}: {exc}"}

"""Optional: account suggestions for bank transactions with TypeSafe's Jev.

Jev is a "System One" model: it does not write text, it answers typed
questions (here: a Choice among the chart of accounts) with calibrated
probabilities and a confidence. That fits aeradex's rule exactly — code does
the arithmetic, the model makes one narrow decision, and the confidence
decides whether a person has to look:

    confidence ≥ threshold  →  a proposal (Buchhaltung › Vorschläge) (never booked directly)
    below                   →  only a hint on the bank transaction

The same applies to supplier bill drafts (Kreditoren upload): Jev picks the
expense account and, among the amounts and dates on the document, the total and
the invoice date, and the Vorsteuer code; below the threshold the draft goes to the agent.

Off by default. Enable per book (aeradex.yaml → jev: {aktiv: true}) and set
TYPESAFE_API_KEY in the environment or save it under Einstellungen (it goes to
<config>/typesafe.env, chmod 600 — never into the book, which is a shared git repo). Sent per transaction: counterparty,
payment message, amount, direction, the account list and up to five earlier
bookings with the same counterparty. Transactions with an employee as
counterparty are never sent.

API: https://docs.typesafe.ai/api (POST /v1/systemone).
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from .book import Book, BookError
from .files import parse_amount

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULTS = {"aktiv": False, "schwelle": 0.7, "modell": "jev-latest"}


ENV_KEY = "TYPESAFE_API_KEY"


def key_file() -> Path:
    """Where the UI stores the key: the same config directory as the server's users (web/auth.py)."""
    return Path(os.environ.get("AERADEX_CONFIG") or Path.home() / ".config" / "aeradex") / "typesafe.env"


def _read_key_file() -> str:
    path = key_file()
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() == ENV_KEY:
            return value.strip().strip("'\"")
    return ""


def api_key() -> tuple[str, str]:
    """(key, source): the environment wins over the key file; ("", "") if neither has one."""
    if os.environ.get(ENV_KEY):
        return os.environ[ENV_KEY], "Umgebung"
    key = _read_key_file()
    return (key, "Datei") if key else ("", "")


def _write_key_file(value: str | None) -> None:
    path = key_file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if ln.strip().removeprefix("export ").partition("=")[0].strip() != ENV_KEY]
    if value:
        lines.append(f"{ENV_KEY}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def save_key(value: str) -> None:
    value = (value or "").strip()
    if not value:
        raise BookError("Bitte einen API-Schlüssel eingeben")
    if any(c.isspace() for c in value) or "=" in value:
        raise BookError("Der API-Schlüssel darf keine Leerzeichen oder = enthalten")
    _write_key_file(value)


def delete_key() -> None:
    _write_key_file(None)


def config(book: Book) -> dict:
    cfg = {**DEFAULTS, **(book.settings.get("jev") or {})}
    cfg["schwelle"] = float(cfg["schwelle"])
    key, source = api_key()
    cfg["schluessel"] = bool(key)
    cfg["schluessel_quelle"] = source
    cfg["schluessel_ende"] = key[-4:] if len(key) >= 12 else ""
    cfg["schluessel_datei"] = bool(_read_key_file())
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
    key = api_key()[0]
    if not key:
        raise BookError("Kein TypeSafe-API-Schlüssel (Einstellungen → Agent & KI)")
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
        if t.get("Status") == "gebucht" and bank.party(t).lower() == name and t.get("Beleg") in by_beleg:
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
    from . import bank
    amount = parse_amount(tx["Betrag"])
    who = bank.party(tx)
    credit = amount > 0
    options = candidate_accounts(book, credit, tx["Konto"])
    state = {
        "firma": book.settings.firma,
        "bewegung": {"datum": tx["Datum"], "betrag_chf": f"{abs(amount):.2f}",
                     "richtung": "Gutschrift: Geld kommt auf das Bankkonto" if credit else
                                 "Belastung: Geld geht vom Bankkonto weg",
                     "gegenpartei": who, "mitteilung": tx.get("Text", ""),
                     "referenz": tx.get("Referenz", "")},
        "fruehere_buchungen_gleiche_gegenpartei": history(book, who),
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


def bill_candidates(text: str, limit: int = 40) -> tuple[dict[str, str], dict[str, str]]:
    """Every amount and every date on the document, each with the line it stands on — the options Jev chooses
    from (it never writes a value itself, so it can only pick what is really on the bill)."""
    from . import erfassung
    found_amounts: dict[str, str] = {}
    found_dates: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        for a in erfassung.amounts(re.sub(erfassung._DATE, " ", line, flags=re.I)):   # not «03.10» of a date
            if a > 0:
                found_amounts.setdefault(f"{a:.2f}", f"{a:.2f} · Zeile «{line[:90]}»")
        for d in erfassung.dates(line):
            found_dates.setdefault(d.isoformat(), f"{d:%d.%m.%Y} · Zeile «{line[:90]}»")
    return dict(list(found_amounts.items())[:limit]), dict(list(found_dates.items())[:limit])


def vat_options(book: Book) -> dict[str, str]:
    """Vorsteuer codes of the current rates plus «no VAT shown», for books that account VAT effectively."""
    from . import mwst
    if mwst.config(book)["methode"] != "effektiv":
        return {}
    out = {c.code: c.label for c in mwst.CODES.values() if c.kind in ("vorsteuer", "investition")}
    out["keine"] = "Keine MWST ausgewiesen (nicht MWST-pflichtiger Lieferant, steuerfrei)"
    return out


def read_bill(book: Book, meta: dict, text: str, cfg: dict, ask: set[str]) -> dict[str, dict]:
    """One call, several narrow choices on a supplier bill or receipt: `konto` (expense account), `betrag` (which
    of the amounts is the invoice total), `datum` (which date is the invoice date), `mwst` (Vorsteuer code).
    Sent: supplier name, amount, invoice number, VAT rate, the first 3000 characters of the text, the chart and
    earlier bills of that supplier. → {name: {wert, konfidenz, alternativen}} for the questions answered."""
    from . import erfassung
    name = erfassung.value(meta, "name")
    found_amounts, found_dates = bill_candidates(text)
    questions: dict[str, dict] = {}
    if "konto" in ask:
        options = {k: v for k, v in candidate_accounts(book, False, "").items()
                   if book.accounts[k].klasse in ("aufwand", "aktiv")}
        questions["aufwandkonto"] = {
            "type": "choice",
            "instructions": ("Diese `rechnung` ist eine Lieferantenrechnung oder Quittung an eine Schweizer KMU. Auf "
                             "welches Konto gehört der Aufwand (bei Anschaffungen von Anlagen ein Aktivkonto)? Wenn "
                             "`fruehere_rechnungen_gleicher_lieferant` Einträge hat, ist das bisher verwendete Konto "
                             "der stärkste Hinweis."),
            "criteria": options}
    if "betrag" in ask and found_amounts:
        questions["rechnungsbetrag"] = {
            "type": "choice",
            "instructions": ("Welcher dieser Beträge aus dem `text` ist der Rechnungsbetrag, also das Total, das zu "
                             "bezahlen ist bzw. bezahlt wurde (inklusive MWST)? Nicht eine Position, ein Zwischentotal, "
                             "der MWST-Betrag oder ein Rabatt."),
            "criteria": found_amounts}
    if "datum" in ask and found_dates:
        questions["rechnungsdatum"] = {
            "type": "choice",
            "instructions": ("Welches dieser Daten aus dem `text` ist das Rechnungs- bzw. Belegdatum? Nicht das "
                             "Fälligkeitsdatum, das Lieferdatum oder ein Leistungszeitraum."),
            "criteria": found_dates}
    vat = vat_options(book) if "mwst" in ask else {}
    if vat:
        questions["mwst_code"] = {
            "type": "choice",
            "instructions": ("Welcher Vorsteuer-Code passt zu diesem Beleg? «V…» für Material, Waren und "
                             "Dienstleistungen, «I…» für Investitionen und übrigen Betriebsaufwand, jeweils mit dem Satz, "
                             "den der Beleg ausweist; «keine», wenn keine MWST ausgewiesen ist."),
            "criteria": vat}
    if not questions:
        return {}
    state = {
        "firma": book.settings.firma,
        "rechnung": {"lieferant": name, "betrag_chf": erfassung.value(meta, "betrag"),
                     "rechnungsnr": erfassung.value(meta, "rechnungsnr"),
                     "mwst_satz": erfassung.value(meta, "mwst_satz"), "text": (text or "")[:3000]},
        "fruehere_rechnungen_gleicher_lieferant": bill_history(book, name),
    }
    answers = system_one(state, questions, cfg["modell"]).get("answers") or {}
    out = {}
    for ours, theirs in (("konto", "aufwandkonto"), ("betrag", "rechnungsbetrag"), ("datum", "rechnungsdatum"),
                         ("mwst", "mwst_code")):
        a = answers.get(theirs)
        if theirs in questions and a and a.get("choice") in questions[theirs]["criteria"]:
            probs = sorted(((k, float(v)) for k, v in (a.get("probabilities") or {}).items()), key=lambda kv: -kv[1])
            out[ours] = {"wert": a["choice"], "konfidenz": float(a.get("confidence") or 0), "alternativen": probs[1:4]}
    return out


def suggest_bill(book: Book, meta: dict, text: str, cfg: dict) -> dict:
    """Expense account for a supplier bill draft (the account question of read_bill alone)."""
    res = read_bill(book, meta, text, cfg, {"konto"}).get("konto")
    if not res:
        raise BookError("Jev hat kein Konto gewählt")
    return {"konto": res["wert"], "konfidenz": res["konfidenz"], "alternativen": res["alternativen"]}


def suggest_bank(book: Book, ids: list[str] | None = None, schwelle: float | None = None) -> dict:
    """Suggest counter accounts for open bank transactions; propose the confident ones."""
    from . import api, bank
    cfg = config(book)
    if not cfg["aktiv"]:
        raise BookError("Jev ist für dieses Buch nicht eingeschaltet (Einstellungen → Agent & KI)")
    if not cfg["schluessel"]:
        raise BookError("Kein TypeSafe-API-Schlüssel (Einstellungen → Agent & KI)")
    threshold = cfg["schwelle"] if schwelle is None else float(schwelle)
    employees = _employee_names(book)
    proposed_banks = {p.get("Bank") for p in api.proposals(book)}
    todo = [t for t in bank.transactions(book) if t["Status"] == "offen" and (not ids or t["ID"] in ids)
            and t["ID"] not in proposed_banks]
    skipped = [t["ID"] for t in todo if bank.party(t).lower() in employees]
    todo = [t for t in todo if t["ID"] not in skipped]
    # Jev only gets what nothing else explains: a movement with an open invoice/bill, a receipt, a rule or a
    # document waiting in the Eingang is booked from that document — a proposal would book it twice.
    waiting = _explained(book, todo)
    todo = [t for t in todo if t["ID"] not in waiting]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda t: _safe(suggest_one, book, t, cfg), todo))
    summary = {"vorgeschlagen": [], "unsicher": [], "fehler": [], "uebersprungen": skipped,
               "zugeordnet": waiting}
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


def _explained(book: Book, txs: list[dict]) -> dict[str, str]:
    """Movements already explained by something in the book, with the reason: a matching invoice, supplier
    bill, receipt, rule or booking (bank suggestions), or a draft in the Eingang with the same amount."""
    from . import bank, erfassung
    if not txs:
        return {}
    out = {}
    for tid, found in bank.suggestions(book, [t["ID"] for t in txs]).items():
        hit = next((f for f in found if f["art"] in ("rechnung", "kreditor", "quittung", "regel", "beleg")), None)
        if hit:
            out[tid] = hit["label"]
    drafts = []
    for d in erfassung.drafts(book).values():
        try:
            amount = Decimal(str(erfassung.value(d, "betrag"))).quantize(Decimal("0.01"))
        except Exception:
            continue
        sign = 1 if d.get("art") == "debitor" else -1
        drafts.append((sign * amount, (erfassung.value(d, "waehrung") or "CHF").upper(), d))
    for t in txs:
        if t["ID"] in out:
            continue
        amount = parse_amount(t["Betrag"])
        cur = bank.account_currency(book, t["Konto"]) or "CHF"
        d = next((d for a, c, d in drafts if a == amount and c == cur
                  and (d.get("zahlung") or {}).get("bank") in (None, "", t["ID"])), None)
        if d is not None:
            out[t["ID"]] = f"Entwurf {d['id']} im Eingang ({erfassung.value(d, 'name') or d.get('datei', '')})"
    return out


def _safe(fn, *args):
    try:
        return fn(*args)
    except BookError as exc:
        return {"fehler": str(exc)}
    except Exception as exc:  # network etc. — reported per transaction, never fatal
        return {"fehler": f"{type(exc).__name__}: {exc}"}

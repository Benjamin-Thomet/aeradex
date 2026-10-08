"""Bank statements (ISO 20022 camt.053): import, automatic matching, reconciliation.

Files:
    bank/auszuege/<JJJJ>/<Auszug-ID>.xml   the statement as delivered by the bank (the Beleg)
    bank/<JJJJ>.md                         one row per bank transaction with its status

On import every transaction is matched, strongest rule first:

    1. credit with a QR/SCOR reference or invoice number of an open invoice → payment booked
    2. debit with the EndToEndId or reference of an open supplier bill       → payment booked
    3. a Beleg on the same bank account, same net amount and side, ±7 days → reconciled
       (salaries incl. expenses, manual bookings with MWST, payment runs — never booked twice)
    4. a bank rule (recurring rent, subscriptions …)                         → booked
    5. anything else stays open; `suggestions` lists what it probably is (open invoice or bill,
       receipt draft, agent proposal, earlier booking, account from history) for one-click `accept`

Status: gebucht (booked from the statement), abgeglichen (matched an existing booking),
offen, ignoriert. The statement's closing balance is compared with the books.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from . import qrbill_ch as qr
from .book import Book, BookError, Row
from .files import CENT, MdTable, parse_amount, parse_date, read_table, write_table

ZERO = Decimal("0")
COLUMNS = ["ID", "Datum", "Konto", "Betrag", "Gegenpartei", "Referenz", "Text", "Status", "Beleg", "Auszug", "Hinweis"]


# ---------- parsing (namespace-agnostic: camt.053.001.02/.04/.08) ----------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, path: str):
    """Find by local names: 'Acct/Id/IBAN'."""
    if el is None:
        return None
    current = [el]
    for part in path.split("/"):
        nxt = []
        for c in current:
            nxt += [x for x in c if _local(x.tag) == part]
        if not nxt:
            return None
        current = nxt
    return current[0]


def _findall(el, name: str) -> list:
    return [x for x in el if _local(x.tag) == name] if el is not None else []


def _text(el, path: str) -> str:
    found = _find(el, path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _amount(el) -> Decimal:
    return parse_amount(_text(el, "Amt") or "0")


def _party(details, role: str) -> str:
    """RltdPties/Dbtr/Pty/Nm (camt .08) or RltdPties/Dbtr/Nm (older)."""
    return _text(details, f"RltdPties/{role}/Pty/Nm") or _text(details, f"RltdPties/{role}/Nm")


def _reference(details) -> tuple[str, str]:
    ref = _text(details, "RmtInf/Strd/CdtrRefInf/Ref")
    kind = (_text(details, "RmtInf/Strd/CdtrRefInf/Tp/CdOrPrtry/Prtry") or
            _text(details, "RmtInf/Strd/CdtrRefInf/Tp/CdOrPrtry/Cd"))
    if ref and not kind:
        kind = "SCOR" if ref.upper().startswith("RF") else "QRR" if ref.isdigit() else ""
    return kind, ref.replace(" ", "")


def parse(data: bytes) -> list[dict]:
    """All statements in a camt.053 file."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise BookError(f"Keine gültige XML-Datei: {exc}") from None
    container = _find(root, "BkToCstmrStmt")
    if container is None:
        raise BookError("Keine camt.053-Datei (BkToCstmrStmt fehlt)")
    statements = []
    for stmt in _findall(container, "Stmt"):
        iban = qr.normalize_iban(_text(stmt, "Acct/Id/IBAN"))
        balances = {}
        for bal in _findall(stmt, "Bal"):
            code = _text(bal, "Tp/CdOrPrtry/Cd")
            amount = _amount(bal) * (-1 if _text(bal, "CdtDbtInd") == "DBIT" else 1)
            balances[code] = (amount, _text(bal, "Dt/Dt") or _text(bal, "Dt/DtTm")[:10])
        entries = []
        for n, ntry in enumerate(_findall(stmt, "Ntry"), 1):
            sign = -1 if _text(ntry, "CdtDbtInd") == "DBIT" else 1
            if _text(ntry, "RvslInd").lower() == "true":
                sign = -sign
            booked = _text(ntry, "BookgDt/Dt") or _text(ntry, "BookgDt/DtTm")[:10]
            valuta = _text(ntry, "ValDt/Dt") or booked
            entry_ref = _text(ntry, "AcctSvcrRef") or _text(ntry, "NtryRef")
            info = _text(ntry, "AddtlNtryInf")
            details = []
            for dtls in _findall(ntry, "NtryDtls"):
                details += _findall(dtls, "TxDtls")
            if len(details) <= 1:
                d = details[0] if details else None
                total = _amount(ntry)
                parts = [(d, total)]
            else:
                parts = [(d, _amount(d) if _find(d, "Amt") is not None else _amount(_find(d, "AmtDtls/TxAmt")))
                         for d in details]
            for i, (d, amount) in enumerate(parts, 1):
                kind, ref = _reference(d) if d is not None else ("", "")
                party = _party(d, "Dbtr" if sign > 0 else "Cdtr") if d is not None else ""
                text = " ".join(t for t in (
                    _text(d, "RmtInf/Ustrd") if d is not None else "",
                    _text(d, "AddtlTxInf") if d is not None else "", info) if t)
                e2e = _text(d, "Refs/EndToEndId") if d is not None else ""
                tx_ref = (_text(d, "Refs/AcctSvcrRef") if d is not None else "") or entry_ref
                entries.append({
                    "datum": parse_date(valuta or booked), "buchungsdatum": booked, "betrag": sign * amount,
                    "gegenpartei": party, "referenz_typ": kind, "referenz": ref,
                    "endtoend": "" if e2e in ("NOTPROVIDED", "") else e2e, "text": text[:200],
                    "bankref": f"{tx_ref}-{i}" if len(parts) > 1 else tx_ref, "position": f"{n}.{i}",
                })
        statements.append({
            "id": _text(stmt, "Id") or _text(container, "GrpHdr/MsgId"), "iban": iban,
            "waehrung": _text(stmt, "Acct/Ccy").upper(),
            "von": _text(stmt, "FrToDt/FrDtTm")[:10], "bis": _text(stmt, "FrToDt/ToDtTm")[:10],
            "eroeffnung": balances.get("OPBD", (None, None))[0], "schluss": balances.get("CLBD", (None, None))[0],
            "schluss_datum": balances.get("CLBD", (None, None))[1], "buchungen": entries,
        })
    if not statements:
        raise BookError("Die Datei enthält keinen Kontoauszug")
    return statements


# ---------- storage ----------

def ledger_account(book: Book, iban: str) -> str:
    """The booking account for a bank IBAN: `bankkonten: {IBAN: konto}` in aeradex.yaml,
    else the main bank account for the book's own (payment) IBAN."""
    mapping = {qr.normalize_iban(k): str(v) for k, v in (book.settings.get("bankkonten") or {}).items()}
    if iban in mapping:
        return mapping[iban]
    own = {qr.normalize_iban(book.settings.get("iban")), qr.normalize_iban(book.settings.get("zahlungs_iban"))}
    if iban in own or not iban:
        return book.settings.konto("bank")
    raise BookError(f"Konto {iban} ist keinem Buchhaltungskonto zugeordnet. In den Einstellungen unter Bank "
                    f"(bankkonten) die IBAN mit einem Konto verknüpfen, z.B. 1020.")


def year_file(book: Book, year: int) -> Path:
    return book.root / "bank" / f"{year}.md"


def transactions(book: Book) -> list[dict]:
    out = []
    folder = book.root / "bank"
    for path in sorted(folder.glob("*.md")) if folder.exists() else []:
        for row in read_table(path).rows:
            out.append({**row, "_jahr": path.stem})
    return out


def _write(book: Book, rows: list[dict]) -> list[Path]:
    by_year = defaultdict(list)
    for r in rows:
        by_year[str(r["Datum"])[:4]].append({k: r.get(k, "") for k in COLUMNS})
    paths = []
    for year, items in by_year.items():
        items.sort(key=lambda r: (r["Datum"], r["ID"]))
        table = MdTable(columns=list(COLUMNS), rows=items, align={"Betrag": "right"},
                        head=f"# Bankbewegungen {year}\n\nAus den Kontoauszügen (camt.053) unter bank/auszuege/. "
                             "Status: gebucht, abgeglichen, offen, ignoriert.")
        write_table(year_file(book, int(year)), table)
        paths.append(year_file(book, int(year)))
    return paths


def _tx_id(stmt_id: str, e: dict) -> str:
    blob = "|".join([e["bankref"], e["datum"].isoformat(), f"{e['betrag']:.2f}", e["referenz"], e["text"], stmt_id if not e["bankref"] else ""])
    return "B" + hashlib.sha1(blob.encode()).hexdigest()[:10]


# ---------- matching ----------

def _linked_belege(rows: list[dict]) -> set[str]:
    return {r["Beleg"] for r in rows if r.get("Beleg") and r.get("Status") in ("gebucht", "abgeglichen")}


def _beleg_effects(book: Book, konto: str) -> dict[str, tuple[date, Decimal]]:
    """Per Beleg: its date and its net effect on `konto` (in the account's currency). A Beleg may
    touch the account in several lines — a salary paid out with expenses, a booking with MWST split off."""
    acct = book.accounts.get(konto)
    foreign = acct.waehrung if acct is not None and acct.is_foreign else ""
    out: dict[str, tuple[date, Decimal]] = {}
    for r in book.rows:
        if konto not in (r.soll, r.haben):
            continue
        value = r.betrag
        if foreign:                       # a foreign-currency account: compare in its currency
            if r.waehrung != foreign or r.fw is None:
                continue
            value = r.fw
        side = value if r.soll == konto else -value
        when, total = out.get(r.beleg, (r.datum, ZERO))
        out[r.beleg] = (min(when, r.datum), total + side)
    return out


def _match_existing(book: Book, konto: str, amount: Decimal, when: date, taken: set[str],
                    days: int = 7) -> str | None:
    """A Beleg on this account with the same net amount and side, closest date within ±`days`."""
    best = None
    for beleg, (datum, net) in _beleg_effects(book, konto).items():
        if beleg in taken or net != amount or abs((datum - when).days) > days:
            continue
        distance = abs((datum - when).days)
        if best is None or distance < best[0]:
            best = (distance, beleg)
    return best[1] if best else None


def import_file(book: Book, source: Path) -> tuple[dict, list[Path]]:
    """Import a statement (camt.053 or a format from a plugin): store it, record its
    transactions, book or reconcile what matches."""
    from . import invoices, kreditoren, plugins
    data = Path(source).read_bytes()
    fmt = plugins.bank_format_for(book, Path(source).name, data)
    statements = fmt.parse(data, book)
    suffix = Path(source).suffix.lower() or (fmt.suffixes[0] if fmt.suffixes else "")
    existing = transactions(book)
    known = {r["ID"] for r in existing}
    taken = _linked_belege(existing)
    rows = list(existing)
    touched: list[Path] = []
    summary = {"auszuege": [], "neu": 0, "doppelt": 0, "gebucht": 0, "abgeglichen": 0, "offen": 0}
    for stmt in statements:
        konto = str(stmt.get("konto") or "") or ledger_account(book, stmt.get("iban", ""))
        acct = book.account(konto)
        stmt_cur = (stmt.get("waehrung") or "").upper()
        if stmt_cur and stmt_cur != acct.waehrung:
            raise BookError(f"Der Auszug ist in {stmt_cur}, Konto {konto} führt {acct.waehrung} — "
                            "unter bankkonten das richtige Konto zuordnen")
        year = (stmt["bis"] or (stmt["buchungen"][0]["datum"].isoformat() if stmt["buchungen"] else date.today().isoformat()))[:4]
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", stmt["id"])[:60] or hashlib.sha1(data).hexdigest()[:10]
        target = book.root / "bank" / "auszuege" / year / f"{safe_id}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy(source, target)
            touched.append(target)
        summary["auszuege"].append({"id": stmt["id"], "iban": stmt.get("iban", ""), "konto": konto, "format": fmt.name,
                                    "schluss": stmt["schluss"], "schluss_datum": stmt["schluss_datum"]})
        for e in stmt["buchungen"]:
            tid = _tx_id(stmt["id"], e)
            if tid in known:
                summary["doppelt"] += 1
                continue
            known.add(tid)
            summary["neu"] += 1
            row = {"ID": tid, "Datum": e["datum"].isoformat(), "Konto": konto, "Betrag": f"{e['betrag']:.2f}",
                   "Gegenpartei": e["gegenpartei"], "Referenz": e["referenz"] or e["endtoend"],
                   "Text": e["text"], "Status": "offen", "Beleg": "", "Auszug": str(target.relative_to(book.root))}
            beleg = _auto_book(book, e, konto, invoices, kreditoren)
            if beleg:
                row["Status"], row["Beleg"] = "gebucht", beleg
                summary["gebucht"] += 1
            else:
                match = _match_existing(book, konto, e["betrag"], e["datum"], taken)
                rule = None if match else matching_rule(book, row)
                if match:
                    row["Status"], row["Beleg"] = "abgeglichen", match
                    summary["abgeglichen"] += 1
                elif rule is not None:
                    booked, _ = _book_with_rule(book, row, rule)
                    row["Status"], row["Beleg"], row["Hinweis"] = "gebucht", booked.beleg, f"Regel {rule['id']}"
                    summary["gebucht"] += 1
                    summary.setdefault("regeln", 0)
                    summary["regeln"] += 1
                else:
                    summary["offen"] += 1
            if row["Beleg"]:
                taken.add(row["Beleg"])
            rows.append(row)
            book.reload()
    touched += _write(book, rows)
    return summary, touched


def _auto_book(book: Book, e: dict, konto: str, invoices, kreditoren) -> str | None:
    """Rules 1 and 2: deterministic matches book the payment right away."""
    amount = e["betrag"]
    try:
        if amount > 0:
            found, how = invoices.find_match(book, amount, " ".join([e["referenz"], e["text"]]))
            if found and how in ("referenz", "nummer") and invoices.amount_fits(book, found, amount, konto):
                row, _ = invoices.pay_invoice(book, found["nummer"], amount, e["datum"], konto)
                return row.beleg
        elif amount < 0:
            bills = kreditoren.bills(book)
            paid = kreditoren.payments(book)
            for nr, meta in bills.items():
                st = kreditoren.state(book, meta, paid.get(nr, []))
                if st["status"] not in ("offen", "angewiesen"):
                    continue
                ref_hit = meta.get("referenz") and meta["referenz"] == e["referenz"]
                if (e["endtoend"] == nr or ref_hit) and kreditoren.amount_fits(book, meta, st, -amount, konto):
                    row, _ = kreditoren.pay(book, nr, e["datum"], -amount, konto)
                    return row.beleg
    except BookError:
        return None
    return None


# ---------- rules: recurring movements booked on import ----------

def rules_path(book: Book) -> Path:
    return book.root / "bank" / "regeln.yaml"


def rules(book: Book) -> list[dict]:
    """bank/regeln.yaml: [{id, name, gegenpartei, text, betrag, richtung, konto, mwst, buchungstext, aktiv}].
    Criteria that are set must all match (texts as case-insensitive parts, betrag exactly)."""
    from .files import read_yaml
    p = rules_path(book)
    return list((read_yaml(p) or {}).get("regeln") or []) if p.exists() else []


def matching_rule(book: Book, tx: dict) -> dict | None:
    amount = parse_amount(tx["Betrag"])
    for r in rules(book):
        if r.get("aktiv") is False:
            continue
        if r.get("gegenpartei") and str(r["gegenpartei"]).lower() not in party(tx).lower():
            continue
        if r.get("text") and str(r["text"]).lower() not in (tx.get("Text") or "").lower():
            continue
        if r.get("betrag") not in (None, "") and parse_amount(r["betrag"]) != abs(amount):
            continue
        if r.get("richtung") == "belastung" and amount > 0 or r.get("richtung") == "gutschrift" and amount < 0:
            continue
        if not (r.get("gegenpartei") or r.get("text")):
            continue                                   # a rule needs something to recognise the movement by
        return r
    return None


def _book_with_rule(book: Book, tx: dict, rule: dict):
    from .journal import book_entry
    amount = parse_amount(tx["Betrag"])
    text = (rule.get("buchungstext") or " ".join(t for t in (tx.get("Gegenpartei"), tx.get("Text")) if t))[:120]
    soll, haben = (tx["Konto"], str(rule["konto"])) if amount > 0 else (str(rule["konto"]), tx["Konto"])
    return book_entry(book, tx["Datum"], soll, haben, abs(amount), text or "Bankbewegung", mwst=rule.get("mwst") or "",
                      waehrung=account_currency(book, tx["Konto"]))


def account_currency(book: Book, konto: str) -> str:
    """'' for a CHF account, else its currency (amounts of its movements are in that currency)."""
    acct = book.accounts.get(konto)
    return acct.waehrung if acct is not None and acct.is_foreign else ""


def save_rules(book: Book, items: list[dict]) -> Path:
    from .files import write_yaml
    write_yaml(rules_path(book), {"regeln": items})
    return rules_path(book)


def add_rule(book: Book, konto: str, gegenpartei: str = "", text: str = "", betrag=None, richtung: str = "",
             mwst: str = "", buchungstext: str = "", name: str = "") -> tuple[dict, Path]:
    book.account(konto)
    if not (gegenpartei.strip() or text.strip()):
        raise BookError("Eine Regel braucht eine Gegenpartei oder einen Text, an dem sie die Bewegung erkennt")
    if richtung not in ("", "belastung", "gutschrift"):
        raise BookError("richtung: belastung, gutschrift oder leer")
    if mwst:
        from .mwst import code
        code(mwst)
    items = rules(book)
    nums = [int(str(r.get("id", "R0"))[1:]) for r in items if str(r.get("id", "")).startswith("R")
            and str(r.get("id"))[1:].isdigit()]
    rule = {"id": f"R{max(nums, default=0) + 1}", "name": name or gegenpartei or text, "gegenpartei": gegenpartei.strip(),
            "text": text.strip(), "betrag": f"{parse_amount(betrag):.2f}" if betrag not in (None, "") else "",
            "richtung": richtung, "konto": str(konto), "mwst": (mwst or "").upper(), "buchungstext": buchungstext.strip(),
            "aktiv": True}
    return rule, save_rules(book, items + [rule])


def rule_from_transaction(book: Book, tid: str, mit_betrag: bool = False) -> tuple[dict, Path]:
    """«Immer so buchen»: a rule from a booked movement — same counterparty, same counter account."""
    tx = find(book, tid)
    if tx["Status"] != "gebucht" or not tx.get("Beleg"):
        raise BookError(f"Bankbewegung {tid} ist nicht gebucht — zuerst buchen, dann die Regel daraus machen")
    rows = [r for r in book.rows if r.beleg == tx["Beleg"]]
    counter = next((r.haben if r.soll == tx["Konto"] else r.soll for r in rows
                    if tx["Konto"] in (r.soll, r.haben) and r.soll and r.haben), None)
    if not counter:
        raise BookError(f"Beleg {tx['Beleg']} hat kein eindeutiges Gegenkonto")
    if not party(tx):
        raise BookError("Die Bewegung hat keine Gegenpartei — Regel von Hand mit einem Text anlegen")
    amount = parse_amount(tx["Betrag"])
    return add_rule(book, counter, gegenpartei=party(tx), betrag=abs(amount) if mit_betrag else None,
                    richtung="belastung" if amount < 0 else "gutschrift", mwst=rows[0].mwst if rows else "",
                    buchungstext=rows[0].text if rows else "")


def remove_rule(book: Book, rule_id: str) -> Path:
    items = rules(book)
    if not any(r.get("id") == rule_id for r in items):
        raise BookError(f"Regel {rule_id} nicht gefunden")
    return save_rules(book, [r for r in items if r.get("id") != rule_id])


def apply_rules(book: Book) -> tuple[list[str], list[Path]]:
    """Book the open movements a rule recognises (e.g. after adding a rule)."""
    done_, touched = [], []
    for tx in [t for t in transactions(book) if t["Status"] == "offen"]:
        rule = matching_rule(book, tx)
        if rule is None:
            continue
        row, t = _book_with_rule(book, tx, rule)
        touched += t + _set(book, tx["ID"], Status="gebucht", Beleg=row.beleg, Hinweis=f"Regel {rule['id']}")
        done_.append(tx["ID"])
        book.reload()
    return done_, touched


# ---------- working on open transactions ----------

def find(book: Book, tid: str) -> dict:
    for r in transactions(book):
        if r["ID"] == tid:
            return r
    raise BookError(f"Bankbewegung {tid} nicht gefunden")


def _set(book: Book, tid: str, **changes) -> list[Path]:
    rows = transactions(book)
    for r in rows:
        if r["ID"] == tid:
            r.update(changes)
    return _write(book, rows)


def book_transaction(book: Book, tid: str, gegenkonto: str, text: str = "", mwst: str = "") -> tuple[Row, list[Path]]:
    """Book an open bank transaction against a counter account (Ertrag/Aufwand/…)."""
    from .journal import book_entry
    tx = find(book, tid)
    if tx["Status"] != "offen":
        raise BookError(f"Bankbewegung {tid} ist {tx['Status']}")
    amount = parse_amount(tx["Betrag"])
    konto = tx["Konto"]
    text = text or " ".join(t for t in (tx["Gegenpartei"], tx["Text"]) if t)[:120] or "Bankbewegung"
    soll, haben = (konto, gegenkonto) if amount > 0 else (gegenkonto, konto)
    row, touched = book_entry(book, tx["Datum"], soll, haben, abs(amount), text, mwst=mwst,
                              waehrung=account_currency(book, konto))
    return row, touched + _set(book, tid, Status="gebucht", Beleg=row.beleg)


def assign(book: Book, tid: str, nummer: str) -> tuple[Row, list[Path]]:
    """Settle an invoice (R-…) or supplier bill (E-…) with an open bank transaction."""
    from . import invoices, kreditoren
    tx = find(book, tid)
    if tx["Status"] != "offen":
        raise BookError(f"Bankbewegung {tid} ist {tx['Status']}")
    amount = parse_amount(tx["Betrag"])
    if nummer.startswith("R-"):
        if amount <= 0:
            raise BookError("Eine Belastung kann keine Kundenrechnung bezahlen")
        row, touched = invoices.pay_invoice(book, nummer, amount, tx["Datum"], tx["Konto"])
    elif nummer.startswith("E-"):
        if amount >= 0:
            raise BookError("Eine Gutschrift kann keine Lieferantenrechnung bezahlen")
        row, touched = kreditoren.pay(book, nummer, tx["Datum"], -amount, tx["Konto"])
    else:
        raise BookError("Nummer einer Rechnung (R-…) oder eines Kreditors (E-…) angeben")
    return row, touched + _set(book, tid, Status="gebucht", Beleg=row.beleg)


def link(book: Book, tid: str, beleg: str) -> list[Path]:
    """Mark an open transaction as matching an existing booking (manual reconciliation)."""
    tx = find(book, tid)
    if tx["Status"] != "offen":
        raise BookError(f"Bankbewegung {tid} ist {tx['Status']}")
    rows = [r for r in book.rows if r.beleg == beleg]
    if not rows:
        raise BookError(f"Beleg {beleg} nicht gefunden")
    if beleg in _linked_belege(transactions(book)):
        raise BookError(f"Beleg {beleg} ist schon mit einer anderen Bankbewegung verknüpft")
    return _set(book, tid, Status="abgeglichen", Beleg=beleg)


def ignore(book: Book, tid: str, grund: str) -> list[Path]:
    tx = find(book, tid)
    if tx["Status"] != "offen":
        raise BookError(f"Bankbewegung {tid} ist {tx['Status']}")
    if not grund.strip():
        raise BookError("Grund angeben (z.B. 'interner Übertrag, im anderen Konto gebucht')")
    return _set(book, tid, Status="ignoriert", Text=f"{tx['Text']} [ignoriert: {grund.strip()}]"[:240])


# ---------- counterparty: from its own column, or read from the booking text ----------

_PARTY_PREFIX = re.compile(
    r"^(?:(?:Dauerauftrag|Kartenzahlung|Karte|E-Banking[- ](?:Vergütung|Auftrag)|Vergütung|Gutschrift|Zahlung|"
    r"Einzahlung|Lastschrift|LSV\+?|Debit Direct|TWINT|eBill|Belastung|Überweisung|Auftrag)\b[:\s]*)+", re.I)
_PARTY_CUT = re.compile(r",|;|\s(?:Mitteilung|Referenz|QR-Referenz|Kurs|Police|RE|Rechnung|Rg\.?)\b"
                        r"|\s(?:CHF|EUR|USD|GBP)\s+\d", re.I)
_PARTY_DATE = re.compile(r"(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|"
                         r"dezember|\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}|\d{1,2}/\d{4})", re.I)
_PARTY_FORM = re.compile(r"(?:AG|GmbH|SA|S\.A\.|Sàrl|Sagl|KG|KlG|Genossenschaft|Inc\.?|Ltd\.?|LLC)", re.I)


def counterparty_from_text(text: str) -> str:
    """The counterparty inside a booking text, for statements without a column for it (many CSV exports):
    'E-Banking Vergütung TechShop Bern AG, Bern, Mitteilung: RE 88' → 'TechShop Bern AG'. Months and
    years are dropped, so the same payee is the same name every month ('Dauerauftrag MIETE AUGUST 2026
    Immobilien Aare AG' → 'Immobilien Aare AG'). A guess: used for matching, never stored."""
    t = _PARTY_PREFIX.sub("", (text or "").strip())
    cut = _PARTY_CUT.search(t)
    words = [w for w in (t[:cut.start()] if cut else t).split() if not _PARTY_DATE.fullmatch(w)]
    for i, w in enumerate(words):
        if i and _PARTY_FORM.fullmatch(w):
            name = words[max(0, i - 3):i]
            if not name[-1].isupper():               # 'MIETE Immobilien Aare' → 'Immobilien Aare'
                while len(name) > 1 and name[0].isupper():
                    name = name[1:]
            return " ".join(name + [w])
    return " ".join(words[:4])


def party(tx: dict) -> str:
    """Counterparty of a movement: the statement's own, else read from the text."""
    return (tx.get("Gegenpartei") or "").strip() or counterparty_from_text(tx.get("Text") or "")


# ---------- suggestions: what an open movement probably is (one click to take it) ----------

_LEGAL = re.compile(r"\b(ag|gmbh|sa|sàrl|sarl|ltd|inc|schweiz|suisse|svizzera)\b")


def _norm(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("é", "e"), ("è", "e"), ("à", "a")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", _LEGAL.sub("", s))


def _name_in(name: str, *texts: str) -> bool:
    """The (normalised) counterparty name appears in one of the texts, or a text in it."""
    n = _norm(name)
    if len(n) < 4:
        return False
    for t in texts:
        m = _norm(t)
        if len(m) >= 4 and (n in m or m in n):
            return True
    return False


def _history(book: Book, konto: str) -> list[tuple[str, str, str, str]]:
    """Earlier free bookings on the bank account: (text, counter account, MWST code, Beleg)."""
    out = []
    for beleg, rows in _group(book).items():
        mine = [r for r in rows if konto in (r.soll, r.haben)]
        if not mine or any(r.quelle for r in rows):
            continue                                 # invoice/bill/payroll rows are not a pattern to repeat
        counter = {r.haben if r.soll == konto else r.soll for r in mine} - {konto, ""}
        main = [c for c in counter if not c.startswith("117")]   # Vorsteuer lines split off by MWST
        if len(main) == 1:
            out.append((rows[0].text, main[0], next((r.mwst for r in rows if r.mwst), ""), beleg))
    return out


def _group(book: Book) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in book.rows:
        groups[r.beleg].append(r)
    return groups


def suggestions(book: Book, ids: list[str] | None = None) -> dict[str, list[dict]]:
    """For each open movement the likely matches, best first. A suggestion is
    {art, ziel, label, grund, sicher, …}; `sicher` ones can be taken in bulk.

    art: vorschlag (an agent's proposal for this movement), rechnung (open invoice), kreditor (open
    supplier bill), quittung (a receipt draft paid with it), regel (a bank rule recognises it), beleg (an
    existing booking, outside the import's ±7 days), konto (book against an account known from earlier
    bookings or the supplier). A suggestion with `pruefen` (a receipt without an account) cannot be taken
    in one click; it points to the page where it is completed."""
    from . import erfassung, invoices as inv, kreditoren as kred
    from .journal import list_proposals
    txs = [t for t in transactions(book) if t["Status"] == "offen" and (ids is None or t["ID"] in ids)]
    if not txs:
        return {}
    paid = inv.settlements(book)
    inv_meta = inv.invoices(book)
    open_inv = [(inv.invoice_state(book, m, paid.get(k, [])), m) for k, m in inv_meta.items()]
    open_inv = [(s, m) for s, m in open_inv if s["status"] in ("offen", "teilbezahlt")]
    kp = kred.payments(book)
    open_bills = [(kred.state(book, m, kp.get(k, [])), m) for k, m in kred.bills(book).items()]
    open_bills = [(s, m) for s, m in open_bills if s["status"] in ("offen", "angewiesen")]
    sups = kred.suppliers(book)
    receipts = [d for d in erfassung.drafts(book, "quittung").values()]
    proposals = [p for p in list_proposals(book) if p.get("Bank")]
    taken = _linked_belege(transactions(book))
    history: dict[str, list] = {}
    effects: dict[str, dict] = {}
    out: dict[str, list[dict]] = {}
    for tx in txs:
        amount = parse_amount(tx["Betrag"])
        when = date.fromisoformat(tx["Datum"])
        who, text = party(tx), tx.get("Text") or ""
        konto = tx["Konto"]
        cur = account_currency(book, konto) or "CHF"
        found: list[dict] = []
        for p in proposals:
            if p["Bank"] == tx["ID"]:
                found.append({"art": "vorschlag", "ziel": p["ID"], "sicher": False,
                              "label": f"Vorschlag {p['ID']} freigeben: {p['Soll']} an {p['Haben']} · {p['Text']}",
                              "grund": p.get("Begründung") or "vom Agenten vorgeschlagen"})
        if amount > 0:
            hits = [(s, m) for s, m in open_inv if s["waehrung"] == cur and s["offen"] == amount]
            number_hits = [s["nummer"] for s, _ in hits if s["nummer"] in text.upper()]
            name_hits = [s["nummer"] for s, m in hits if _name_in(
                who, *(str(v) for k, v in (m.get("an") or {}).items() if k in ("name", "zusatz", "firma")))]
            decisive = number_hits or name_hits
            for s, m in hits:
                names = [str(v) for k, v in (m.get("an") or {}).items() if k in ("name", "zusatz", "firma")]
                by_name = _name_in(who, *names) or s["nummer"] in text.upper()
                found.append({"art": "rechnung", "ziel": s["nummer"],
                              "sicher": decisive == [s["nummer"]],
                              "label": f"Rechnung {s['nummer']} · {s['name']} begleichen",
                              "grund": "Betrag = offener Betrag" + (", Kunde stimmt" if by_name else "")})
        elif amount < 0:
            bill_hits = []
            for s, m in open_bills:
                by_nr = bool(s["rechnungsnr"] and s["rechnungsnr"].upper() in text.upper())
                by_name = _name_in(who, s["name"] or "") or by_nr
                same = s["waehrung"] == cur and s["offen"] == -amount
                if same or (by_name and s["waehrung"] != cur and kred.amount_fits(book, m, s, -amount, konto)):
                    bill_hits.append((s, same, by_name, by_nr))
            n_same = sum(1 for _, same, _, _ in bill_hits if same)
            number_hits = sum(1 for _, _, _, nr in bill_hits if nr)
            for s, same, by_name, by_nr in bill_hits:
                # several open bills with this amount: only the invoice number in the text decides
                sure = same and ((by_nr and number_hits == 1) if n_same > 1 else by_name)
                found.append({"art": "kreditor", "ziel": s["nummer"], "sicher": sure,
                              "label": f"Kreditor {s['nummer']} · {s['name']}"
                                       + (f" ({s['rechnungsnr']})" if s["rechnungsnr"] else "") + " bezahlen",
                              "grund": ("Betrag = offener Betrag" if same else f"Rechnung in {s['waehrung']}")
                                       + (", Lieferant stimmt" if by_name else "")})
            for d in receipts:
                try:
                    r_amount = Decimal(erfassung.value(d, "betrag")).quantize(CENT)
                    r_date = date.fromisoformat(erfassung.value(d, "datum"))
                except Exception:
                    continue
                pay = d.get("zahlung") or {}
                if pay.get("art") == "bank" and pay.get("bank") != tx["ID"]:
                    continue                          # already matched with another movement
                if pay.get("art") in ("spesen", "buchung") or (pay.get("art") == "konto" and pay.get("quelle")
                                                                not in ("Standard", "Karte", "TWINT", None)):
                    continue                          # paid in cash, privately or by credit card
                if (erfassung.value(d, "waehrung") or "CHF").upper() != cur or r_amount != -amount \
                        or abs((r_date - when).days) > 5:
                    continue
                ready = bool((d.get("konto") or {}).get("wert") or (d.get("positionen") or {}).get("zeilen"))
                by_name = _name_in(erfassung.value(d, "name"), who, text)
                found.append({"art": "quittung", "ziel": d["id"], "sicher": ready and by_name,
                              **({} if ready else {"pruefen": f"/eingang/quittung?entwurf={d['id']}"}),
                              "label": f"Quittung {d['id']} · {erfassung.value(d, 'name') or d['datei']} buchen"
                                       + (f" auf {d['konto']['wert']}" if (d.get("konto") or {}).get("wert") else ""),
                              "grund": "gleicher Betrag, Datum ±5 Tage" + ("" if ready else " — Konto fehlt noch")})
        if konto not in effects:
            effects[konto] = _beleg_effects(book, konto)
        for beleg, (datum, net) in effects[konto].items():
            if beleg not in taken and net == amount and 7 < abs((datum - when).days) <= 14:
                found.append({"art": "beleg", "ziel": beleg, "sicher": False,
                              "label": f"Mit Beleg {beleg} vom {datum:%d.%m.%Y} abgleichen",
                              "grund": "schon gebucht, gleicher Betrag"})
        matched = any(f["art"] in ("rechnung", "kreditor", "quittung", "vorschlag") for f in found)
        rule = None if matched else matching_rule(book, tx)
        if rule is not None:                          # e.g. a rule added after the import
            name = book.accounts[str(rule["konto"])].name if str(rule["konto"]) in book.accounts else ""
            found.append({"art": "regel", "ziel": rule["id"], "sicher": True,
                          "label": f"Regel {rule['id']} «{rule.get('name') or rule.get('gegenpartei') or rule.get('text')}»:"
                                   f" auf {rule['konto']} {name} buchen",
                          "grund": "wiederkehrende Bewegung, Regel erkennt sie"})
        key = who or text
        if not matched and rule is None and key:
            if konto not in history:
                history[konto] = _history(book, konto)
            seen = [(c, m) for t, c, m, _ in history[konto] if _name_in(key, t)]
            if seen:
                counts = defaultdict(int)
                for c, _ in seen:
                    counts[c] += 1
                best = max(counts, key=counts.get)
                mwst_ = next((m for c, m in seen if c == best and m), "")
                name = book.accounts[best].name if best in book.accounts else ""
                found.append({"art": "konto", "ziel": best, "mwst": mwst_, "sicher": False,
                              "label": f"Buchen auf {best} {name}" + (f" ({mwst_})" if mwst_ else ""),
                              "grund": f"«{key}» wurde {counts[best]}× so gebucht"
                                       + ("" if len(counts) == 1 else f" (auch {', '.join(c for c in counts if c != best)})")})
            else:
                sup = next((s for s in sups.values() if who and s.get("konto")
                            and _name_in(who, str(s.get("name", "")))), None)
                if sup is not None and amount < 0:
                    k = str(sup["konto"])
                    found.append({"art": "konto", "ziel": k, "mwst": str(sup.get("mwst") or ""), "sicher": False,
                                  "label": f"Buchen auf {k} {book.accounts[k].name if k in book.accounts else ''}",
                                  "grund": f"Standardkonto des Lieferanten {sup.get('name')}"})
        order = {"vorschlag": 0, "regel": 1, "rechnung": 1, "kreditor": 1, "quittung": 2, "beleg": 3, "konto": 4}
        if sum(1 for f in found if f["sicher"]) > 1:
            for f in found:
                f["sicher"] = False                    # competing matches need a person's choice
        found.sort(key=lambda f: (not f["sicher"], order[f["art"]]))
        if found:
            out[tx["ID"]] = found
    return out


def accept(book: Book, tid: str, art: str, ziel: str) -> tuple[str, list[Path]]:
    """Take one suggestion for an open movement (as listed by `suggestions`)."""
    from . import erfassung
    from .journal import approve
    options = suggestions(book, [tid]).get(tid, [])
    s = next((o for o in options if o["art"] == art and o["ziel"] == ziel), None)
    if s is None:
        raise BookError(f"Für Bankbewegung {tid} gibt es den Vorschlag {art} {ziel} nicht (mehr)")
    tx = find(book, tid)
    if art in ("rechnung", "kreditor"):
        row, touched = assign(book, tid, ziel)
        return f"{ziel} beglichen (Beleg {row.beleg})", touched
    if art == "beleg":
        return f"mit Beleg {ziel} abgeglichen", link(book, tid, ziel)
    if art == "vorschlag":
        rows, touched = approve(book, [ziel])
        return f"Vorschlag {ziel} gebucht (Beleg {rows[0].beleg})", touched
    if art == "regel":
        rule = matching_rule(book, tx)
        if rule is None or rule["id"] != ziel:
            raise BookError(f"Regel {ziel} passt nicht (mehr) auf Bankbewegung {tid}")
        row, touched = _book_with_rule(book, tx, rule)
        return (f"mit Regel {ziel} gebucht (Beleg {row.beleg})",
                touched + _set(book, tid, Status="gebucht", Beleg=row.beleg, Hinweis=f"Regel {ziel}"))
    if art == "quittung":
        meta = erfassung.draft(book, ziel)
        v = erfassung.form_values(meta)
        if not (v["konto"] or v["positionen"]):
            raise BookError(f"Quittung {ziel} hat noch kein Konto — unter Einkauf › Entwürfe kontieren")
        cur = (v["waehrung"] or "CHF").upper()
        rows, touched = erfassung.book_receipt(book, ziel, v["datum"] or tx["Datum"], (v["name"] or "Quittung").strip(),
                                               v["betrag"], v["konto"], v["mwst"], v["positionen"] or None,
                                               "" if cur == "CHF" else cur, None, {"art": "bank", "bank": tid})
        return f"Quittung {ziel} gebucht (Beleg {rows[0].beleg})", touched
    if art == "konto":
        row, touched = book_transaction(book, tid, ziel, mwst=s.get("mwst") or "")
        return f"auf {ziel} gebucht (Beleg {row.beleg})", touched
    raise BookError(f"Unbekannte Vorschlagsart {art}")


def candidates(book: Book, tid: str, q: str = "") -> list[dict]:
    """«Suchen & zuordnen» for one open movement: open invoices (money in) or supplier bills and receipt
    drafts (money out), and bookings on the account not linked to a movement yet. Without a query only
    those with the same amount; with one, anything whose name, number or text contains it (or whose
    amount equals it). Each hit: {art, ziel, label, grund, betrag}; art rechnung/kreditor → `assign`,
    beleg → `link`, quittung → completed on its own page."""
    from . import erfassung, invoices as inv, kreditoren as kred
    tx = find(book, tid)
    amount = parse_amount(tx["Betrag"])
    konto = tx["Konto"]
    query = (q or "").strip()
    try:
        q_amount = abs(parse_amount(query)) if query else None
    except Exception:
        q_amount = None

    def wanted(value: Decimal, *texts: str) -> bool:
        if not query:
            return value == abs(amount)
        if q_amount is not None and value == q_amount:
            return True
        needle = query.lower()
        return any(needle in (t or "").lower() for t in texts) or _name_in(query, *texts)

    out: list[dict] = []
    if amount > 0:
        paid = inv.settlements(book)
        for k, m in inv.invoices(book).items():
            s = inv.invoice_state(book, m, paid.get(k, []))
            if s["status"] not in ("offen", "teilbezahlt"):
                continue
            if wanted(s["offen"], s["nummer"], s["name"], str((m.get("extern") or {}).get("rechnungsnr", ""))):
                out.append({"art": "rechnung", "ziel": s["nummer"], "betrag": s["offen"],
                            "label": f"Rechnung {s['nummer']} · {s['name']}",
                            "grund": f"offen {s['offen']:.2f}, fällig {s['faellig']}"})
    else:
        kp = kred.payments(book)
        for k, m in kred.bills(book).items():
            s = kred.state(book, m, kp.get(k, []))
            if s["status"] not in ("offen", "angewiesen"):
                continue
            if wanted(s["offen"], s["nummer"], s["name"], s.get("rechnungsnr") or ""):
                out.append({"art": "kreditor", "ziel": s["nummer"], "betrag": s["offen"],
                            "label": f"Kreditor {s['nummer']} · {s['name']}"
                                     + (f" ({s['rechnungsnr']})" if s.get("rechnungsnr") else ""),
                            "grund": f"offen {s['offen']:.2f}, fällig {s['faellig']}"})
        for d in erfassung.drafts(book, "quittung").values():
            try:
                r_amount = Decimal(erfassung.value(d, "betrag")).quantize(CENT)
            except Exception:
                continue
            if wanted(r_amount, d["id"], erfassung.value(d, "name"), d.get("datei") or ""):
                out.append({"art": "quittung", "ziel": d["id"], "betrag": r_amount,
                            "pruefen": f"/eingang/quittung?entwurf={d['id']}",
                            "label": f"Quittung {d['id']} · {erfassung.value(d, 'name') or d['datei']}",
                            "grund": f"vom {erfassung.value(d, 'datum') or '?'}"})
    taken = _linked_belege(transactions(book))
    groups = _group(book)
    for beleg, (datum, net) in _beleg_effects(book, konto).items():
        if beleg in taken or (net > 0) != (amount > 0) or not net:
            continue
        text = groups[beleg][0].text if groups.get(beleg) else ""
        if wanted(abs(net), beleg, text):
            out.append({"art": "beleg", "ziel": beleg, "betrag": abs(net),
                        "label": f"Beleg {beleg} · {text}", "grund": f"schon gebucht am {datum:%d.%m.%Y}"})
    out.sort(key=lambda c: (c["betrag"] != abs(amount), c["art"] == "beleg"))
    return out[:30]


def reconciliation(book: Book) -> list[dict]:
    """Per stored statement: the bank's closing balance against the books on that date."""
    from .ledger import BalanceEngine
    eng = BalanceEngine(book)
    out = []
    folder = book.root / "bank" / "auszuege"
    open_by_konto = defaultdict(lambda: ZERO)
    for r in transactions(book):
        if r["Status"] == "offen":
            open_by_konto[(r["Konto"], r["Datum"])] += parse_amount(r["Betrag"])
    from . import plugins
    for path in sorted(folder.glob("*/*")) if folder.exists() else []:
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            statements = plugins.bank_format_for(book, path.name, data).parse(data, book)
        except BookError:
            continue
        for stmt in statements:
            if stmt["schluss"] is None or not stmt["schluss_datum"]:
                continue
            try:
                konto = str(stmt.get("konto") or "") or ledger_account(book, stmt.get("iban", ""))
            except BookError:
                continue
            when = parse_date(stmt["schluss_datum"])
            if account_currency(book, konto):
                from .ledger import fw_balance
                books = fw_balance(book, konto, when)
            else:
                books = eng.balance_at(konto, when) if when.year in book.years() else ZERO
            pending = sum((v for (k, d), v in open_by_konto.items() if k == konto and d <= when.isoformat()), ZERO)
            out.append({"auszug": str(path.relative_to(book.root)), "id": stmt["id"], "konto": konto,
                        "datum": when, "bank": stmt["schluss"], "buch": books, "offen": pending,
                        "differenz": stmt["schluss"] - books - pending})
    return out

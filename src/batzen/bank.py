"""Bank statements (ISO 20022 camt.053): import, automatic matching, reconciliation.

Files:
    bank/auszuege/<JJJJ>/<Auszug-ID>.xml   the statement as delivered by the bank (the Beleg)
    bank/<JJJJ>.md                         one row per bank transaction with its status

On import every transaction is matched, strongest rule first:

    1. credit with a QR/SCOR reference or invoice number of an open invoice → payment booked
    2. debit with the EndToEndId or reference of an open supplier bill       → payment booked
    3. a journal row on the same bank account, same amount and side, ±7 days → reconciled
       (salaries, manual bookings, payment runs booked earlier — never booked twice)
    4. anything else stays open for a person or the agent

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
            "von": _text(stmt, "FrToDt/FrDtTm")[:10], "bis": _text(stmt, "FrToDt/ToDtTm")[:10],
            "eroeffnung": balances.get("OPBD", (None, None))[0], "schluss": balances.get("CLBD", (None, None))[0],
            "schluss_datum": balances.get("CLBD", (None, None))[1], "buchungen": entries,
        })
    if not statements:
        raise BookError("Die Datei enthält keinen Kontoauszug")
    return statements


# ---------- storage ----------

def ledger_account(book: Book, iban: str) -> str:
    """The booking account for a bank IBAN: `bankkonten: {IBAN: konto}` in batzen.yaml,
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


def _match_existing(book: Book, konto: str, amount: Decimal, when: date, taken: set[str]) -> str | None:
    """A journal row on this account with the same amount and side, closest date within ±7 days."""
    best = None
    for r in book.rows:
        if r.beleg in taken or abs((r.datum - when).days) > 7:
            continue
        side = r.betrag if r.soll == konto else (-r.betrag if r.haben == konto else None)
        if side is None or side != amount:
            continue
        distance = abs((r.datum - when).days)
        if best is None or distance < best[0]:
            best = (distance, r.beleg)
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
        if acct.is_foreign:
            raise BookError(f"Konto {konto} führt {acct.waehrung}: Bankimport für Fremdwährungskonten folgt noch — "
                            "Bewegungen bitte im Journal mit Währung buchen")
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
                if match:
                    row["Status"], row["Beleg"] = "abgeglichen", match
                    summary["abgeglichen"] += 1
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
            if found and how in ("referenz", "nummer") and amount <= found["offen"]:
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
    row, touched = book_entry(book, tx["Datum"], soll, haben, abs(amount), text, mwst=mwst)
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
            books = eng.balance_at(konto, when) if when.year in book.years() else ZERO
            pending = sum((v for (k, d), v in open_by_konto.items() if k == konto and d <= when.isoformat()), ZERO)
            out.append({"auszug": str(path.relative_to(book.root)), "id": stmt["id"], "konto": konto,
                        "datum": when, "bank": stmt["schluss"], "buch": books, "offen": pending,
                        "differenz": stmt["schluss"] - books - pending})
    return out

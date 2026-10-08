"""Find and recode: give many bookings another account and/or MWST code in one go.

The selection is a list of Belege. The account is replaced wherever it appears in a Beleg — Soll or
Haben — and a new MWST code splits the gross amount anew (net + tax). Manual Belege are rewritten
like an edit (`journal.plan_amend`: same number, receipts stay); supplier bills change on the bill
itself and their rows are regenerated, so the document and the journal stay one. Everything else
(invoices, payroll, payments …) belongs to its document and is refused with the reason, as are
locked periods and booked MWST Abrechnungen. `plan` changes nothing; `apply` writes all
changeable Belege at once."""
from __future__ import annotations

import copy
from collections import OrderedDict
from pathlib import Path

from .book import Book, BookError, Row
from .files import write_frontmatter

NO_CODE = "-"           # mwst_neu: remove the code; "" keeps it


def _params(book: Book, konto_alt: str, konto_neu: str, mwst_neu: str) -> tuple[str, str, str]:
    from . import mwst
    konto_alt, konto_neu = (konto_alt or "").strip(), (konto_neu or "").strip()
    mwst_neu = (mwst_neu or "").strip().upper()
    if not konto_neu and not mwst_neu:
        raise BookError("Neues Konto oder neuen MWST-Code angeben")
    if konto_neu:
        if not konto_alt:
            raise BookError("Welches Konto soll ersetzt werden? «Konto von» angeben (z.B. 6500)")
        book.account(konto_alt)
        book.account(konto_neu)
        if konto_alt == konto_neu:
            raise BookError("Altes und neues Konto sind gleich")
    if mwst_neu and mwst_neu != NO_CODE:
        if mwst.config(book)["methode"] == "keine":
            raise BookError("Dieses Buch ist nicht MWST-pflichtig")
        mwst.code(mwst_neu)
    return konto_alt, konto_neu, mwst_neu


def _swap(value: str, alt: str, neu: str) -> str:
    return neu if alt and neu and value == alt else value


def _new_code(current: str, mwst_neu: str) -> str:
    return current if not mwst_neu else ("" if mwst_neu == NO_CODE else mwst_neu)


def _plan_manual(book: Book, beleg: str, rows: list[Row], alt: str, neu: str, code: str) -> list[Row]:
    from . import journal
    problem = journal.edit_problem(book, beleg)
    if problem:
        raise BookError(problem)
    lines = journal.edit_lines(book, rows)
    if alt and not any(alt in (z["soll"], z["haben"]) for z in lines):
        if any(alt in (r.soll, r.haben) for r in rows):
            raise BookError(f"Konto {alt} entsteht hier aus dem MWST-Code — den Code ändern statt des Kontos")
        raise BookError(f"Konto {alt} kommt in {beleg} nicht vor")
    if code and len(lines) > 1:
        raise BookError("Sammelbuchung mit mehreren Zeilen — den MWST-Code im Beleg selbst ändern (✎)")
    new_lines = [{**z, "soll": _swap(z["soll"], alt, neu), "haben": _swap(z["haben"], alt, neu),
                  "mwst": _new_code(z["mwst"], code)} for z in lines]
    if new_lines == lines:
        raise BookError("Keine Änderung")
    _, new = journal.plan_amend(book, beleg, rows[0].datum, rows[0].text, new_lines)
    return new


def _plan_bill(book: Book, nr: str, alt: str, neu: str, code: str) -> tuple[list[Row], dict]:
    from . import journal, kreditoren as kred
    meta = copy.deepcopy(kred.bill(book, nr))
    if meta.get("status") == "storniert":
        raise BookError(f"Kreditor {nr} ist storniert")
    old = kred.booking_rows(book, meta)
    problem = journal.lock_problem(book, old[0].datum)
    if problem:
        raise BookError(problem)
    konto_k = str(meta.get("kreditorenkonto") or kred.KREDITOREN)
    if alt and alt == konto_k:
        raise BookError(f"{alt} ist das Kreditorenkonto der Rechnung — Zahlungen hängen daran, nicht umbuchbar")
    if meta.get("positionen"):
        targets = meta["positionen"]
    else:
        targets = [meta]
    if alt and not any(str(p.get("konto")) == alt for p in targets):
        if any(alt in (r.soll, r.haben) for r in old):
            raise BookError(f"Konto {alt} entsteht hier aus dem MWST-Code — den Code ändern statt des Kontos")
        raise BookError(f"Konto {alt} kommt in {nr} nicht vor")
    for p in targets:
        hit = not alt or str(p.get("konto")) in (alt, neu)
        p["konto"] = _swap(str(p.get("konto") or ""), alt, neu)
        if hit and code:
            p["mwst"] = _new_code(str(p.get("mwst") or "").upper(), code)
    if meta.get("positionen"):
        meta["mwst"] = ""
    meta["fingerprint"] = kred.fingerprint(meta)
    new = kred.booking_rows(book, meta)
    if sorted((r.soll, r.haben, r.betrag, r.mwst) for r in new) == sorted((r.soll, r.haben, r.betrag, r.mwst) for r in old):
        raise BookError("Keine Änderung")
    journal.validate_rows(book, new, replacing=nr)
    problem = journal.mwst_filed_problem(book, old + new, nr)
    if problem:
        raise BookError(problem)
    return new, meta


def plan(book: Book, belege: list[str], konto_alt: str = "", konto_neu: str = "", mwst_neu: str = "") -> list[dict]:
    """Per Beleg: {beleg, art, datum, text, vorher, nachher, problem}. Writes nothing."""
    alt, neu, code = _params(book, konto_alt, konto_neu, mwst_neu)
    by_beleg: "OrderedDict[str, list[Row]]" = OrderedDict()
    for r in book.rows:
        by_beleg.setdefault(r.beleg, []).append(r)
    out = []
    for beleg in dict.fromkeys(b.strip() for b in belege if b and b.strip()):
        rows = by_beleg.get(beleg)
        item = {"beleg": beleg, "art": "", "datum": rows[0].datum if rows else None,
                "text": rows[0].text if rows else "", "vorher": rows or [], "nachher": [], "problem": None}
        try:
            if not rows:
                raise BookError(f"Beleg {beleg} nicht gefunden")
            kind, _, ref = rows[0].quelle.partition(":")
            if not rows[0].quelle:
                item["art"] = "manuell"
                item["nachher"] = _plan_manual(book, beleg, rows, alt, neu, code)
            elif kind == "kreditor":
                item["art"] = "kreditor"
                item["nachher"], item["_meta"] = _plan_bill(book, ref, alt, neu, code)
            else:
                raise BookError(f"Gehört zu {kind} {ref} — dort ändern")
        except BookError as exc:
            item["problem"] = str(exc)
        out.append(item)
    if not out:
        raise BookError("Keine Belege ausgewählt")
    return out


def apply(book: Book, belege: list[str], konto_alt: str = "", konto_neu: str = "",
          mwst_neu: str = "") -> tuple[list[dict], list[dict], list[Path]]:
    """Recode every Beleg of the selection that can be recoded, in one go.
    Returns (changed, skipped, touched files)."""
    items = plan(book, belege, konto_alt, konto_neu, mwst_neu)
    ok = [i for i in items if not i["problem"]]
    skipped = [i for i in items if i["problem"]]
    if not ok:
        reasons = "; ".join(f"{i['beleg']}: {i['problem']}" for i in skipped[:5])
        raise BookError(f"Nichts umzubuchen — {reasons}")
    manual = {i["beleg"] for i in ok if i["art"] == "manuell"}
    bills = {f"kreditor:{i['_meta']['nummer']}" for i in ok if i["art"] == "kreditor"}
    touched = book.remove_rows(lambda r: (not r.quelle and r.beleg in manual) or r.quelle in bills)
    touched += book.add_rows([r for i in ok for r in i["nachher"]])
    for i in ok:
        if i["art"] == "kreditor":
            meta = i.pop("_meta")
            path, text = meta.pop("_pfad"), meta.pop("_text", "")
            write_frontmatter(path, meta, text)
            touched.append(path)
    return ok, skipped, touched

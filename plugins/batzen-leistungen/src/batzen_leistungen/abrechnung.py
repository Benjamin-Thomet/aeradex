"""Billing: filter open entries, preview the invoice, issue it through the core.

One invoice per customer. Positions are either one line per entry (`detail`) or
grouped (hours per person and rate, products per product and price). The invoice
is a normal batzen invoice (QR-bill, Debitor 1100 an Ertrag, MWST); the entries
get its number, all inside one `api.write` → one check, one commit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from batzen import api, invoices
from batzen import mwst as vat
from batzen import pdf as core_pdf
from batzen.book import Book, BookError
from batzen.files import parse_date

from . import daten

ZERO = Decimal("0")


def _d(value) -> date | None:
    return parse_date(value, "datum") if value not in (None, "") else None


def select(book: Book, kunde: str = "", projekt: str = "", von=None, bis=None, art: str = "", wer: str = "",
           status: str = "offen", ids: list[str] | None = None) -> list[dict]:
    """Entries with their state, filtered. status: offen | abgerechnet | pauschal | intern | '' (all)."""
    von, bis = _d(von), _d(bis)
    wanted = set(ids or [])
    out = []
    for e in daten.with_state(book):
        if wanted and e["id"] not in wanted:
            continue
        if status and e["status"] != status:
            continue
        if kunde and e["kunde"] != kunde.upper():
            continue
        if projekt and e["projekt"] != projekt.upper():
            continue
        if art and e["art"] != art:
            continue
        if wer and e["wer"] != wer.upper():
            continue
        if von and e["datum"] < von or bis and e["datum"] > bis:
            continue
        out.append(e)
    return sorted(out, key=lambda e: (e["datum"], e["id"]))


def summary(book: Book, bis=None) -> list[dict]:
    """Open billable work per customer (and project): hours, products, amount."""
    names = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    projs = daten.projects(book)
    per: dict[tuple[str, str], dict] = {}
    for e in select(book, bis=bis):
        key = (e["kunde"], e["projekt"])
        s = per.setdefault(key, {"kunde": e["kunde"], "kunde_name": names.get(e["kunde"], e["kunde"]),
                                 "projekt": e["projekt"],
                                 "projekt_name": projs.get(e["projekt"], {}).get("name", "") if e["projekt"] else "",
                                 "stunden": ZERO, "produkte": ZERO, "betrag": ZERO, "eintraege": 0,
                                 "von": e["datum"], "bis": e["datum"]})
        s["stunden" if e["art"] == "Zeit" else "produkte"] += e["betrag"] if e["art"] == "Produkt" else e["menge"]
        s["betrag"] += e["betrag"]
        s["eintraege"] += 1
        s["von"], s["bis"] = min(s["von"], e["datum"]), max(s["bis"], e["datum"])
    return sorted(per.values(), key=lambda s: (s["kunde"], s["projekt"]))


def _period(items: list[dict]) -> str:
    a, b = min(e["datum"] for e in items), max(e["datum"] for e in items)
    return a.strftime("%d.%m.%Y") if a == b else f"{a.strftime('%d.%m.%Y')}–{b.strftime('%d.%m.%Y')}"


def positions(book: Book, items: list[dict], detail: bool = False) -> list[dict]:
    """Invoice positions (raw, for `invoices.normalize_positions`) for the given entries."""
    cfg = daten.settings(book)
    taxed = vat.config(book)["methode"] != "keine"
    names = {k: p["name"] for k, p in daten.people(book).items()}
    prods = daten.products(book)
    projs = daten.projects(book)
    out = []
    times = [e for e in items if e["art"] == "Zeit"]
    goods = [e for e in items if e["art"] == "Produkt"]

    def tax(code):
        return {"mwst": code} if taxed and code else {}

    if detail:
        for e in times:
            who = names.get(e["wer"], e["wer"])
            out.append({"text": f"{e['datum'].strftime('%d.%m.%Y')} {who}: {e['text']}", "menge": e["menge"],
                        "einheit": "h", "preis": e["preis"], "konto": cfg["konto_stunden"]})
        for e in goods:
            p = prods.get(e["produkt"], {})
            out.append({"text": f"{e['datum'].strftime('%d.%m.%Y')} {e['text']}", "menge": e["menge"],
                        "einheit": p.get("einheit") or "", "preis": e["preis"],
                        "konto": p.get("konto") or cfg["konto_produkte"], **tax(p.get("mwst"))})
        return out
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in times:
        groups[(e["projekt"], e["wer"], e["preis"])].append(e)
    for (proj, wer, preis), group in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        label = f"Arbeit {names.get(wer, wer)}"
        if proj:
            label += f", {projs.get(proj, {}).get('name', proj)}"
        out.append({"text": f"{label} ({_period(group)})", "menge": sum((e["menge"] for e in group), ZERO),
                    "einheit": "h", "preis": preis, "konto": cfg["konto_stunden"]})
    goods_groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in goods:
        goods_groups[(e["produkt"], e["preis"])].append(e)
    for (nr, preis), group in sorted(goods_groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        p = prods.get(nr, {})
        out.append({"text": p.get("text") or group[0]["text"], "menge": sum((e["menge"] for e in group), ZERO),
                    "einheit": p.get("einheit") or "", "preis": preis,
                    "konto": p.get("konto") or cfg["konto_produkte"], **tax(p.get("mwst"))})
    return out


def totals(book: Book, raw: list[dict]) -> dict:
    cfg = vat.config(book)
    pos = invoices.normalize_positions(raw, book.settings.konto("ertrag"), "U81" if cfg["methode"] != "keine" else "")
    netto = sum((p["betrag"] for p in pos), ZERO)
    breakdown = invoices.mwst_breakdown(pos)
    return {"positionen": pos, "netto": netto, "mwst": breakdown,
            "total": netto + sum((b["steuer"] for b in breakdown), ZERO)}


def _billable(book: Book, ids: list[str]) -> list[dict]:
    if not ids:
        raise BookError("Keine Einträge ausgewählt")
    items = select(book, status="", ids=ids)
    found = {e["id"] for e in items}
    missing = [i for i in ids if i not in found]
    if missing:
        raise BookError(f"Nicht gefunden: {', '.join(missing)}")
    wrong = [f"{e['id']} ({e['status']})" for e in items if e["status"] != "offen"]
    if wrong:
        raise BookError(f"Nicht offen: {', '.join(wrong)}")
    customers = {e["kunde"] for e in items}
    if len(customers) != 1:
        raise BookError(f"Eine Rechnung geht an einen Kunden — ausgewählt: {', '.join(sorted(customers))}")
    return items


def preview(book: Book, ids: list[str], detail: bool = False) -> dict:
    items = _billable(book, ids)
    return {"kunde": items[0]["kunde"], "eintraege": len(items), **totals(book, positions(book, items, detail))}


def bill(book: Book, ids: list[str], detail: bool = False, datum=None, text: str = "",
         zahlungsfrist: int | None = None, rapport: bool = True) -> tuple[dict, list[Path]]:
    """Issue the invoice for the entries (QR-bill PDF, booked as a Debitor) and mark them billed.
    With `rapport` a Leistungsrapport (every entry) is written next to the invoice PDF."""
    items = _billable(book, ids)
    kunde = items[0]["kunde"]
    raw = positions(book, items, detail)
    if not text:
        text = f"Unsere Leistungen vom {_period(items)}."
    meta, touched = invoices.issue_invoice(book, kunde, raw, datum, text, zahlungsfrist)
    meta["_text"] = text
    cust = invoices.customer(book, kunde)
    pdf_path = api.meta_pdf_path(book, meta)
    pdf_path.write_bytes(core_pdf.invoice_pdf(book, meta, cust))
    touched.append(pdf_path)
    if rapport:
        from .pdf import rapport_pdf
        rp = pdf_path.with_name(f"{meta['nummer']}-rapport.pdf")
        rp.write_bytes(rapport_pdf(book, meta, items))
        touched.append(rp)
    touched += daten.mark_billed(book, [e["id"] for e in items], meta["nummer"])
    meta.pop("_text", None)
    return {"rechnung": meta["nummer"], "kunde": kunde, "total": meta["total"], "eintraege": len(items),
            "pdf": book.rel(pdf_path)}, touched


def mismatches(book: Book) -> list[str]:
    """Invoices whose billed entries no longer add up to the invoice (an entry was changed by hand)."""
    status, _ = daten.context_maps(book)
    per: dict[str, list[dict]] = defaultdict(list)
    for e in daten.entries(book):
        if e["rechnung"] and status.get(e["rechnung"]) not in (None, "storniert") and e["abrechenbar"]:
            per[e["rechnung"]].append(e)
    if not per:
        return []
    all_invoices = invoices.invoices(book)
    out = []
    for nr, items in sorted(per.items()):
        meta = all_invoices[nr]
        netto = sum((daten.money(p["betrag"]) for p in meta.get("positionen") or []), ZERO)
        if {e["kunde"] for e in items} != {meta.get("kunde")}:
            out.append(f"{nr}: Leistungen eines anderen Kunden markiert")
            continue
        try:
            expected = {totals(book, positions(book, items, d))["netto"] for d in (False, True)}
        except BookError:
            expected = set()
        if netto not in expected:
            out.append(f"{nr}: Leistungen ergeben {', '.join(f'{x:.2f}' for x in sorted(expected))}, "
                       f"die Rechnung {netto:.2f} — wurde ein abgerechneter Eintrag von Hand geändert?")
    return out

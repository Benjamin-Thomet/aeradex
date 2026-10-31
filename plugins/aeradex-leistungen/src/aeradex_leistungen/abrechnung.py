"""Billing: filter open entries, preview the invoice, issue it through the core.

One invoice per customer. Positions are either one line per entry (`detail`) or
grouped (hours per person and rate, products per product and price). The invoice
is a normal aeradex invoice (QR-bill, Debitor 1100 an Ertrag, MWST); the entries
get its number, all inside one `api.write` → one check, one commit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from aeradex import api, invoices
from aeradex import mwst as vat
from aeradex import pdf as core_pdf
from aeradex.book import Book, BookError
from aeradex.files import parse_date

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
        s.setdefault("ids", []).append(e["id"])
    from . import kontrolle
    today = date.today()
    for s in per.values():
        s["alter_tage"] = (today - s["von"]).days
        s["budget"] = kontrolle.project(book, s["projekt"]).get("auslastung") if s["projekt"] else None
    return sorted(per.values(), key=lambda s: (-s["betrag"], s["kunde"], s["projekt"]))


def groups(book: Book, bis=None, je: str = "kunde") -> list[dict]:
    """Open billable work bundled into the invoices «Alle abrechnen» would issue: one per customer, or one per
    customer and project (`je="projekt"`)."""
    out: dict[tuple, dict] = {}
    for s in summary(book, bis):
        key = (s["kunde"], s["projekt"] if je == "projekt" else "")
        g = out.setdefault(key, {"kunde": s["kunde"], "kunde_name": s["kunde_name"], "projekt": key[1],
                                 "projekt_name": s["projekt_name"] if key[1] else "", "ids": [], "betrag": ZERO})
        g["ids"] += s["ids"]
        g["betrag"] += s["betrag"]
    return list(out.values())


def bill_all(book: Book, bis=None, je: str = "kunde", datum=None, rapport: bool = True) -> tuple[dict, list[Path]]:
    """Month end: issue every open invoice at once (see `groups`), each with its Leistungsrapport. One write."""
    from .entwuerfe import in_drafts
    held = in_drafts(book)                  # work in a draft waits for the person completing it
    todo = [{**g, "ids": [i for i in g["ids"] if i not in held]} for g in groups(book, bis, je)]
    todo = [g for g in todo if g["ids"]]
    if not todo:
        raise BookError("Nichts offen zum Abrechnen")
    issued, touched = [], []
    for g in todo:
        res, paths = bill(book, g["ids"], False, datum, "", None, rapport)
        issued.append(res)
        touched += paths
    total = sum((Decimal(str(r["total"])) for r in issued), ZERO)
    # month end is not the Schlussrechnung: Akontos stay open, but nobody should miss them
    warn = [r["warnung"] for r in issued if r.get("warnung")]
    return {"rechnungen": issued, "anzahl": len(issued), "total": total, "warnungen": warn}, touched


def _period(items: list[dict]) -> str:
    a, b = min(e["datum"] for e in items), max(e["datum"] for e in items)
    return a.strftime("%d.%m.%Y") if a == b else f"{a.strftime('%d.%m.%Y')}–{b.strftime('%d.%m.%Y')}"


def _descriptions(group: list[dict], skip: set[str]) -> list[str]:
    """«13.10. Abdecken, Wände grundiert» per day: every different text of that day once (two people on the same
    job: one of them describes it), without the bare name of the Leistungsart; days nobody described are left out."""
    days: dict[date, list[str]] = defaultdict(list)
    for e in sorted(group, key=lambda e: (e["datum"], e["id"])):
        text = " ".join(str(e.get("text") or "").split())
        days[e["datum"]]            # keep the day in order even when it has no text
        if text and text not in skip and text not in days[e["datum"]]:
            days[e["datum"]].append(text)
    runs: list[tuple[date, date, list[str]]] = []          # days in a row with the same text: one line
    for d, texts in sorted(days.items()):
        if not texts:
            continue
        if runs and runs[-1][2] == texts:
            runs[-1] = (runs[-1][0], d, texts)
        else:
            runs.append((d, d, texts))
    return [f"{a.strftime('%d.%m.')}{'–' + b.strftime('%d.%m.') if b != a else ''} {'; '.join(texts)}"
            for a, b, texts in runs]


def undescribed_days(book: Book, items: list[dict]) -> list[dict]:
    """Billable working days (per customer and project) on which nobody wrote what was done — with
    `rechnung_tage` the invoice would show no line for them. → [{datum, kunde, projekt, ids}]"""
    prods = daten.products(book)
    days: dict[tuple, list[dict]] = defaultdict(list)
    for e in items:
        if e["art"] == "Zeit" and e.get("abrechenbar") and e.get("kunde"):
            days[(e["datum"], e["kunde"], e.get("projekt") or "")].append(e)
    out = []
    for (d, kunde, projekt), group in sorted(days.items()):
        skip = {prods.get(e.get("produkt") or "", {}).get("text") for e in group} - {None}
        if not _descriptions(group, skip):
            out.append({"datum": d, "kunde": kunde, "projekt": projekt, "ids": [e["id"] for e in group]})
    return out


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

    def service(e):
        """konto and MWST code of the entry's Leistungsart (or the hours account)."""
        art = prods.get(e.get("produkt") or "", {})
        return {"konto": art.get("konto") or cfg["konto_stunden"], **tax(art.get("mwst"))}

    if detail:
        for e in times:
            who = names.get(e["wer"], e["wer"])
            out.append({"text": f"{e['datum'].strftime('%d.%m.%Y')} {who}: {e['text']}", "menge": e["menge"],
                        "einheit": "h", "preis": e["preis"], **service(e)})
        for e in goods:
            p = prods.get(e["produkt"], {})
            out.append({"text": f"{e['datum'].strftime('%d.%m.%Y')} {e['text']}", "menge": e["menge"],
                        "einheit": p.get("einheit") or "", "preis": e["preis"],
                        "konto": p.get("konto") or cfg["konto_produkte"], **tax(p.get("mwst"))})
        return out
    # Hours with a Leistungsart are grouped by it («Malerarbeiten 12.5 h à 95»); hours without one per person,
    # as always — so invoices issued before Leistungsarten existed still add up the same (mismatches()).
    # `rechnung_tage`: never per person; the position lists the days with what was done below its title.
    by_day = cfg["rechnung_tage"]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in times:
        art = e.get("produkt") or ""
        groups[(e["projekt"], art, "" if art or by_day else e["wer"], e["preis"])].append(e)
    # `rechnung_tage`: the days' descriptions once per object and Leistungsart — several rates (people) of the same
    # work are further positions named after their people, without repeating the descriptions
    work: dict[tuple, list[tuple]] = defaultdict(list)
    for key, group in groups.items():
        work[key[:2]].append((min(e["datum"] for e in group), str(key[3]), key, group))
    ordered = [(key, group, i, len(rows)) for rows in work.values()
               for i, (_, _, key, group) in enumerate(sorted(rows, key=lambda r: (r[0], r[1])))]
    ordered.sort(key=lambda r: (str(r[0][0]), str(r[0][1]), r[2]))
    for (proj, art, wer, preis), group, index, count in ordered:
        art_text = prods.get(art, {}).get("text") or art if art else ""
        label = art_text or (f"Arbeit {names.get(wer, wer)}" if wer else "Arbeiten")
        if by_day and count > 1:
            label += " " + ", ".join(sorted({names.get(e["wer"], e["wer"]) for e in group}))
        if proj:
            label += f", {projs.get(proj, {}).get('name', proj)}"
        text = f"{label} ({_period(group)})"
        if by_day and index == 0:
            everyone = [e for g in work[(proj, art)] for e in g[3]]
            text = "\n".join([text] + _descriptions(everyone, {art_text} - {""}))
        out.append({"text": text, "menge": sum((e["menge"] for e in group), ZERO),
                    "einheit": "h", "preis": preis, **service(group[0])})
    goods_groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in goods:
        goods_groups[(e["produkt"], e["preis"])].append(e)
    for (nr, preis), group in sorted(goods_groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        p = prods.get(nr, {})
        out.append({"text": p.get("text") or group[0]["text"], "menge": sum((e["menge"] for e in group), ZERO),
                    "einheit": p.get("einheit") or "", "preis": preis,
                    "konto": p.get("konto") or cfg["konto_produkte"], **tax(p.get("mwst"))})
    return out


def totals(book: Book, raw: list[dict], kunde: str = "", rabatt=None, akonto=None) -> dict:
    cfg = vat.config(book)
    pos = invoices.normalize_positions(raw, book.settings.konto("ertrag"), "U81" if cfg["methode"] != "keine" else "")
    pct = invoices._rabatt_percent(rabatt)
    if pct:
        pos += invoices.rabatt_positions(book, pos, pct)
    if kunde and akonto:
        cur = (book.settings.get("waehrung") or "CHF").upper()
        for p in invoices.akonto_positions(book, kunde, list(akonto), cur):
            if not p["mwst"]:
                p.pop("mwst")
            pos.append(p)
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


def preview(book: Book, ids: list[str], detail: bool = False, rabatt=None) -> dict:
    """The invoice the entries would make. Open Akontorechnungen of the customer are taken off, as `bill`
    does unless they are unticked."""
    items = _billable(book, ids)
    kunde = items[0]["kunde"]
    leer = undescribed_days(book, items) if daten.settings(book)["rechnung_tage"] and not detail else []
    offen = invoices.open_akonto(book, kunde)
    return {"kunde": kunde, "eintraege": len(items), "ohne_beschreibung": leer, "offen_akonto": offen,
            **totals(book, positions(book, items, detail), kunde, rabatt, [a["nummer"] for a in offen])}


def object_line(book: Book, items: list[dict]) -> str:
    """When all entries belong to one project (object): «MFH Bernstrasse, Bernstrasse 12, 3600 Thun» for the
    invoice's Objekt line; else ""."""
    nrs = {e["projekt"] for e in items}
    if len(nrs) != 1 or not next(iter(nrs)):
        return ""
    p = daten.projects(book).get(next(iter(nrs)))
    if not p:
        return ""
    return daten.object_text(p)


def offer_of(book: Book, items: list[dict]) -> dict | None:
    """The offer behind the entries' one project (its `offerte`), as the invoice's «Ihre Offerte vom»."""
    nrs = {e["projekt"] for e in items}
    if len(nrs) != 1 or not next(iter(nrs)):
        return None
    nr = str((daten.projects(book).get(next(iter(nrs))) or {}).get("offerte") or "").strip()
    if not nr:
        return None
    from . import offerten
    q = offerten.quotes(book).get(nr)
    return {"nummer": nr, **({"datum": str(q["datum"])} if q and q.get("datum") else {})}


def bill(book: Book, ids: list[str], detail: bool = False, datum=None, text: str = "",
         zahlungsfrist: int | None = None, rapport: bool = True, rabatt=None,
         akonto=None, objekt: str | None = None, offerte=None) -> tuple[dict, list[Path]]:
    """Issue the invoice for the entries (QR-bill PDF, booked as a Debitor) and mark them billed.
    With `rapport` a Leistungsrapport (every entry) is written next to the invoice PDF. `rabatt` in percent;
    `akonto` the Akontorechnungen it credits (core `invoices.issue_invoice`). `objekt`/`offerte` default to the
    entries' project and its offer. The period of the entries is the invoice's Leistungszeitraum."""
    items = _billable(book, ids)
    kunde = items[0]["kunde"]
    raw = positions(book, items, detail)
    days = sorted(str(e["datum"]) for e in items)
    if not text and not core_pdf.brief(book):        # the letter layout shows the period as «Ausführung»
        text = f"Unsere Leistungen vom {_period(items)}."
    meta, touched = invoices.issue_invoice(book, kunde, raw, datum, text, zahlungsfrist, rabatt=rabatt,
                                           akonto=list(akonto or []),
                                           objekt=object_line(book, items) if objekt is None else objekt,
                                           leistung_von=days[0], leistung_bis=days[-1] if days[-1] != days[0] else None,
                                           offerte=offer_of(book, items) if offerte is None else offerte)
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
    out = {"rechnung": meta["nummer"], "kunde": kunde, "total": meta["total"], "eintraege": len(items),
           "pdf": book.rel(pdf_path)}
    if warn := invoices.unanrechnet_warnung(book, kunde, meta):
        out["warnung"] = warn
    return out, touched


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
        # the entries make the positions; a discount or an Akonto taken off comes on top of them
        netto = sum((daten.money(p["betrag"]) for p in meta.get("positionen") or []
                     if not p.get("rabatt") and not p.get("akonto")), ZERO)
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

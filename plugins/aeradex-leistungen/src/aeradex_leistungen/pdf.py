"""PDFs: the quote (Offerte, on the letterhead like an invoice, without payment slip)
and the Leistungsrapport that goes with an invoice made from recorded work."""
from __future__ import annotations

import io
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Spacer, Table

from aeradex.book import Book
from aeradex.pdf import ADDRESS_TOP, SIDE, TOP, P, _doc, _grid, _letterhead, chf, d

from . import daten


def _address(meta: dict) -> list[str]:
    a = meta.get("an") or {}
    lines = [a.get("name")] + ([a.get("zusatz")] if a.get("zusatz") else []) + [
        " ".join(x for x in (a.get("strasse"), a.get("nr")) if x),
        " ".join(x for x in (a.get("plz"), a.get("ort")) if x)]
    if a.get("land") and a.get("land") != "CH":
        lines.append(a["land"])
    return [l for l in lines if l]


def _info(rows: list[tuple[str, str]]) -> Table:
    return Table([[P(k, "small"), P(v)] for k, v in rows], colWidths=[34 * mm, 80 * mm], hAlign="LEFT",
                 style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0.5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5)])


def _head(book: Book, meta: dict, title: str, width: float) -> list:
    story = [_letterhead(book, width), Spacer(1, max(0, ADDRESS_TOP - TOP - 22 * mm))]
    story.append(Table([[None, [P(l) for l in _address(meta)]]], colWidths=[width * 0.55, width * 0.45],
                       style=[("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 14 * mm), P(title, "title")]
    return story


def quote_pdf(book: Book, meta: dict) -> bytes:
    s = book.settings
    width = A4[0] - 2 * SIDE
    title = f"Offerte {meta['nummer']}" + (f" · {meta['titel']}" if meta.get("titel") else "")
    story = _head(book, meta, title, width)
    story.append(_info([("Datum", d(meta["datum"])), ("Gültig bis", d(meta["gueltig_bis"])),
                        ("Kundennummer", meta.get("kunde") or "")]))
    if meta.get("_text"):
        story += [Spacer(1, 6 * mm)] + [P(p) for p in meta["_text"].split("\n\n") if p.strip()]
    story.append(Spacer(1, 6 * mm))
    cur = meta.get("waehrung") or "CHF"
    data = [["Position", "Menge", "Preis", f"Betrag {cur}"]]
    for p in meta["positionen"]:
        menge = f"{Decimal(str(p['menge'])).normalize():f} {p.get('einheit') or ''}".strip()
        data.append([P(p["text"], "cell"), menge, chf(p["preis"]), chf(p["betrag"])])
    sums = []
    if meta.get("mwst"):
        data.append(["Total exkl. MWST", "", "", chf(meta.get("netto"))])
        sums.append(len(data) - 1)
        for b in meta["mwst"]:
            rate = Decimal(str(b.get("satz") or 0))
            label = (f"MWST {rate:g} % auf {chf(b['netto'])}" if rate else
                     {"U0": "Steuerbefreite Leistung", "UA": "Von der MWST ausgenommen"}.get(b["code"], b["code"]))
            data.append([label, "", "", chf(b["steuer"]) if rate else ""])
    data.append([f"Total {cur}" + (" inkl. MWST" if meta.get("mwst") else ""), "", "", chf(meta["total"])])
    story.append(_grid(data, [width * 0.52, width * 0.14, width * 0.16, width * 0.18],
                       total_rows=sums + [len(data) - 1], right_cols=(1, 2, 3)))
    if meta.get("abrechnung") == "aufwand":
        story += [Spacer(1, 3 * mm), P("Abrechnung nach effektivem Aufwand; die Offerte ist eine Schätzung.", "small")]
    story += [Spacer(1, 6 * mm),
              P(s.get("offerte_gruss") or "Wir freuen uns auf Ihren Auftrag und stehen für Fragen gerne zur Verfügung.")]
    buf = io.BytesIO()
    _doc(buf, f"Offerte {meta['nummer']}", s.firma, label=f"Offerte {meta['nummer']}").build(story)
    return buf.getvalue()


def rapport_pdf(book: Book, invoice: dict, items: list[dict]) -> bytes:
    """Every entry behind an invoice: date, who, what, quantity, price, amount."""
    s = book.settings
    width = A4[0] - 2 * SIDE
    names = {k: p["name"] for k, p in daten.people(book).items()}
    projs = daten.projects(book)
    story = _head(book, invoice, f"Leistungsrapport zu Rechnung {invoice['nummer']}", width)
    story += [_info([("Rechnungsdatum", d(invoice["datum"])), ("Kundennummer", invoice.get("kunde") or "")]),
              Spacer(1, 6 * mm)]
    data = [["Datum", "Wer", "Leistung", "Menge", "Preis", "Betrag"]]
    for e in sorted(items, key=lambda e: (e["datum"], e["id"])):
        what = e["text"] + (f" ({projs[e['projekt']]['name']})" if e.get("projekt") in projs else "")
        unit = "h" if e["art"] == "Zeit" else ""
        data.append([e["datum"].strftime("%d.%m.%Y"), names.get(e["wer"], e["wer"]), P(what, "cell"),
                     f"{e['menge'].normalize():f} {unit}".strip(), chf(e["preis"]), chf(daten.amount(e))])
    hours = sum((e["menge"] for e in items if e["art"] == "Zeit"), Decimal("0"))
    data.append([f"Total ({hours.normalize():f} h)", "", "", "", "", chf(sum((daten.amount(e) for e in items), Decimal("0")))])
    story.append(_grid(data, [width * 0.13, width * 0.17, width * 0.36, width * 0.1, width * 0.1, width * 0.14],
                       total_rows=[len(data) - 1], right_cols=(3, 4, 5), zebra=True))
    story += [Spacer(1, 3 * mm), P("Beträge exkl. MWST.", "small")]
    buf = io.BytesIO()
    _doc(buf, f"Leistungsrapport {invoice['nummer']}", s.firma, label=f"Leistungsrapport {invoice['nummer']}").build(story)
    return buf.getvalue()

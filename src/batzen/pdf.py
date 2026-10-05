"""PDF documents: invoices with Swiss QR-bill, payslips, Jahresrechnung,
Kontoblatt, Journal and Debitorenliste.

Plain Swiss business documents on the company's own letterhead; batzen puts
no mark of its own on anything that leaves the house.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

from . import payroll
from . import qrbill_ch as qr
from .book import MONTHS_DE, Book
from .files import parse_date

INK = colors.HexColor("#1c1917")
MUTED = colors.HexColor("#78716c")
RULE = colors.HexColor("#d6d3d1")
SOFT = colors.HexColor("#f5f5f4")

SIDE = 20 * mm
TOP = 20 * mm
BOTTOM = 18 * mm
ADDRESS_TOP = 48 * mm      # lines up with the window of a C5/C6 envelope (left window)
SLIP_HEIGHT = 105 * mm
SLIP_RESERVE = 118 * mm

ST = {
    "base": ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=INK),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "sender": ParagraphStyle("sender", fontName="Helvetica", fontSize=7, leading=9, textColor=MUTED),
    "firm": ParagraphStyle("firm", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=INK, alignment=TA_RIGHT),
    "firmline": ParagraphStyle("firmline", fontName="Helvetica", fontSize=8.5, leading=11, textColor=MUTED, alignment=TA_RIGHT),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=INK, spaceAfter=4),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=INK, spaceBefore=10, spaceAfter=4),
    "right": ParagraphStyle("right", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK, alignment=TA_RIGHT),
    "bold": ParagraphStyle("bold", fontName="Helvetica-Bold", fontSize=9.5, leading=13, textColor=INK),
    "boldright": ParagraphStyle("boldright", fontName="Helvetica-Bold", fontSize=9.5, leading=13, textColor=INK, alignment=TA_RIGHT),
}


def chf(value, blank_zero: bool = False) -> str:
    v = Decimal(str(value or 0))
    if not v:
        if blank_zero:
            return ""
        v = abs(v)
    return f"{v:,.2f}".replace(",", "'")


def d(value) -> str:
    return parse_date(value).strftime("%d.%m.%Y") if value else ""


def P(text, style="base"):
    return Paragraph(escape(str(text)) if not str(text).startswith("<") else str(text), ST[style])


def _footer(canvas, doc, label: str, y: float = 10 * mm, rule: bool = True):
    canvas.saveState()
    if rule:
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(SIDE, y + 4 * mm, A4[0] - SIDE if doc.pagesize == A4 else doc.pagesize[0] - SIDE, y + 4 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(SIDE, y, label)
    canvas.drawRightString(doc.pagesize[0] - SIDE, y, f"Seite {doc.page}")
    canvas.restoreState()


def _letterhead(book: Book, width: float) -> Table:
    s = book.settings
    sender = " · ".join([s.firma] + s.address_lines)
    right = [[P(s.firma, "firm")]] + [[P(l, "firmline")] for l in s.address_lines]
    if s.get("telefon"):
        right.append([P(s.get("telefon"), "firmline")])
    uid = (s.get("uid") or "").strip()
    if uid:
        label = "MWST-Nr." if uid.upper().endswith("MWST") else "UID"
        right.append([P(f"{label} {uid}", "firmline")])
    flush = TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                        ("VALIGN", (0, 0), (-1, -1), "TOP")])
    t = Table([[P(sender, "sender"), Table(right, colWidths=[width * 0.45], style=flush)]],
              colWidths=[width * 0.55, width * 0.45])
    t.setStyle(flush)
    return t


def _grid(data, widths, header=True, total_rows=(), zebra=False, right_cols=()):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5), ("LEFTPADDING", (0, 0), (-1, -1), 3),
             ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    for c in right_cols:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if header:
        style += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK)]
    for r in total_rows:
        style += [("FONT", (0, r), (-1, r), "Helvetica-Bold", 8.5), ("LINEABOVE", (0, r), (-1, r), 0.6, INK)]
    if zebra:
        for r in range(1 if header else 0, len(data)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), SOFT))
    t.setStyle(TableStyle(style))
    return t


def _doc(buf, title: str, author: str, pagesize=A4, label: str = "") -> BaseDocTemplate:
    doc = BaseDocTemplate(buf, pagesize=pagesize, leftMargin=SIDE, rightMargin=SIDE,
                          topMargin=TOP, bottomMargin=BOTTOM, title=title, author=author)
    w, h = pagesize
    frame = Frame(SIDE, BOTTOM, w - 2 * SIDE, h - TOP - BOTTOM, 0, 0, 0, 0)
    doc.addPageTemplates([PageTemplate("main", [frame], onPage=lambda c, dd: _footer(c, dd, label or title))])
    return doc


# ---------- Invoice ----------

def invoice_pdf(book: Book, meta: dict, customer: dict | None) -> bytes:
    s = book.settings
    width = A4[0] - 2 * SIDE
    a = meta.get("an") or {}
    lines = [a.get("name")] + ([a.get("zusatz")] if a.get("zusatz") else []) + [
        " ".join(x for x in (a.get("strasse"), a.get("nr")) if x),
        " ".join(x for x in (a.get("plz"), a.get("ort")) if x)]
    if a.get("land") and a.get("land") != "CH":
        lines.append(a["land"])

    def letter():
        story = [_letterhead(book, width)]
        story.append(Spacer(1, max(0, ADDRESS_TOP - TOP - 22 * mm)))
        story.append(Table([[None, [P(l) for l in lines if l]]], colWidths=[width * 0.55, width * 0.45],
                           style=[("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story += [Spacer(1, 14 * mm), P(f"Rechnung {meta['nummer']}", "title")]
        info = [["Rechnungsdatum", d(meta["datum"])], ["Zahlbar bis", d(meta["faellig"])],
                ["Kundennummer", meta.get("kunde") or ""]]
        if meta.get("referenz"):
            info.append(["Referenz", qr.format_reference(meta["referenz"])])
        story.append(Table([[P(k, "small"), P(v)] for k, v in info], colWidths=[34 * mm, 80 * mm],
                           hAlign="LEFT", style=[("LEFTPADDING", (0, 0), (-1, -1), 0),
                                                 ("TOPPADDING", (0, 0), (-1, -1), 0.5),
                                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5)]))
        if meta.get("_text"):
            story += [Spacer(1, 6 * mm)] + [P(p) for p in meta["_text"].split("\n\n") if p.strip()]
        story.append(Spacer(1, 6 * mm))
        data = [["Position", "Menge", "Preis", f"Betrag {meta.get('waehrung', 'CHF')}"]]
        for p in meta["positionen"]:
            menge = f"{Decimal(str(p['menge'])).normalize():f} {p.get('einheit') or ''}".strip()
            data.append([P(p["text"], "cell"), menge, chf(p["preis"]), chf(p["betrag"])])
        cur = meta.get('waehrung', 'CHF')
        sums = []
        if meta.get("mwst"):
            data.append([f"Total exkl. MWST", "", "", chf(meta.get("netto"))])
            sums.append(len(data) - 1)
            for b in meta["mwst"]:
                rate = Decimal(str(b.get("satz") or 0))
                label = (f"MWST {rate:g} % auf {chf(b['netto'])}" if rate else
                         {"U0": "Steuerbefreite Leistung", "UA": "Von der MWST ausgenommen"}.get(b["code"], b["code"]))
                data.append([label, "", "", chf(b["steuer"]) if rate else ""])
        data.append([f"Total {cur}" + (" inkl. MWST" if meta.get("mwst") else ""), "", "", chf(meta["total"])])
        story.append(_grid(data, [width * 0.52, width * 0.14, width * 0.16, width * 0.18],
                           total_rows=sums + [len(data) - 1], right_cols=(1, 2, 3)))
        uid = (s.get("uid") or "").upper()
        if meta.get("mwst") and uid:
            story += [Spacer(1, 3 * mm), P(f"MWST-Nr. {s.get('uid')}", "small")]
        elif s.get("ohne_mwst_hinweis", True) and not uid.endswith("MWST") and not meta.get("mwst"):
            story += [Spacer(1, 3 * mm), P("Nicht MWST-pflichtig, es wird keine Mehrwertsteuer erhoben.", "small")]
        story += [Spacer(1, 6 * mm), P(s.get("rechnung_gruss") or "Besten Dank für Ihren Auftrag.")]
        return story

    def make(first: str, buf):
        doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=SIDE, rightMargin=SIDE, topMargin=TOP,
                              bottomMargin=BOTTOM, title=f"Rechnung {meta['nummer']}", author=s.firma)

        def frame(bottom):
            return Frame(SIDE, bottom, width, A4[1] - TOP - bottom, 0, 0, 0, 0)

        def slip(c, dd):
            _footer(c, dd, "", y=SLIP_HEIGHT + 3 * mm, rule=False)
            qr.draw_payment_slip(c, meta, customer, s, s.get("sprache") or "de")

        templates = {
            "slip": PageTemplate("slip", [frame(SLIP_RESERVE)], onPage=slip),
            "plain": PageTemplate("plain", [frame(BOTTOM)],
                                  onPage=lambda c, dd: _footer(c, dd, f"Rechnung {meta['nummer']}" if dd.page > 1 else "")),
        }
        doc.addPageTemplates([templates.pop(first)] + list(templates.values()))
        return doc

    probe = make("slip", io.BytesIO())
    probe.build(letter())
    buf = io.BytesIO()
    if probe.page == 1:
        make("slip", buf).build(letter())
    else:
        story = letter() + [NextPageTemplate("slip"), PageBreak(),
                            P(f"Rechnung {meta['nummer']}", "h2"),
                            P(f"Total {meta.get('waehrung', 'CHF')} {chf(meta['total'])}, zahlbar bis {d(meta['faellig'])}")]
        make("plain", buf).build(story)
    return buf.getvalue()


# ---------- Reminder (Mahnung) ----------

def reminder_pdf(book: Book, meta: dict, state: dict, entry: dict, text: str, customer: dict | None) -> bytes:
    """A reminder letter with a QR-bill for the open amount (same reference as the invoice)."""
    s = book.settings
    width = A4[0] - 2 * SIDE
    a = meta.get("an") or {}
    cur = meta.get("waehrung", "CHF")
    lines = [a.get("name")] + ([a.get("zusatz")] if a.get("zusatz") else []) + [
        " ".join(x for x in (a.get("strasse"), a.get("nr")) if x),
        " ".join(x for x in (a.get("plz"), a.get("ort")) if x)]
    if a.get("land") and a.get("land") != "CH":
        lines.append(a["land"])
    slip_meta = {**meta, "total": state["offen"]}
    title = f"{entry['bezeichnung']} zu Rechnung {meta['nummer']}"
    story = [_letterhead(book, width), Spacer(1, max(0, ADDRESS_TOP - TOP - 22 * mm)),
             Table([[None, [P(l) for l in lines if l]]], colWidths=[width * 0.55, width * 0.45],
                   style=[("LEFTPADDING", (0, 0), (-1, -1), 0)]),
             Spacer(1, 14 * mm), P(title, "title"),
             P(f"{s.adresse.get('ort') or ''}, {d(entry['datum'])}".strip(", "), "small"), Spacer(1, 6 * mm),
             P(text), Spacer(1, 6 * mm)]
    data = [["Rechnung", "Datum", "Fällig", f"Betrag {cur}"],
            [meta["nummer"], d(meta["datum"]), d(meta["faellig"]), chf(state["total"])]]
    if state.get("bezahlt"):
        data.append(["Bereits bezahlt", "", "", "−" + chf(state["bezahlt"])])
    if state.get("gutgeschrieben"):
        data.append(["Gutschrift", "", "", "−" + chf(state["gutgeschrieben"])])
    data.append([f"Offen, zahlbar bis {d(entry['frist'])}", "", "", chf(state["offen"])])
    story.append(_grid(data, [width * 0.46, width * 0.17, width * 0.17, width * 0.20],
                       total_rows=[len(data) - 1], right_cols=(3,)))
    if meta.get("referenz"):
        story += [Spacer(1, 3 * mm), P(f"Referenz {qr.format_reference(meta['referenz'])}", "small")]
    story += [Spacer(1, 6 * mm), P("Freundliche Grüsse"), P(s.firma)]
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=SIDE, rightMargin=SIDE, topMargin=TOP, bottomMargin=BOTTOM,
                          title=title, author=s.firma)

    def slip(c, dd):
        _footer(c, dd, "", y=SLIP_HEIGHT + 3 * mm, rule=False)
        qr.draw_payment_slip(c, slip_meta, customer, s, s.get("sprache") or "de")

    doc.addPageTemplates([PageTemplate("slip", [Frame(SIDE, SLIP_RESERVE, width, A4[1] - TOP - SLIP_RESERVE, 0, 0, 0, 0)],
                                       onPage=slip)])
    doc.build(story)
    return buf.getvalue()


# ---------- Payslip ----------

def payslip_pdf(book: Book, meta: dict, emp: dict) -> bytes:
    s = book.settings
    width = A4[0] - 2 * SIDE
    w = meta["werte"]
    buf = io.BytesIO()
    doc = _doc(buf, f"Lohnabrechnung {meta['monat']:02d}/{meta['jahr']}", s.firma,
               label=f"Lohnabrechnung {meta.get('name')} {meta['monat']:02d}/{meta['jahr']}")
    a = emp.get("adresse") or {}
    addr = [meta.get("name"), " ".join(str(x) for x in (a.get("strasse"), a.get("nr")) if x),
            " ".join(str(x) for x in (a.get("plz"), a.get("ort")) if x)]
    story = [_letterhead(book, width), Spacer(1, 18 * mm),
             Table([[None, [P(l) for l in addr if l]]], colWidths=[width * 0.55, width * 0.45],
                   style=[("LEFTPADDING", (0, 0), (-1, -1), 0)]),
             Spacer(1, 12 * mm), P(f"Lohnabrechnung {MONTHS_DE[int(meta['monat'])]} {meta['jahr']}", "title")]
    info = [["Personalnummer", meta["mitarbeiter"]], ["AHV-Nr.", emp.get("ahv_nr") or ""]]
    if w.get("lohnart") == "monat":
        info.append(["Pensum", f"{Decimal(str(w['pensum'])):g} %"])
    story.append(Table([[P(k, "small"), P(v)] for k, v in info], colWidths=[34 * mm, 80 * mm], hAlign="LEFT",
                       style=[("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(Spacer(1, 6 * mm))

    rows = [["", "Basis", "Satz", "CHF"]]
    if w.get("lohnart") == "stunde":
        rows.append(["Grundlohn", f"{w['stunden']} h", chf(w["stundenlohn"]), chf(w["grundlohn"])])
    else:
        rows.append(["Grundlohn", "", "", chf(w["grundlohn"])])
    if Decimal(str(w.get("ferienzuschlag") or 0)):
        rows.append(["Ferienzuschlag", "", f"{Decimal(str(w['ferienzuschlag_satz'])) * 100:.2f} %", chf(w["ferienzuschlag"])])
    rows.append(["Bruttolohn", "", "", chf(w["bruttolohn"])])
    brutto_row = len(rows) - 1
    cfg_an = payroll.config(book)["saetze_an"]
    for key, label in (("ahv", "AHV/IV/EO"), ("alv", "ALV"), ("uvg", "NBU"), ("ktg", "KTG")):
        if Decimal(str(w.get(key) or 0)):
            rows.append([label, chf(w["bruttolohn"]), f"{Decimal(str(cfg_an.get(key) or 0)) * 100:.3g} %", f"−{chf(w[key])}"])
    if Decimal(str(w.get("bvg") or 0)):
        rows.append(["BVG", "", "", f"−{chf(w['bvg'])}"])
    if Decimal(str(w.get("quellensteuer") or 0)):
        rows.append([f"Quellensteuer {w.get('qst_code') or ''}".strip(),
                     chf(w.get("qst_satzbestimmend")) if w.get("qst_code") else chf(w["bruttolohn"]),
                     f"{Decimal(str(w['qst_satz'])) * 100:.2f} %", f"−{chf(w['quellensteuer'])}"])
    rows.append(["Total Abzüge", "", "", f"−{chf(w['total_abzuege'])}"])
    abz_row = len(rows) - 1
    if Decimal(str(w.get("kinderzulagen") or 0)):
        rows.append(["Kinderzulagen", "", "", chf(w["kinderzulagen"])])
    if Decimal(str(w.get("korrektur") or 0)):
        rows.append([(meta.get("eingaben") or {}).get("korrektur_text") or "Korrektur", "", "", chf(w["korrektur"])])
    if Decimal(str(w.get("spesen") or 0)):
        rows.append(["Nettolohn", "", "", chf(w["nettolohn"])])
        rows.append([f"Spesen (effektiv, {', '.join((meta.get('eingaben') or {}).get('spesen') or [])})", "", "",
                     chf(w["spesen"])])
        rows.append(["Auszahlung", "", "", chf(w["auszahlung"])])
    else:
        rows.append(["Nettolohn (Auszahlung)", "", "", chf(w["nettolohn"])])
    story.append(_grid(rows, [width * 0.46, width * 0.18, width * 0.16, width * 0.2],
                       total_rows=[brutto_row, abz_row, len(rows) - 1], right_cols=(1, 2, 3)))
    if s.get("iban"):
        story += [Spacer(1, 6 * mm), P("Die Auszahlung erfolgt auf das bei uns hinterlegte Konto.", "small")]
    doc.build(story)
    return buf.getvalue()


# ---------- Jahresrechnung ----------

def statement_pdf(book: Book, st: dict, anhang_blocks: list) -> bytes:
    s = book.settings
    width = A4[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, f"Jahresrechnung {st['jahr']}", s.firma, label=f"{s.firma} · Jahresrechnung {st['jahr']}")
    story = [P(s.firma, "title"), P(f"Jahresrechnung {st['jahr']}", "h2"),
             P(f"Geschäftsjahr 01.01.{st['jahr']} – 31.12.{st['jahr']}", "small"), Spacer(1, 8 * mm)]

    def block(title, rows):
        data = [[title, str(st["jahr"]), str(st["vorjahr"])]]
        totals, subs = [], []
        for r in rows:
            if r["stil"] == "line":
                data.append([P(r["label"], "cell"), chf(r["aktuell"]), chf(r["vorjahr"])])
            else:
                data.append([r["label"], chf(r["aktuell"]), chf(r["vorjahr"])])
                (totals if r["stil"] == "total" else subs).append(len(data) - 1)
        t = _grid(data, [width * 0.62, width * 0.19, width * 0.19], total_rows=totals, right_cols=(1, 2))
        t.setStyle(TableStyle([("FONT", (0, r), (-1, r), "Helvetica-Bold", 8.5) for r in subs]))
        return t

    story += [block("Aktiven", st["aktiven"]), Spacer(1, 8 * mm), block("Passiven", st["passiven"]),
              PageBreak(), P("Erfolgsrechnung", "h2"), block("", st["erfolg"])]
    g = st["gewinnverwendung"]
    story += [Spacer(1, 10 * mm), KeepTogether([
        P("Antrag über die Verwendung des Bilanzgewinns", "h2"),
        _grid([["", "CHF"],
               ["Gewinnvortrag", chf(g["gewinnvortrag"])],
               ["Jahresgewinn / Jahresverlust", chf(g["jahresgewinn"])],
               ["Bilanzgewinn", chf(g["bilanzgewinn"])],
               ["Dividende", f"−{chf(g['dividende'])}"],
               ["Zuweisung gesetzliche Gewinnreserve", f"−{chf(g['reserve'])}"],
               ["Vortrag auf neue Rechnung", chf(g["vortrag_neu"])]],
              [width * 0.62, width * 0.19], total_rows=[3, 6], right_cols=(1,))])]
    if anhang_blocks:
        story += [PageBreak(), P("Anhang", "h2")]
        for kind, text in anhang_blocks:
            story.append(P(text, "bold" if kind == "heading" else "base"))
            if kind != "heading":
                story.append(Spacer(1, 3 * mm))
    doc.build(story)
    return buf.getvalue()


# ---------- Kontoblatt, Journal, Debitoren ----------

def ledger_pdf(book: Book, led: dict) -> bytes:
    s = book.settings
    width = landscape(A4)[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, f"Kontoblatt {led['konto']} {led['jahr']}", s.firma, pagesize=landscape(A4),
               label=f"{s.firma} · Konto {led['konto']} {led['name']} · {led['jahr']}")
    data = [["Datum", "Beleg", "Text", "Gegenkonto", "Soll", "Haben", "Saldo"],
            ["", "", "Eröffnung", "", "", "", chf(led["eroeffnung"])]]
    for z in led["zeilen"]:
        data.append([d(z["datum"]), z["beleg"], P(z["text"], "cell"), z["gegenkonto"], chf(z["soll"], True),
                     chf(z["haben"], True), chf(z["saldo"])])
    data.append(["", "", "Total / Saldo", "", chf(led["total_soll"]), chf(led["total_haben"]), chf(led["saldo"])])
    story = [P(f"Konto {led['konto']} {led['name']}", "title"), P(f"Geschäftsjahr {led['jahr']}", "small"),
             Spacer(1, 5 * mm),
             _grid(data, [w * width for w in (0.09, 0.09, 0.4, 0.09, 0.11, 0.11, 0.11)],
                   total_rows=[len(data) - 1], zebra=True, right_cols=(4, 5, 6))]
    doc.build(story)
    return buf.getvalue()


def journal_pdf(book: Book, rows: list, title: str) -> bytes:
    s = book.settings
    width = landscape(A4)[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, title, s.firma, pagesize=landscape(A4), label=f"{s.firma} · {title}")
    data = [["Datum", "Beleg", "Text", "Soll", "Haben", "Betrag"]]
    total = Decimal("0")
    for r in rows:
        data.append([d(r.datum), r.beleg, P(r.text, "cell"), r.soll, r.haben, chf(r.betrag)])
        total += r.betrag
    data.append(["", "", "Total", "", "", chf(total)])
    story = [P(title, "title"), Spacer(1, 4 * mm),
             _grid(data, [w * width for w in (0.09, 0.11, 0.5, 0.08, 0.08, 0.14)],
                   total_rows=[len(data) - 1], zebra=True, right_cols=(5,))]
    doc.build(story)
    return buf.getvalue()


def receivables_pdf(book: Book, ar: dict) -> bytes:
    s = book.settings
    width = A4[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, "Offene Debitoren", s.firma, label=f"{s.firma} · Offene Debitoren per {d(ar['stichtag'])}")
    data = [["Rechnung", "Kunde", "Datum", "Alter", "Offen"]]
    for p in ar["posten"]:
        data.append([p["nummer"], P(p["name"] or p["kunde"], "cell"), d(p["datum"]), f"{p['alter_tage']} T", chf(p["offen"])])
    data.append(["Total", "", "", "", chf(ar["total_offen"])])
    story = [P(f"Offene Debitoren per {d(ar['stichtag'])}", "title"), Spacer(1, 4 * mm),
             _grid(data, [w * width for w in (0.17, 0.4, 0.14, 0.11, 0.18)], total_rows=[len(data) - 1],
                   right_cols=(3, 4)),
             Spacer(1, 5 * mm),
             P(f"Saldo Konto {ar['debitorenkonto']}: {chf(ar['saldo_debitoren'])} · "
               f"Differenz zur Offen-Posten-Liste: {chf(ar['differenz'])}", "small")]
    doc.build(story)
    return buf.getvalue()



def mwst_abstimmung_pdf(book: Book, rep: dict) -> bytes:
    """Year-end reconciliation, including cash-basis bridge and unresolved findings."""
    year = rep["jahr"]
    cash = rep["abrechnungsart"] == "vereinnahmt"
    width = landscape(A4)[0] - 2 * SIDE
    buf = io.BytesIO()
    title = f"MWST-Umsatzabstimmung {year}"
    doc = _doc(buf, title, book.settings.firma, pagesize=landscape(A4),
               label=f"{book.settings.firma} · {title}")
    story = [P(title, "title"),
             P(f"{book.settings.firma} · {book.settings.get('uid') or ''} · "
               f"{rep['methode']} · {rep['abrechnungsart']}e Entgelte · Stand {d(date.today())}", "small"),
             P("Umsatz und Steuer stimmen mit den gebuchten Abrechnungen überein." if rep["ok"] else
               "Nicht abgestimmt. Offene Abrechnungen und Differenzen prüfen.", "bold")]
    for hint in rep["hinweise"]:
        story.append(P(hint, "small"))
    story += [P("Umsatz und Steuer (CHF)", "h2")]
    header = ["Ziffer / Bezeichnung", "Buchhaltung"]
    if cash:
        header += ["+ offen 1.1.", "− offen 31.12."]
    header += ["Zu deklarieren", "Deklariert", "Differenz"]
    data = [header]
    for row in rep["zeilen"]:
        values = [row["buchhaltung"]]
        if cash:
            values += [row["offen_anfang"], row["offen_ende"]]
        values += [row["soll"], row["deklariert"], row["differenz"]]
        data.append([P(f"{row['ziffer'][:3]} {row['label']}", "cell")] + [chf(v) for v in values])
    story += [_grid(data, [width * 0.34] + [width * 0.66 / (len(header) - 1)] * (len(header) - 1),
                    right_cols=tuple(range(1, len(header)))),
              P("Buchhaltung nach Belegdatum; deklariert laut gebuchten Abrechnungen. "
                "Steuerdifferenzen bis 1 Rappen je Abrechnung gelten als Rundung.", "small"),
              P("Gebuchte Abrechnungen", "h2")]
    periods = [["Periode", "Status", "Zahllast CHF"]]
    for period in rep["perioden"]:
        status = "seit Buchung geändert" if period["veraendert"] else "gebucht" if period["gebucht"] else "offen"
        periods.append([period["periode"], status, chf(period["zahllast"])])
    story += [_grid(periods, [width * 0.2, width * 0.6, width * 0.2], right_cols=(2,)),
              P("Ertrag laut Erfolgsrechnung", "h2")]
    revenue = [["Konto / Bezeichnung", "Total CHF", "mit Umsatzcode", "ohne Code"]]
    for account in rep["ertrag"]["konten"]:
        revenue.append([P(f"{account['konto']} {account['name']}", "cell"), chf(account["total"]),
                        chf(account["mit_code"]), chf(account["ohne_code"])])
    revenue.append(["Total"] + [chf(rep["ertrag"][key]) for key in ("total", "mit_code", "ohne_code")])
    story += [_grid(revenue, [width * 0.46] + [width * 0.18] * 3,
                    total_rows=(len(revenue) - 1,), right_cols=(1, 2, 3))]
    if rep["ertrag"]["umsatz_andere_konten"]:
        story.append(P(f"Umsatzcodes auf anderen Konten: {chf(rep['ertrag']['umsatz_andere_konten'])} CHF", "small"))
    story.append(P("MWST-Konten per 31.12. (vor Abgrenzung)", "h2"))
    accounts = [["Konto / Erklärung", "Saldo CHF", "Erwartet", "Differenz"]]
    for account in rep["steuerkonten"]:
        accounts.append([P(f"{account['konto']} {account['rolle']} · {account['erklaerung']}", "cell"),
                         chf(account["saldo"]), chf(account["erwartet"]), chf(account["differenz"])])
    settlement = rep["abrechnungskonto"]
    accounts.append([f"{settlement['konto']} Abrechnungskonto", chf(settlement["saldo"]), "", ""])
    story += [_grid(accounts, [width * 0.55] + [width * 0.15] * 3, right_cols=(1, 2, 3)),
              P("Vorzeichen wie im Hauptbuch: Aktiven +, Passiven −. Das Abrechnungskonto zeigt "
                "offene Zahllast (−) bzw. Guthaben (+) gegenüber der ESTV.", "small")]
    if cash:
        end = rep["offen"]["ende"]
        status = "veraltet" if rep["abgrenzung_veraltet"] else "gebucht" if rep["abgrenzung"] else "noch nicht gebucht"
        story += [P("Abgrenzung der offenen Posten", "h2"),
                  P(f"Umsatzsteuer offen: {chf(end['umsatzsteuer'])} CHF · "
                    f"Vorsteuer offen: {chf(end['vorsteuer'])} CHF · Abgrenzung: {status}", "small")]
        if end["belege"]:
            items = [["Art", "Beleg", "Steuer CHF"]] + [
                [item["art"], item["nummer"], chf(item["steuer"])] for item in end["belege"]]
            story.append(_grid(items, [width * 0.2, width * 0.6, width * 0.2], right_cols=(2,)))
    story += [Spacer(1, 4 * mm), P(f"Einreichefrist Jahresabstimmung (Berichtigungsabrechnung nach Art. 72 MWSTG): "
                                 f"{d(rep['frist_korrektur'])}. Vergleich mit den gebuchten Abrechnungen; "
                                 "die Einreichung im ESTV-Portal separat prüfen.", "small")]
    doc.build(story)
    return buf.getvalue()


def mwst_pdf(book: Book, rep: dict) -> bytes:
    """Summary of a MWST-Abrechnung to transcribe into the ESTV portal."""
    s = book.settings
    width = A4[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, f"MWST-Abrechnung {rep['periode']}", s.firma, label=f"{s.firma} · MWST {rep['periode']}")
    z = rep["ziffern"]
    labels = [("200", "Total der vereinbarten Entgelte"), ("220", "Steuerbefreite Leistungen"),
              ("230", "Von der Steuer ausgenommene Leistungen"), ("289", "Total Abzüge"),
              ("299", "Steuerbares Gesamtentgelt")]
    if rep["methode"] == "saldo":
        labels += [("322", f"Leistungen zum Saldosteuersatz {rep['saldosteuersatz']} %")]
    else:
        labels += [("303", "Leistungen zum Normalsatz 8.1 %"), ("313", "Reduzierter Satz 2.6 %"),
                   ("343", "Beherbergung 3.8 %")]
    data = [["Ziffer", "Bezeichnung", "Entgelt", "Steuer"]]
    for nr, label in labels:
        data.append([nr, P(label, "cell"), chf(z.get(nr, 0)), chf(z.get(nr + "_steuer", 0)) if nr + "_steuer" in z else ""])
    data.append(["399", "Total geschuldete Steuer", "", chf(z.get("399", 0))])
    if rep["methode"] != "saldo":
        data += [["400", P("Vorsteuer Material- und Dienstleistungsaufwand", "cell"), "", chf(z.get("400", 0))],
                 ["405", P("Vorsteuer Investitionen und übriger Betriebsaufwand", "cell"), "", chf(z.get("405", 0))],
                 ["479", "Total Vorsteuer", "", chf(z.get("479", 0))]]
    pay = Decimal(str(rep["zahllast"]))
    data.append(["500" if pay >= 0 else "510", "Zu bezahlender Betrag" if pay >= 0 else "Guthaben", "", chf(abs(pay))])
    story = [P(f"MWST-Abrechnung {rep['periode']}", "title"),
             P(f"{s.firma} · {s.get('uid') or ''} · {d(rep['von'])} – {d(rep['bis'])} · "
               f"{'Saldosteuersatz' if rep['methode'] == 'saldo' else 'effektive Methode'}", "small"),
             Spacer(1, 6 * mm),
             _grid(data, [width * 0.1, width * 0.54, width * 0.18, width * 0.18], total_rows=[len(data) - 1],
                   right_cols=(2, 3)),
             Spacer(1, 6 * mm),
             P("Hilfsblatt aus batzen. Die Ziffern entsprechen dem ESTV-Formular mit den Sätzen ab 1.1.2024; "
               "vor dem Einreichen im ePortal mit dem aktuellen Formular abgleichen.", "small")]
    doc.build(story)
    return buf.getvalue()

"""The initialisation letter (INI and HIA): the hashes of the participant's keys, signed by hand and sent to
the bank. Only with it does the bank release the keys it received electronically."""
from __future__ import annotations

import datetime as dt
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import ebics

BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=10, leading=13)
SMALL = ParagraphStyle("small", parent=BASE, fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
H1 = ParagraphStyle("h1", parent=BASE, fontName="Helvetica-Bold", fontSize=14, leading=18, spaceAfter=4)
MONO = ParagraphStyle("mono", parent=BASE, fontName="Courier-Bold", fontSize=10, leading=13)


def _table(rows, widths):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _hash(text: str) -> Paragraph:
    pairs = text.split()
    half = (len(pairs) + 1) // 2
    return Paragraph(" ".join(pairs[:half]) + "<br/>" + " ".join(pairs[half:]), MONO)


def pdf(book) -> bytes:
    st = ebics.status(book)
    cfg = st["cfg"]
    if not st["eigene"]:
        raise ebics.BookError("Es gibt noch keine EBICS-Schlüssel (zuerst einrichten)")
    firma = book.settings.firma
    adresse = book.settings.address_lines
    heute = dt.date.today().strftime("%d.%m.%Y")
    by_version = {h["version"]: h for h in st["eigene"]}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=22 * mm, rightMargin=20 * mm, topMargin=20 * mm,
                            bottomMargin=18 * mm, title="EBICS-Initialisierungsbrief", author=firma)
    story = []
    for titel, auftrag, versionen in (("Initialisierungsbrief INI", "INI", ("A006",)),
                                      ("Initialisierungsbrief HIA", "HIA", ("X002", "E002"))):
        story += [Paragraph(escape(firma), BASE)] + [Paragraph(escape(line), BASE) for line in adresse]
        story += [Spacer(1, 10 * mm), Paragraph(f"EBICS — {titel}", H1),
                  Paragraph(f"An: {escape(cfg.get('bank') or 'die Bank')}", BASE), Spacer(1, 4 * mm)]
        story.append(_table([
            ["Datum", heute], ["Host-ID", cfg["host_id"]], ["Partner-ID (Kunden-ID)", cfg["partner_id"]],
            ["Teilnehmer-ID", cfg["user_id"]], ["Auftragsart", auftrag], ["EBICS-Version", "3.0 (H005)"],
            ["Gesendet am", (cfg.get(f"{auftrag.lower()}_gesendet") or "—").replace("T", " ")]],
            [55 * mm, 113 * mm]))
        story.append(Spacer(1, 6 * mm))
        for v in versionen:
            h = by_version[v]
            story += [Paragraph(f"<b>{v} — {escape(h['art'])}</b> · RSA {h['bits']} Bit · selbst signiertes "
                                f"Zertifikat, Seriennummer {h['seriennummer']}", BASE), Spacer(1, 2 * mm),
                      _table([[Paragraph("Hashwert des Zertifikats<br/>(SHA-256, EBICS 3.0)", SMALL), _hash(h["zertifikat"])],
                              [Paragraph("Hashwert des Schlüssels<br/>(SHA-256, EBICS 2.5)", SMALL), _hash(h["schluessel"])]],
                             [55 * mm, 113 * mm]),
                      Spacer(1, 6 * mm)]
        story += [Paragraph("Wir bestätigen, dass wir die oben aufgeführten öffentlichen Schlüssel elektronisch an die "
                            "Bank übermittelt haben, und bitten um Freischaltung.", BASE), Spacer(1, 16 * mm),
                  _table([["Ort, Datum", "Rechtsgültige Unterschrift"], ["\n\n", ""]], [60 * mm, 108 * mm]),
                  Spacer(1, 3 * mm), Paragraph(escape(firma), SMALL)]
        if auftrag == "INI":
            story.append(PageBreak())
    doc.build(story)
    return buf.getvalue()

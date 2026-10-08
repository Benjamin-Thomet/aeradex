"""PDF documents: invoices with Swiss QR-bill, payslips, Jahresrechnung,
Kontoblatt, Journal and Debitorenliste.

Plain Swiss business documents on the company's own letterhead; aeradex puts
no mark of its own on anything that leaves the house.
"""
from __future__ import annotations

import contextvars
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
    "lh_firm": ParagraphStyle("lh_firm", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK),
    "lh_line": ParagraphStyle("lh_line", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "label": ParagraphStyle("label", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=MUTED),
    "section": ParagraphStyle("section", fontName="Helvetica-Bold", fontSize=7.5, leading=9.5, textColor=MUTED),
    "cover": ParagraphStyle("cover", fontName="Helvetica-Bold", fontSize=26, leading=31, textColor=INK),
    "coversub": ParagraphStyle("coversub", fontName="Helvetica", fontSize=15, leading=20, textColor=INK),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK, spaceAfter=1),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=9.5, leading=13, textColor=INK, spaceBefore=7,
                         spaceAfter=1, keepWithNext=1),
}


def chf(value, blank_zero: bool = False) -> str:
    v = Decimal(str(value or 0))
    if not v:
        if blank_zero:
            return ""
        v = abs(v)
    return f"{v:,.2f}".replace(",", "'")


def price(value) -> str:
    """A unit price: like chf, with up to four decimals where the price has them (0.335)."""
    v = Decimal(str(value or 0))
    if v == v.quantize(Decimal("0.01")):
        return chf(v)
    whole, _, frac = f"{v:.4f}".rstrip("0").partition(".")
    return f"{int(whole):,}".replace(",", "'") + "." + frac


def d(value) -> str:
    return parse_date(value).strftime("%d.%m.%Y") if value else ""


def P(text, style="base"):
    return Paragraph(escape(str(text)) if not str(text).startswith("<") else str(text), ST[style])


# Set while aeradex builds the reports of a year that is not locked yet: every page
# drawn through _doc then carries this word across it (see dossier.Kontext.draft).
WATERMARK: contextvars.ContextVar[str] = contextvars.ContextVar("aeradex_watermark", default="")


def _watermark(canvas, doc) -> None:
    text = WATERMARK.get()
    if not text:
        return
    w, h = doc.pagesize
    canvas.saveState()
    canvas.setFillColor(colors.Color(0.6, 0.6, 0.6, alpha=0.22))
    canvas.setFont("Helvetica-Bold", 72)
    canvas.translate(w / 2, h / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, -24, text)
    canvas.restoreState()


def _footer(canvas, doc, label: str, y: float = 10 * mm, rule: bool = True, first_number: bool = True):
    _watermark(canvas, doc)
    if not first_number and doc.page == 1:   # a one-page letter needs no page number
        return
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


def _running_head(canvas, doc, title: str) -> None:
    """Company name and document title above every page of a report or list."""
    w, h = doc.pagesize
    firm = doc.author or ""
    if title.startswith(f"{firm} · "):
        title = title[len(firm) + 3:]
    y = h - 12 * mm
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(INK)
    canvas.drawString(SIDE, y, firm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(w - SIDE, y, title)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(SIDE, y - 2.2 * mm, w - SIDE, y - 2.2 * mm)
    canvas.restoreState()


FLUSH = [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
         ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]


def _firm_block(book: Book, width: float) -> Table:
    """The company once, top left: name, address on one line, contact and UID/MWST-Nr."""
    s = book.settings
    rows = [[P(s.firma, "lh_firm")]]
    if s.address_lines:
        rows.append([P(" · ".join(s.address_lines), "lh_line")])
    contact = [x for x in (s.get("telefon"), s.get("email"), s.get("web")) if x]
    if contact:
        rows.append([P(" · ".join(contact), "lh_line")])
    uid = (s.get("uid") or "").strip()
    if uid:
        rows.append([P(f"{'MWST-Nr.' if uid.upper().endswith('MWST') else 'UID'} {uid}", "lh_line")])
    return Table(rows, colWidths=[width], style=FLUSH)


def letter_head(book: Book, width: float, recipient: list[str]) -> list:
    """Top of a letter (invoice, reminder, payslip, offer): the company top left, the recipient in the
    right window of a C5/C6 envelope with the sender line directly above it (SN 010130)."""
    s = book.settings
    sender = ", ".join([s.firma] + s.address_lines)
    window = [[P(f"<u>{escape(sender)}</u>", "sender")], [Spacer(1, 1.5 * mm)]] + \
             [[P(escape(l))] for l in recipient if l]
    head_h = ADDRESS_TOP - TOP - 5 * mm
    t = Table([[_firm_block(book, width * 0.55), ""], ["", Table(window, colWidths=[width * 0.45], style=FLUSH)]],
              colWidths=[width * 0.55, width * 0.45], rowHeights=[head_h, 28 * mm])
    t.setStyle(TableStyle(FLUSH))
    return [t, Spacer(1, 6 * mm)]


def _letterhead(book: Book, width: float) -> Table:
    """Kept for plugins written against the old layout: the company block alone."""
    return _firm_block(book, width)


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


def _doc(buf, title: str, author: str, pagesize=A4, label: str = "", head: bool = True) -> BaseDocTemplate:
    """A report or list: running head (company · title) and page numbers. head=False for letters."""
    doc = BaseDocTemplate(buf, pagesize=pagesize, leftMargin=SIDE, rightMargin=SIDE,
                          topMargin=TOP, bottomMargin=BOTTOM, title=title, author=author)
    w, h = pagesize
    frame = Frame(SIDE, BOTTOM, w - 2 * SIDE, h - TOP - BOTTOM, 0, 0, 0, 0)

    def page(c, dd):
        if head:
            _running_head(c, dd, label or title)
        _footer(c, dd, "" if head else (label or title))

    doc.addPageTemplates([PageTemplate("main", [frame], onPage=page)])
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
        story = letter_head(book, width, lines)
        story.append(P(f"Rechnung {meta['nummer']}", "title"))
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
            data.append([P(p["text"], "cell"), menge, price(p["preis"]), chf(p["betrag"])])
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
            _footer(c, dd, "", y=SLIP_HEIGHT + 3 * mm, rule=False, first_number=False)
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
    story = [*letter_head(book, width, lines), P(title, "title"),
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
        _footer(c, dd, "", y=SLIP_HEIGHT + 3 * mm, rule=False, first_number=False)
        qr.draw_payment_slip(c, slip_meta, customer, s, s.get("sprache") or "de")

    doc.addPageTemplates([PageTemplate("slip", [Frame(SIDE, SLIP_RESERVE, width, A4[1] - TOP - SLIP_RESERVE, 0, 0, 0, 0)],
                                       onPage=slip)])
    doc.build(story)
    return buf.getvalue()


# ---------- Payslip ----------

def _pct(rate) -> str:
    """A rate as a percentage with two decimals, three where needed (0.7 → '0.70 %', 0.01425 → '1.425 %')."""
    x = Decimal(str(rate or 0)) * 100
    return f"{x:.3f} %" if x != x.quantize(Decimal("0.01")) else f"{x:.2f} %"


def _info_grid(pairs: list[tuple[str, str]], width: float, cols: int = 2) -> Table:
    """Label/value pairs in a framed grid of `cols` columns (header data of a payslip or statement)."""
    pairs = [(k, v) for k, v in pairs if v]
    per = -(-len(pairs) // cols)
    rows = []
    for i in range(per):
        row = []
        for c in range(cols):
            k, v = pairs[c * per + i] if c * per + i < len(pairs) else ("", "")
            row += [P(k, "label"), P(v, "cell")]
        rows.append(row)
    lw, vw = width / cols * 0.42, width / cols * 0.58
    t = Table(rows, colWidths=[lw, vw] * cols)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, RULE), ("BACKGROUND", (0, 0), (-1, -1), SOFT),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2), ("LEFTPADDING", (0, 0), (-1, -1), 5)]
                          + [("LINEBEFORE", (2 * c, 0), (2 * c, -1), 0.5, RULE) for c in range(1, cols)]))
    return t


def payslip_pdf(book: Book, meta: dict, emp: dict) -> bytes:
    """Lohnabrechnung as usual in Switzerland: the employee in the envelope window, the employment data,
    then wage, social insurance, Quellensteuer, allowances and expenses down to the amount paid out."""
    import calendar
    s = book.settings
    width = A4[0] - 2 * SIDE
    w = meta["werte"]
    D = lambda key: Decimal(str(w.get(key) or 0))  # noqa: E731
    jahr, monat = int(meta["jahr"]), int(meta["monat"])
    last = calendar.monthrange(jahr, monat)[1]
    buf = io.BytesIO()
    doc = _doc(buf, f"Lohnabrechnung {monat:02d}/{jahr}", s.firma,
               label=f"Lohnabrechnung {meta.get('name')} {monat:02d}/{jahr}", head=False)
    a = emp.get("adresse") or {}
    addr = [meta.get("name"), " ".join(str(x) for x in (a.get("strasse"), a.get("nr")) if x),
            " ".join(str(x) for x in (a.get("plz"), a.get("ort")) if x)]
    if a.get("land") and a.get("land") != "CH":
        addr.append(a["land"])
    datum = date(jahr, monat, last)
    ort = s.adresse.get("ort") or ""
    story = letter_head(book, width, addr)
    story += [Table([[P(f"Lohnabrechnung {MONTHS_DE[monat]} {jahr}", "title"),
                      P(f"{ort + ', ' if ort else ''}{d(datum)}", "right")]],
                    colWidths=[width * 0.7, width * 0.3], style=FLUSH + [("VALIGN", (0, 0), (-1, -1), "BOTTOM")]),
              Spacer(1, 3 * mm)]

    stunde = w.get("lohnart") == "stunde"
    qst = (emp.get("qst") or {}) if isinstance(emp.get("qst"), dict) else {}
    tarif = w.get("qst_code") or qst.get("tarif") or ""
    iban = (emp.get("iban") or "").strip()
    info = [("Personal-Nr.", meta["mitarbeiter"]), ("AHV-Nr.", emp.get("ahv_nr") or ""),
            ("Geburtsdatum", d(emp.get("geburtsdatum"))), ("Eintritt", d(emp.get("eintritt"))),
            ("Abrechnungsperiode", f"01.{monat:02d}.{jahr} – {last:02d}.{monat:02d}.{jahr}"),
            ("Lohnart", "Stundenlohn" if stunde else f"Monatslohn, Pensum {D('pensum').normalize():f} %"),
            ("Quellensteuer-Tarif", tarif), ("Auszahlung auf", iban)]
    if emp.get("austritt"):
        info.insert(4, ("Austritt", d(emp.get("austritt"))))
    story += [_info_grid(info, width), Spacer(1, 6 * mm)]

    rows = [["", "Basis", "Ansatz", "Betrag CHF"]]
    sections, totals, strong = [], [], []

    def section(title):
        rows.append([P(title.upper(), "section"), "", "", ""])
        sections.append(len(rows) - 1)

    def total(label, value, big=False):
        rows.append([label, "", "", value])
        (strong if big else totals).append(len(rows) - 1)

    section("Lohn")
    if stunde:
        rows.append(["Stundenlohn", f"{D('stunden'):.2f} Std.", chf(w["stundenlohn"]), chf(w["grundlohn"])])
    else:
        voll = Decimal(str(emp.get("monatslohn") or 0))
        if voll and D("pensum") and (voll * D("pensum") / 100).quantize(Decimal("0.01")) == D("grundlohn"):
            rows.append(["Monatslohn", chf(voll), f"{D('pensum'):.2f} %", chf(w["grundlohn"])])
        else:
            rows.append(["Monatslohn", "", "", chf(w["grundlohn"])])
    if D("ferienzuschlag"):
        rows.append(["Ferienentschädigung", chf(w["grundlohn"]), _pct(w["ferienzuschlag_satz"]), chf(w["ferienzuschlag"])])
    total("Bruttolohn", chf(w["bruttolohn"]))

    section("Sozialversicherungen")
    cfg_an = payroll.config(book)["saetze_an"]
    for key, label in (("ahv", "AHV/IV/EO"), ("alv", "ALV"), ("uvg", "NBUV Nichtberufsunfall"), ("ktg", "Krankentaggeld")):
        if D(key):
            rows.append([label, chf(w["bruttolohn"]), _pct(cfg_an.get(key)), f"−{chf(w[key])}"])
    if D("bvg"):
        rows.append(["BVG Pensionskasse", "", "", f"−{chf(w['bvg'])}"])
    notes = []
    if D("quellensteuer"):
        section("Quellensteuer")
        rows.append([f"Quellensteuer{' Tarif ' + tarif if tarif else ''}", chf(w.get("qst_basis", w["bruttolohn"])),
                     _pct(w["qst_satz"]), f"−{chf(w['quellensteuer'])}"])
        basis = Decimal(str(w.get("qst_basis", w["bruttolohn"]) or 0))
        if D("qst_satzbestimmend") and D("qst_satzbestimmend") != basis:
            notes.append(f"Satzbestimmendes Einkommen für die Quellensteuer: CHF {chf(w['qst_satzbestimmend'])}.")
    total("Total Abzüge", f"−{chf(w['total_abzuege'])}")

    extra = [(label, D(key)) for key, label in (("kinderzulagen", "Kinder- und Ausbildungszulagen"),
             ("korrektur", (meta.get("eingaben") or {}).get("korrektur_text") or "Korrektur")) if D(key)]
    if extra:
        section("Zulagen")
        for label, v in extra:
            rows.append([label, "", "", f"−{chf(-v)}" if v < 0 else chf(v)])
    if D("spesen"):
        total("Nettolohn", chf(w["nettolohn"]))
        section("Spesen (nicht AHV-pflichtig)")
        belege = ", ".join((meta.get("eingaben") or {}).get("spesen") or [])
        rows.append([P(f"Spesenrückerstattung effektiv{' (' + belege + ')' if belege else ''}", "cell"), "", "",
                     chf(w["spesen"])])
        total("Auszahlung", chf(w["auszahlung"]), big=True)
    else:
        total("Nettolohn (Auszahlung)", chf(w["nettolohn"]), big=True)

    t = Table(rows, colWidths=[width * 0.5, width * 0.18, width * 0.13, width * 0.19], repeatRows=1)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
             ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
             ("FONT", (0, 0), (-1, 0), "Helvetica", 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
             ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK)]
    for r in sections:
        style += [("TOPPADDING", (0, r), (-1, r), 7), ("SPAN", (0, r), (-1, r))]
    for r in totals:
        style += [("FONT", (0, r), (-1, r), "Helvetica-Bold", 9), ("LINEABOVE", (0, r), (-1, r), 0.5, INK)]
    for r in strong:
        style += [("FONT", (0, r), (-1, r), "Helvetica-Bold", 10.5), ("LINEABOVE", (0, r), (-1, r), 1, INK),
                  ("LINEBELOW", (0, r), (-1, r), 1, INK), ("BACKGROUND", (0, r), (-1, r), SOFT),
                  ("TOPPADDING", (0, r), (-1, r), 4), ("BOTTOMPADDING", (0, r), (-1, r), 4)]
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Spacer(1, 4 * mm))
    for n in notes:
        story.append(P(n, "small"))
    if iban:
        story.append(P(f"Die Auszahlung erfolgt auf das Konto {iban}.", "small"))
    elif s.get("iban"):
        story.append(P("Die Auszahlung erfolgt auf das bei uns hinterlegte Konto.", "small"))
    if D("ferienzuschlag"):
        story.append(P("Die Ferienentschädigung ist im Lohn enthalten und wird separat ausgewiesen; sie ist für "
                       "den Bezug der Ferien bestimmt.", "small"))
    doc.build(story)
    return buf.getvalue()


# ---------- Jahresrechnung ----------

def _amount(value) -> str:
    """A figure in a financial statement: proper minus sign, a dash for nothing."""
    v = Decimal(str(value or 0))
    return "–" if not v else (f"−{chf(-v)}" if v < 0 else chf(v))


def statement_pdf(book: Book, st: dict, anhang_blocks: list) -> bytes:
    """Jahresrechnung (OR 958 ff.): cover, Bilanz, Erfolgsrechnung and Anhang each on their own page,
    then the Antrag über die Verwendung des Bilanzergebnisses and the signatures (OR 958 Abs. 3)."""
    s = book.settings
    width = A4[0] - 2 * SIDE
    jahr = st["jahr"]
    buf = io.BytesIO()
    title = f"Jahresrechnung {jahr}"
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=SIDE, rightMargin=SIDE, topMargin=TOP, bottomMargin=BOTTOM,
                          title=title, author=s.firma)
    frame = Frame(SIDE, BOTTOM, width, A4[1] - TOP - BOTTOM, 0, 0, 0, 0)

    def page(c, dd):
        _running_head(c, dd, title)
        _footer(c, dd, "")

    doc.addPageTemplates([PageTemplate("cover", [frame], onPage=_watermark), PageTemplate("main", [frame], onPage=page)])

    rows_all = st["aktiven"] + st["passiven"] + st["erfolg"]
    prior = jahr > book.settings.erstes_jahr or any(Decimal(str(r["vorjahr"] or 0)) for r in rows_all)
    ort = s.adresse.get("ort") or ""
    art = book.settings.rechtsform_art
    organ = {"einzelfirma": "Inhaber/in", "verein": "Vorstand"}.get(
        art, "Verwaltungsrat" if str(s.get("rechtsform") or "").strip().upper() == "AG" else "Geschäftsführung")

    # Cover
    parts = ["Bilanz", "Erfolgsrechnung", "Anhang"]
    if st.get("gewinnverwendung"):
        parts.append("Antrag über die Verwendung des Bilanzergebnisses")
    cover = [Spacer(1, 62 * mm), P(s.firma, "cover"), Spacer(1, 3 * mm), P(title, "coversub"), Spacer(1, 2 * mm),
             P(f"Geschäftsjahr vom 1. Januar bis 31. Dezember {jahr}", "base"), Spacer(1, 22 * mm)]
    facts = [("Sitz", ort), ("Rechtsform", s.get("rechtsform") or ""), ("UID", (s.get("uid") or "").replace(" MWST", "")),
             ("Inhalt", " · ".join(parts))]
    cover.append(Table([[P(k, "label"), P(v, "cell")] for k, v in facts if v], colWidths=[28 * mm, width - 28 * mm],
                       style=FLUSH + [("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LINEABOVE", (0, 0), (-1, 0), 0.8, INK)]))
    if not st.get("abgeschlossen"):
        cover += [Spacer(1, 8 * mm), P("Entwurf — das Geschäftsjahr ist noch nicht abgeschlossen.", "small")]
    story = cover + [NextPageTemplate("main"), PageBreak()]

    def heading(text, sub):
        return [P(text, "h1"), P(sub, "small"), Spacer(1, 5 * mm)]

    def block(caption, rows, cur_head, pri_head):
        data = [[P(f"<b>{escape(caption)}</b>", "base") if caption else "", cur_head] + ([pri_head] if prior else [])]
        style, n = [], 1
        for r in rows:
            label = P(r["label"], "cell") if r["stil"] == "line" else r["label"]
            line = [label, _amount(r["aktuell"])] + ([_amount(r["vorjahr"])] if prior else [])
            data.append(line)
            if r["stil"] == "zwischentotal":
                style += [("FONT", (0, n), (-1, n), "Helvetica-Bold", 8.5), ("LINEABOVE", (1, n), (-1, n), 0.4, MUTED)]
            elif r["stil"] == "total":
                style += [("FONT", (0, n), (-1, n), "Helvetica-Bold", 9), ("LINEABOVE", (0, n), (-1, n), 0.8, INK),
                          ("LINEBELOW", (0, n), (-1, n), 0.8, INK), ("TOPPADDING", (0, n), (-1, n), 4),
                          ("BOTTOMPADDING", (0, n), (-1, n), 4)]
            n += 1
        widths = [width * 0.62, width * 0.19, width * 0.19] if prior else [width * 0.75, width * 0.25]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                               ("FONT", (1, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (1, 0), (-1, 0), MUTED),
                               ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK)] + style))
        return t

    stichtag, vortag = f"31.12.{jahr}", f"31.12.{jahr - 1}"
    story += heading(f"Bilanz per 31. Dezember {jahr}", "in CHF")
    story += [block("Aktiven", st["aktiven"], stichtag, vortag), Spacer(1, 9 * mm),
              block("Passiven", st["passiven"], stichtag, vortag), PageBreak()]
    story += heading(f"Erfolgsrechnung {jahr}", f"1. Januar bis 31. Dezember {jahr} · in CHF")
    story.append(block("", st["erfolg"], str(jahr), str(jahr - 1)))
    ek = st.get("eigenkapital")
    if ek:
        data = [["", "CHF"], [f"{ek['name']} 31.12. vor Abschluss", _amount(ek["bestand"])],
                *[[f"{p['konto']} {p['name']}", _amount(p["betrag"])] for p in ek["privat"]],
                ["Jahresergebnis", _amount(ek["jahresergebnis"])], [f"{ek['name']} nach Abschluss", _amount(ek["neu"])]]
        story += [Spacer(1, 10 * mm), KeepTogether([P(f"Veränderung {ek['name']}", "h2"),
                  _grid(data, [width * 0.75, width * 0.25], total_rows=[len(data) - 1], right_cols=(1,))])]

    if anhang_blocks:
        story += [PageBreak()] + heading(f"Anhang zur Jahresrechnung {jahr}", "Angaben nach Art. 959c OR")
        nr = 0
        for kind, text in anhang_blocks:
            if kind == "heading":
                nr += 1
                story.append(P(f"{nr}. {text}", "h3"))
            elif text.startswith(("- ", "* ")):
                story.append(Paragraph(escape(text[2:]), ST["base"], bulletText="•"))
            else:
                story += [P(text), Spacer(1, 1.5 * mm)]

    g = st.get("gewinnverwendung")
    if g:
        gewinn, bilanz = Decimal(str(g["bilanzgewinn"])), Decimal(str(g["jahresgewinn"]))
        wort = "Bilanzgewinns" if gewinn >= 0 else "Bilanzverlustes"
        rows = [["", "CHF"],
                ["Gewinnvortrag" if Decimal(str(g["gewinnvortrag"])) >= 0 else "Verlustvortrag", _amount(g["gewinnvortrag"])],
                ["Jahresgewinn" if bilanz >= 0 else "Jahresverlust", _amount(g["jahresgewinn"])],
                ["Bilanzgewinn" if gewinn >= 0 else "Bilanzverlust", _amount(g["bilanzgewinn"])]]
        totals = [3]
        if Decimal(str(g["dividende"] or 0)):
            rows.append(["Dividende", _amount(-Decimal(str(g["dividende"])))])
        if Decimal(str(g["reserve"] or 0)):
            rows.append(["Zuweisung an die gesetzliche Gewinnreserve", _amount(-Decimal(str(g["reserve"])))])
        rows.append(["Vortrag auf neue Rechnung", _amount(g["vortrag_neu"])])
        totals.append(len(rows) - 1)
        antrag = "Der Verwaltungsrat beantragt der Generalversammlung" if organ == "Verwaltungsrat" else \
            "Die Geschäftsführung beantragt der Gesellschafterversammlung"
        story += [PageBreak()] + heading(f"Antrag über die Verwendung des {wort}", f"per 31. Dezember {jahr}")
        story += [P(f"{antrag}, den {'Bilanzgewinn' if gewinn >= 0 else 'Bilanzverlust'} wie folgt zu verwenden:"),
                  Spacer(1, 4 * mm), _grid(rows, [width * 0.75, width * 0.25], total_rows=totals, right_cols=(1,))]

    sign = Table([[P("Ort, Datum", "label"), P(organ, "label"), P("Verantwortlich für die Rechnungslegung", "label")],
                  [P(f"{ort}, ", "cell") if ort else "", "", ""]],
                 colWidths=[width * 0.3, width * 0.35, width * 0.35], rowHeights=[None, 14 * mm])
    sign.setStyle(TableStyle(FLUSH + [("LINEBELOW", (0, 1), (-1, 1), 0.6, INK), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                      ("VALIGN", (0, 1), (-1, 1), "BOTTOM")]))
    story += [Spacer(1, 16 * mm), KeepTogether([P("Unterzeichnet gemäss Art. 958 Abs. 3 OR", "small"),
                                                Spacer(1, 3 * mm), sign])]
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
             _grid(data, [w * width for w in (0.08, 0.125, 0.375, 0.09, 0.11, 0.11, 0.11)],
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
             _grid(data, [w * width for w in (0.08, 0.125, 0.495, 0.08, 0.08, 0.14)],
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


def payables_pdf(book: Book, ap: dict) -> bytes:
    """Offene Kreditoren per Stichtag (`kreditoren.open_payables`), foreign amounts next to the CHF value."""
    s = book.settings
    width = A4[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, "Offene Kreditoren", s.firma, label=f"{s.firma} · Offene Kreditoren per {d(ap['stichtag'])}")
    data = [["Kreditor", "Lieferant", "Rechnungsnr.", "Datum", "Alter", "Offen FW", "Offen CHF"]]
    for p in ap["posten"]:
        fw = f"{p['waehrung']} {chf(p['offen'])}" if p.get("waehrung", "CHF") != "CHF" else ""
        data.append([p["nummer"], P(p.get("name") or p.get("lieferant") or "", "cell"), p.get("rechnungsnr") or "",
                     d(p["datum"]), f"{p['alter_tage']} T", fw, chf(p["offen_chf"])])
    data.append(["Total", "", "", "", "", "", chf(ap["total_offen"])])
    story = [P(f"Offene Kreditoren per {d(ap['stichtag'])}", "title"), Spacer(1, 4 * mm),
             _grid(data, [w * width for w in (0.14, 0.27, 0.13, 0.11, 0.08, 0.13, 0.14)],
                   total_rows=[len(data) - 1], right_cols=(4, 5, 6)),
             Spacer(1, 5 * mm),
             P(f"Saldo Konto {ap['kreditorenkonto']}: {chf(ap['saldo_kreditoren'])} · "
               f"Differenz zur Offen-Posten-Liste: {chf(ap['differenz'])}", "small")]
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
        data.append([P(f"{row['nummer']} {row['label']}", "cell")] + [chf(v) for v in values])
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


def mwst_pdf(book: Book, rep: dict, herkunft: dict | None = None) -> bytes:
    """The MWST-Abrechnung laid out like the ESTV form (Ziffern and wording of form 0550/0535, Stand 2024),
    to transcribe into the ePortal or keep with the books; with `herkunft` (mwst.herkunft) followed by
    every journal row behind each Ziffer."""
    from . import mwst
    s = book.settings
    width = A4[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, f"MWST-Abrechnung {rep['periode']}", s.firma, label=f"{s.firma} · MWST-Abrechnung {rep['periode']}")
    form = mwst.formular(rep)
    story = [_mwst_kopf(book, form, width), Spacer(1, 5 * mm)]
    for section in form["abschnitte"]:
        story += [KeepTogether(_mwst_abschnitt(section, width)), Spacer(1, 4 * mm)]
    story += [_mwst_unterschrift(width), Spacer(1, 3 * mm),
              P("Erstellt mit aeradex aus der Buchhaltung, im Aufbau des ESTV-Formulars (Ziffern und Sätze ab "
                "1.1.2024). Kein amtliches Formular: eingereicht wird im ePortal der ESTV, am einfachsten mit der "
                "eMWST-Datei (eCH-0217). Grau hinterlegte Ziffern ohne Betrag leitet aeradex nicht aus der "
                "Buchhaltung ab — falls sie zutreffen, im ePortal ergänzen.", "small")]
    if herkunft:
        story += _mwst_herkunft(herkunft, width)
    doc.build(story)
    return buf.getvalue()


def _mwst_kopf(book: Book, form: dict, width: float) -> Table:
    s = book.settings
    uid = (s.get("uid") or "").strip()
    mwst_nr = uid if not uid or uid.upper().endswith("MWST") else f"{uid} MWST"
    art = "vereinnahmte Entgelte" if form["abrechnungsart"] == "vereinnahmt" else "vereinbarte Entgelte"
    left = [[P("MWST-Abrechnung", "title")], [P(form["titel"], "bold")], [Spacer(1, 3 * mm)],
            [P(s.firma, "bold")]] + [[P(l)] for l in s.address_lines]
    facts = [("MWST-Nr.", mwst_nr or "—"), ("Abrechnungsperiode", f"{d(form['von'])} – {d(form['bis'])}"),
             ("Periode", form["periode"]), ("Abrechnungsart", art)]
    right = Table([[P(k, "small"), P(v, "bold")] for k, v in facts], colWidths=[width * 0.19, width * 0.26])
    right.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, INK), ("BACKGROUND", (0, 0), (-1, -1), SOFT),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE)]))
    flush = [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    t = Table([[Table(left, colWidths=[width * 0.53], style=flush), right]], colWidths=[width * 0.55, width * 0.45])
    t.setStyle(TableStyle(flush))
    return t


def _mwst_abschnitt(section: dict, width: float) -> Table:
    """One section of the form: a title bar, then Ziffer | wording | boxed amounts."""
    zif, label_w = width * 0.08, None
    satz_cols = len(section["spalten"]) == 3
    amount_w = width * 0.19
    if satz_cols:
        satz_w = width * 0.09
        label_w = width - zif - satz_w - 2 * amount_w
        widths = [zif, label_w, satz_w, amount_w, amount_w]
        head = ["", "", *section["spalten"]]
    elif section["titel"].startswith("I."):
        label_w = width - zif - 2 * amount_w
        widths = [zif, label_w, amount_w, amount_w]       # deductions in the middle, totals on the right
        head = ["", "", "Abzüge CHF", section["spalten"][0]]
    else:
        label_w = width - zif - amount_w
        widths = [zif, label_w, amount_w]
        head = ["", "", section["spalten"][0]]
    ncol = len(widths)
    data = [[P(section["titel"].upper(), "bold")] + [""] * (ncol - 1), [P(h, "small") for h in head]]
    style = [("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5), ("LEFTPADDING", (0, 0), (-1, -1), 3),
             ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
             ("LINEBELOW", (0, 1), (-1, 1), 0.6, INK)]
    data[0][0] = Paragraph(f"<font color='white'><b>{escape(section['titel'].upper())}</b></font>", ST["base"])

    def amount(v, hand):
        return "" if v is None or hand else chf(v)

    for line in section["zeilen"]:
        r = len(data)
        bold = line["art"] == "total"
        text = P(line["label"], "cell") if not bold else Paragraph(f"<b>{escape(line['label'])}</b>", ST["cell"])
        if line["von_hand"]:
            text = Paragraph(f"<font color='#78716c'>{escape(line['label'])}</font>", ST["cell"])
        nr = Paragraph(f"<b>{line['nr']}</b>", ST["cell"])
        sign = "– " if line["art"] == "abzug" and line["nr"] not in ("220", "221", "225", "230", "235", "280") else ""
        if satz_cols:
            data.append([nr, text, line["satz"], amount(line["entgelt"], line["von_hand"]),
                         (sign + amount(line["steuer"], line["von_hand"])) if amount(line["steuer"], line["von_hand"]) else ""])
            boxes = [3, 4] if line["art"] == "satz" else [4]
        elif ncol == 4:
            value = amount(line["entgelt"], line["von_hand"])
            data.append([nr, text, value if line["art"] == "abzug" else "", "" if line["art"] == "abzug" else value])
            boxes = [2] if line["art"] == "abzug" else [3]
        else:
            data.append([nr, text, amount(line["entgelt"], line["von_hand"])])
            boxes = [2]
        for c in boxes:
            style += [("BOX", (c, r), (c, r), 0.5, MUTED),
                      ("BACKGROUND", (c, r), (c, r), SOFT if line["von_hand"] else colors.white)]
        if bold:
            style += [("FONT", (2, r), (-1, r), "Helvetica-Bold", 9), ("LINEABOVE", (0, r), (1, r), 0.4, RULE)]
        else:
            style.append(("FONT", (2, r), (-1, r), "Helvetica", 9))
        if line["nr"] in ("500", "510"):
            style += [("LINEABOVE", (0, r), (-1, r), 1, INK), ("BOX", (-1, r), (-1, r), 1.2, INK)]
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle(style))
    return t


def _mwst_unterschrift(width: float) -> KeepTogether:
    lines = [["Datum", "Buchstelle / Kontaktperson", "Telefon", "Rechtsverbindliche Unterschrift"]]
    t = Table([[P(h, "small") for h in lines[0]], ["", "", "", ""]],
              colWidths=[width * 0.16, width * 0.32, width * 0.18, width * 0.34], rowHeights=[None, 12 * mm])
    t.setStyle(TableStyle([("LINEBELOW", (0, 1), (-1, 1), 0.6, INK), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    return KeepTogether([P("Der/die Unterzeichnende bestätigt die Richtigkeit der Angaben.", "base"), Spacer(1, 2 * mm), t])


def _mwst_herkunft(h: dict, width: float) -> list:
    """MWST report: the rows behind each Ziffer, with a reconciliation line per group."""
    story = [PageBreak(), P(f"Herkunft der Zahlen {h['periode']}", "title"),
             P(f"{h['zeilen']} Buchungszeilen mit MWST-Code vom {d(h['von'])} bis {d(h['bis'])}"
               + (" · vereinnahmte Entgelte: Rechnungen und Kreditoren zählen anteilig am Zahlungsdatum"
                  if h["abrechnungsart"] == "vereinnahmt" else "")
               + ". Entgelt und Steuer mit Vorzeichen wie in der Abrechnung (Gutschriften negativ).", "small")]
    if not h["gruppen"]:
        return story + [Spacer(1, 4 * mm), P("Keine Buchungen mit MWST-Code in diesem Zeitraum.")]
    widths = [width * w for w in (0.1, 0.14, 0.34, 0.08, 0.06, 0.14, 0.14)]
    for g in h["gruppen"]:
        data = [["Datum", "Beleg", "Text", "Konto", "Code", "Entgelt", "Steuer"]]
        for z in g["zeilen"]:
            text = z["text"] + (f" ({z['hinweis']})" if z["hinweis"] else "")
            data.append([d(z["datum"]), z["beleg"], P(text, "cell"), z["konto"], z["code"],
                         _signed(z["entgelt"]), _signed(z["steuer"])])
        data.append(["", "", "Summe der Buchungen", "", "", _signed(g["entgelt"]), _signed(g["steuer"])])
        totals = [len(data) - 1]
        ze, zs = g["ziffer_entgelt"], g["ziffer_steuer"]
        data.append(["", "", f"Ziffer {g['nummer']} in der Abrechnung", "", "",
                     _signed(ze) if ze is not None else "", _signed(zs) if zs is not None else ""])
        notes = []
        if g["differenz_entgelt"]:
            notes.append(f"Entgelt weicht um {chf(g['differenz_entgelt'])} ab — bitte prüfen.")
        if g["differenz_steuer"]:
            notes.append(f"Steuer: die ESTV rechnet Entgelt × Satz; gebucht sind {chf(g['steuer'])}, Differenz "
                         f"{_signed(g['differenz_steuer'])} (Rundung je Beleg, wird beim Buchen ausgeglichen).")
        if g.get("hinweis"):
            notes.append(g["hinweis"])
        block = [P(f"Ziffer {g['nummer']} · {g['label']}", "h2"),
                 _grid(data, widths, total_rows=totals, zebra=True, right_cols=(5, 6))]
        block += [P(n, "small") for n in notes]
        story += ([KeepTogether(block)] if len(data) < 25 else block) + [Spacer(1, 2 * mm)]
    return story


def _signed(value) -> str:
    v = Decimal(str(value or 0))
    return "" if not v else (f"−{chf(-v)}" if v < 0 else chf(v))


# ---------- Abschlussdossier: tables, Belegordner pages, stamps ----------

def _cellify(value, wrap: bool):
    if isinstance(value, (Paragraph, Table)):
        return value
    text = "" if value is None else str(value)
    if wrap and (len(text) > 18 or "\n" in text):
        return Paragraph(escape(text).replace("\n", "<br/>"), ST["cell"])
    return text


def report_pdf(book: Book, title: str, blocks: list, subtitle: str = "", wide: bool = False) -> bytes:
    """A plain report from blocks: ("h2", text), ("text", text), ("small", text), ("page",), ("flowable", obj) or
    ("table", header, rows, widths, opts) — widths as fractions of the line, opts with
    `right` (column indexes), `totals` (row indexes into rows), `wrap` (columns that wrap)."""
    s = book.settings
    pagesize = landscape(A4) if wide else A4
    width = pagesize[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, title, s.firma, pagesize=pagesize, label=f"{s.firma} · {title}")
    story = [P(title, "title")]
    if subtitle:
        story.append(P(subtitle, "small"))
    story.append(Spacer(1, 4 * mm))
    for block in blocks:
        kind = block[0]
        if kind == "page":
            story.append(PageBreak())
        elif kind == "flowable":                   # e.g. a chart (reportlab Drawing)
            story += [block[1], Spacer(1, 3 * mm)]
        elif kind in ("h2", "text", "small", "bold"):
            story.append(P(block[1], {"text": "base"}.get(kind, kind)))
        elif kind == "table":
            header, rows, widths = block[1], block[2], block[3]
            opts = block[4] if len(block) > 4 else {}
            wrap = set(opts.get("wrap", ()))
            data = [list(header)] + [[_cellify(v, i in wrap) for i, v in enumerate(r)] for r in rows]
            if len(data) == 1:
                story.append(P("(keine)", "small"))
                continue
            t = _grid(data, [w * width for w in widths], zebra=opts.get("zebra", False),
                      total_rows=[t + 1 for t in opts.get("totals", ())], right_cols=tuple(opts.get("right", ())))
            heads = [h + 1 for h in opts.get("heads", ())]
            if heads:
                t.setStyle(TableStyle([s for h in heads for s in (
                    ("FONT", (0, h), (-1, h), "Helvetica-Bold", 8.5), ("TOPPADDING", (0, h), (-1, h), 7))]))
            story.append(t)
            story.append(Spacer(1, 3 * mm))
    doc.build(story)
    return buf.getvalue()


def _single_page(size, draw) -> "object":
    """A one-page PDF drawn on a reportlab canvas, returned as a pypdf page."""
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas as rl_canvas
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=size)
    draw(c, size)
    c.showPage()
    c.save()
    return PdfReader(io.BytesIO(buf.getvalue())).pages[0]


def stamp(page, text: str) -> None:
    """Put the Beleg stamp top right on a pypdf page (in place). Rotation is moved into
    the content first, so the stamp sits on the page as it is read."""
    if page.rotation % 360:
        try:
            page.transfer_rotation_to_content()
        except Exception:
            pass
    box = page.mediabox
    w, h = float(box.width), float(box.height)
    x0, y0 = float(box.left), float(box.bottom)

    def draw(c, size):
        c.setFont("Helvetica-Bold", 8)
        tw = c.stringWidth(text, "Helvetica-Bold", 8)
        x, y = x0 + w - tw - 10 * mm, y0 + h - 8 * mm
        c.setFillColor(colors.white)
        c.setStrokeColor(INK)
        c.setLineWidth(0.6)
        c.rect(x - 2 * mm, y - 1.8 * mm, tw + 4 * mm, 5.6 * mm, fill=1, stroke=1)
        c.setFillColor(INK)
        c.drawString(x, y, text)

    page.merge_page(_single_page((x0 + w, y0 + h), draw))


def image_page_pdf(path) -> bytes:
    """A photo or scan (JPG, PNG …) fitted onto an A4 page."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas
    img = ImageReader(str(path))
    iw, ih = img.getSize()
    size = landscape(A4) if iw > ih * 1.15 else A4
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=size)
    margin = 12 * mm
    top = 16 * mm            # room for the stamp
    scale = min((size[0] - 2 * margin) / iw, (size[1] - margin - top) / ih)
    c.drawImage(img, (size[0] - iw * scale) / 2, margin + (size[1] - margin - top - ih * scale) / 2,
                iw * scale, ih * scale, preserveAspectRatio=True)
    c.showPage()
    c.save()
    return buf.getvalue()


def beleg_sheet_pdf(book: Book, beleg: str, title: str, rows: list, notes: list[str], missing: bool) -> bytes:
    """A page for a Beleg without its own document: the booking itself, and why there is no file."""
    s = book.settings
    width = A4[0] - 2 * SIDE
    buf = io.BytesIO()
    doc = _doc(buf, f"Beleg {beleg}", s.firma, label=f"{s.firma} · Beleg {beleg}")
    story = [P(f"Beleg {beleg}", "title"), P(title, "h2")]
    for note in notes:
        story.append(P(note, "bold" if missing else "base"))
    data = [["Datum", "Text", "Soll", "Haben", "Betrag"]]
    for r in rows:
        data.append([d(r.datum), P(r.text, "cell"), r.soll, r.haben, chf(r.betrag)])
    story += [Spacer(1, 4 * mm), _grid(data, [w * width for w in (0.13, 0.5, 0.1, 0.1, 0.17)], right_cols=(4,))]
    doc.build(story)
    return buf.getvalue()

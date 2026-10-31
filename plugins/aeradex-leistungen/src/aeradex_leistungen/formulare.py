"""Paper forms to fill in by hand, printed with what aeradex already knows: number, QR code, customer, object and
the people (Regie-/Material-Rapport), or name, week and dates (Wochenrapport). The QR code names the form, so the scan
finds its way back (rapporte.parse_qr).

A book can bring its own designed forms: `leistungen/formulare/{regie,material,woche}.pdf` with the positions to
print at in `leistungen/formulare/felder.yaml` (points of that PDF's page, from its bottom left):

    regie:                         # also material
      nr: [492, 719]
      auftraggeber: [148, 693]
      adresse: [[148, 675], [148, 657]]
      baustelle: [148, 640]
      qr: [490, 632, 58]           # x, y, size
      personen: [47, [252, 234, 217, 199]]   # x, baselines of the empty person rows (optional)
    woche:
      name: [140, 695]
      woche: [458, 695]
      tage: [[63, 644], [63, 541], [63, 436], [63, 331], [63, 227], [63, 123]]
      qr: [505, 702, 46]

Without them aeradex draws plain forms of its own. The printed PDF lands in berichte/ (outside git).
"""
from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from aeradex import invoices
from aeradex.book import Book, BookError
from aeradex.files import read_yaml

from . import daten, rapporte

W, H = A4
DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"]


def folder(book: Book) -> Path:
    return book.root / "leistungen" / "formulare"


def layout(book: Book, art: str) -> tuple[Path, dict] | None:
    """The book's own form for `art` and where to print, or None."""
    pdf, fields = folder(book) / f"{art}.pdf", folder(book) / "felder.yaml"
    if not pdf.is_file() or not fields.is_file():
        return None
    spec = (read_yaml(fields) or {}).get(art)
    return (pdf, spec) if spec else None


def _page_size(pdf: Path) -> tuple[float, float]:
    from pypdf import PdfReader
    box = PdfReader(str(pdf)).pages[0].mediabox
    return float(box.width), float(box.height)


def _qr(c: Canvas, text: str, x: float, y: float, size: float) -> None:
    widget = QrCodeWidget(text, barLevel="M")
    x0, y0, x1, y1 = widget.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, x, y)


def _text(c: Canvas, value: str, pos, size: float = 10.5) -> None:
    if value and pos:
        c.setFont("Helvetica", size)
        c.drawString(float(pos[0]), float(pos[1]), str(value))


def _target(book: Book, ziel: str) -> dict:
    """«P:P0003» / «K:K0002» → customer, object, address lines for the head of the form."""
    out = {"kunde": "", "projekt": "", "auftraggeber": "", "adresse": [], "baustelle": ""}
    if not ziel:
        return out
    kind, _, nr = ziel.partition(":")
    if kind == "P":
        p = daten.project(book, nr)
        out.update(projekt=p["nummer"], kunde=p["kunde"], baustelle=daten.object_text(p))
    elif kind == "K":
        out["kunde"] = invoices.customer(book, nr)["nummer"]
    else:
        raise BookError("Kunde oder Objekt wählen")
    c = invoices.customer(book, out["kunde"])
    a = c.get("adresse") or {}
    out["auftraggeber"] = invoices.qr.invoice_name(c) + (f", {c['name']}" if c.get("firma") and c.get("name") else "")
    out["adresse"] = [x for x in (" ".join(y for y in (a.get("strasse"), str(a.get("nr") or "")) if y),
                                  " ".join(y for y in (str(a.get("plz") or ""), a.get("ort")) if y)) if x]
    return out


def _people(book: Book) -> list[str]:
    return [p["name"] for p in daten.people(book).values() if p.get("aktiv", True)]


# ---------- plain forms of aeradex's own ----------

def _plain_head(c: Canvas, book: Book, title: str, nr: str = "") -> float:
    s = book.settings
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20 * mm, H - 20 * mm, s.firma)
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, H - 25 * mm, " · ".join([*s.address_lines, s.get("telefon") or "", s.get("email") or ""]).strip(" ·"))
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, H - 38 * mm, title)
    if nr:
        c.setFont("Helvetica", 11)
        c.drawRightString(W - 20 * mm, H - 38 * mm, f"Nr. {nr}")
    return H - 46 * mm


def _line(c: Canvas, label: str, value: str, y: float) -> float:
    c.setFont("Helvetica", 9.5)
    c.drawString(20 * mm, y, label)
    c.line(48 * mm, y - 1.5, 140 * mm, y - 1.5)
    _text(c, value, (49 * mm, y + 1))
    return y - 7.5 * mm


def _grid(c: Canvas, x: float, y: float, widths: list[float], rows: int, row_h: float, head: list[str]) -> float:
    total = sum(widths)
    c.setFont("Helvetica-Bold", 8.5)
    xs = [x]
    for w in widths:
        xs.append(xs[-1] + w)
    for i, label in enumerate(head):
        c.drawString(xs[i] + 1.5 * mm, y - 4.5 * mm, label)
    c.setLineWidth(0.6)
    for r in range(rows + 2):
        c.line(x, y - r * row_h, x + total, y - r * row_h)
    for xx in xs:
        c.line(xx, y, xx, y - (rows + 1) * row_h)
    return y - (rows + 1) * row_h


def _signatures(c: Canvas, book: Book, y: float) -> None:
    c.setFont("Helvetica", 8.5)
    for x, label in ((20 * mm, book.settings.firma), (115 * mm, "Der Auftraggeber")):
        c.drawString(x, y, label)
        c.line(x, y - 14 * mm, x + 75 * mm, y - 14 * mm)
        c.drawString(x, y - 18 * mm, "Datum / Unterschrift")


def _plain_report(c: Canvas, book: Book, art: str, nr: str, head: dict) -> None:
    year = nr.split("-")[1] if nr.count("-") >= 2 else date.today().year
    y = _plain_head(c, book, f"{rapporte.ARTEN[art]} {year}", nr)
    y = _line(c, "Auftraggeber:", head["auftraggeber"], y)
    adr = head["adresse"] + ["", ""]
    y = _line(c, "Adresse:", adr[0], y)
    y = _line(c, "", adr[1], y)
    y = _line(c, "Baustelle:", head["baustelle"], y)
    width = W - 40 * mm
    if art == "regie":
        c.setFont("Helvetica-Bold", 9)
        c.drawString(20 * mm, y, "Ausgeführte Arbeiten")
        y -= 2 * mm
        c.rect(20 * mm, y - 14 * 6.5 * mm, width, 14 * 6.5 * mm)
        for i in range(1, 14):
            c.line(20 * mm, y - i * 6.5 * mm, 20 * mm + width, y - i * 6.5 * mm)
        y -= 14 * 6.5 * mm + 6 * mm
        names = _people(book)[:6]
        rows = max(len(names) + 2, 6)
        widths = [40 * mm] + [16 * mm] * 6 + [14 * mm, 14 * mm]
        bottom = _grid(c, 20 * mm, y, widths, rows, 7 * mm, ["Name / Datum", "", "", "", "", "", "", "Total", "Ansatz"])
        c.setFont("Helvetica", 9.5)
        for i, n in enumerate(names):
            c.drawString(21.5 * mm, y - (i + 2) * 7 * mm + 2 * mm, n)
        _signatures(c, book, bottom - 10 * mm)
    else:
        widths = [22 * mm, 82 * mm, 18 * mm, 18 * mm, 15 * mm, 15 * mm]
        bottom = _grid(c, 20 * mm, y - 2 * mm, widths, 24, 7 * mm, ["Datum", "Material", "Einheit", "Menge", "Preis", "Betrag"])
        _signatures(c, book, bottom - 10 * mm)


def _plain_week(c: Canvas, book: Book, name: str, kw: int, jahr: int, monday: date) -> None:
    y = _plain_head(c, book, f"Wochenrapport {jahr}")
    c.setFont("Helvetica", 9.5)
    c.drawString(20 * mm, y, "Name:")
    _text(c, name, (35 * mm, y))
    c.drawString(130 * mm, y, "Woche:")
    _text(c, f"KW {kw}", (147 * mm, y))
    y -= 6 * mm
    widths = [32 * mm, 88 * mm, 20 * mm, 30 * mm]
    rows_per_day = 5
    c.setFont("Helvetica-Bold", 8.5)
    for i, label in enumerate(["Datum", "Kunde / Objekt", "Stunden", "Bemerkung"]):
        c.drawString(20 * mm + sum(widths[:i]) + 1.5 * mm, y - 4.5 * mm, label)
    c.line(20 * mm, y, 20 * mm + sum(widths), y)
    y -= 6.5 * mm
    for d, day in enumerate(DAYS):
        top = y
        for r in range(rows_per_day + 1):
            c.line(20 * mm + widths[0], y, 20 * mm + sum(widths), y)
            if r == 0:
                c.setFont("Helvetica", 8.5)
                c.drawString(20 * mm + widths[0] + 1.5 * mm, y - 4.3 * mm, "Reisezeit")
            y -= 5.6 * mm
        c.setLineWidth(1)
        c.line(20 * mm, top, 20 * mm + sum(widths), top)
        c.setLineWidth(0.6)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(21.5 * mm, top - 4.3 * mm, day)
        c.setFont("Helvetica", 9)
        c.drawString(21.5 * mm, top - 9 * mm, (monday + timedelta(days=d)).strftime("%d.%m.%Y"))
        for i in range(len(widths) + 1):
            xx = 20 * mm + sum(widths[:i])
            c.line(xx, top, xx, y)
    c.line(20 * mm, y, 20 * mm + sum(widths), y)
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(20 * mm + widths[0] + widths[1] - 2 * mm, y - 5 * mm, "Total Stunden")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y - 16 * mm, "Visum:")
    c.line(35 * mm, y - 16.5 * mm, 110 * mm, y - 16.5 * mm)


# ---------- printing ----------

def _overlay(template: Path, pages: list[bytes]) -> bytes:
    """Each overlay page on a fresh copy of the template's first page."""
    from pypdf import PdfReader, PdfWriter
    out = PdfWriter()
    for data in pages:
        base = out.add_page(PdfReader(str(template)).pages[0])
        base.merge_page(PdfReader(io.BytesIO(data)).pages[0])
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue()


def _save(book: Book, name: str, data: bytes) -> str:
    target = book.root / "berichte" / "Formulare" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return book.rel(target)


def reports(book: Book, art: str, ziel: str = "", anzahl: int = 1) -> tuple[dict, list[Path]]:
    """`anzahl` Regie- or Material-Rapporte with the next numbers, for a customer/object (or blank)."""
    if art not in ("regie", "material"):
        raise BookError("Formular: regie oder material")
    if not 1 <= int(anzahl) <= 50:
        raise BookError("Anzahl 1 bis 50")
    head = _target(book, ziel)
    numbers, touched = rapporte.reserve(book, art, rapporte.book_year(book), anzahl)
    own = layout(book, art)
    pages = []
    size = _page_size(own[0]) if own else A4
    for nr in numbers:
        buf = io.BytesIO()
        c = Canvas(buf, pagesize=size)
        payload = rapporte.qr_payload(art, nr, head["kunde"], head["projekt"])
        if own:
            spec = own[1]
            _text(c, nr, spec.get("nr"), 12)
            _text(c, head["auftraggeber"], spec.get("auftraggeber"))
            for line, pos in zip(head["adresse"], spec.get("adresse") or []):
                _text(c, line, pos)
            _text(c, head["baustelle"], spec.get("baustelle"))
            if art == "regie" and spec.get("personen"):
                x, ys = spec["personen"]
                printed = {str(n).lower() for n in spec.get("vorgedruckt") or []}
                for name, y in zip([n for n in _people(book) if n.lower() not in printed], ys):
                    _text(c, name, (x, y), 9.5)
            q = spec.get("qr") or [W - 40 * mm, H - 75 * mm, 20 * mm]
            _qr(c, payload, q[0], q[1], q[2])
        else:
            _plain_report(c, book, art, nr, head)
            _qr(c, payload, W - 42 * mm, H - 62 * mm, 22 * mm)
        c.showPage()
        c.save()
        pages.append(buf.getvalue())
    data = _overlay(own[0], pages) if own else _merge(pages)
    name = f"{rapporte.ARTEN[art]} {numbers[0]}" + (f" bis {numbers[-1]}" if len(numbers) > 1 else "") + ".pdf"
    label = f"{len(numbers)} {rapporte.ARTEN[art]}" + (f" für {head['auftraggeber']}" if head["auftraggeber"] else "")
    return {"meldung": f"{label} ({', '.join(numbers)})", "datei": _save(book, name, data), "nummern": numbers}, touched


def weeks(book: Book, kw: str, personen: list[str] | None = None) -> tuple[dict, list[Path]]:
    """One Wochenrapport per person for the week `2027-W05` with name, week, dates and QR."""
    try:
        jahr, woche = (int(x) for x in str(kw).replace("W", "").split("-"))
        monday = date.fromisocalendar(jahr, woche, 1)
    except ValueError:
        raise BookError("Woche als JJJJ-Www, z.B. 2027-W05") from None
    people = daten.people(book)
    chosen = [p for p in personen or [] if p] or [nr for nr, p in people.items() if p.get("aktiv", True)]
    own = layout(book, "woche")
    pages = []
    size = _page_size(own[0]) if own else A4
    for nr in chosen:
        p = daten.person(book, nr)
        buf = io.BytesIO()
        c = Canvas(buf, pagesize=size)
        payload = f"{rapporte.QR_PREFIX}woche:{jahr}-W{woche:02d}:{p['nummer']}"
        if own:
            spec = own[1]
            _text(c, p["name"], spec.get("name"))
            _text(c, f"{woche}  ({monday:%d.%m.}–{monday + timedelta(days=5):%d.%m.})", spec.get("woche"), 9.5)
            for i, pos in enumerate(spec.get("tage") or []):
                _text(c, (monday + timedelta(days=i)).strftime("%d.%m.%Y"), pos, 9)
            q = spec.get("qr") or [W - 35 * mm, H - 50 * mm, 16 * mm]
            _qr(c, payload, q[0], q[1], q[2])
        else:
            _plain_week(c, book, p["name"], woche, jahr, monday)
            _qr(c, payload, W - 40 * mm, H - 44 * mm, 20 * mm)
        c.showPage()
        c.save()
        pages.append(buf.getvalue())
    data = _overlay(own[0], pages) if own else _merge(pages)
    path = _save(book, f"Wochenrapporte {jahr}-KW{woche:02d}.pdf", data)
    return {"meldung": f"{len(chosen)} Wochenrapport(e) KW {woche}/{jahr}", "datei": path}, []


def _merge(pages: list[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter
    out = PdfWriter()
    for data in pages:
        out.add_page(PdfReader(io.BytesIO(data)).pages[0])
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue()

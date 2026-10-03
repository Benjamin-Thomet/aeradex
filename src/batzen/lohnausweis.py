"""Lohnausweis (Formular 11): aggregate the closed payslips of a year and fill
the official AcroForm. Whole Franken only, as the form demands.

    Ziffer 1   Lohn                   = Brutto + Kinderzulagen + Korrekturen
    Ziffer 8   Bruttolohn total       = Ziffer 1
    Ziffer 9   AHV/IV/EO/ALV/NBUV     = AHV + ALV + NBU (Arbeitnehmer)
    Ziffer 10.1 BVG ordentlich        = BVG (Arbeitnehmer)
    Ziffer 11  Nettolohn              = 8 − 9 − 10.1
    Ziffer 12  Quellensteuerabzug     = Quellensteuer

KTG has no line of its own and is noted under Ziffer 15. The PDF keeps its form
fields editable so the preparer can adjust anything before signing.
"""
from __future__ import annotations

import calendar
import io
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .book import Book
from .files import parse_date
from .payroll import display_name, employee, payslips

FORM_PATH = Path(__file__).parent / "data" / "forms" / "lohnausweis_form11_de.pdf"
CHECKBOX_ON = "/Ja / Oui / Sì"


def _fr(value) -> int:
    return int(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _num_field(n: int) -> str:
    # The form's fields reformat their value; a grouped "1'640" is misread as 1.640.
    return str(n) if n else ""


def annual_totals(book: Book, year: int, nr: str) -> dict:
    slips = [p for p in payslips(book, year) if p["mitarbeiter"] == nr]
    closed = [p for p in slips if p.get("status") == "abgeschlossen"]

    def total(key):
        return sum((Decimal(str(p["werte"].get(key) or 0)) for p in closed), Decimal("0"))

    z1 = _fr(total("bruttolohn") + total("kinderzulagen") + total("korrektur"))
    z9 = _fr(total("ahv") + total("alv") + total("uvg"))
    z10_1 = _fr(total("bvg"))
    return {"monate": len(closed), "entwuerfe": len(slips) - len(closed),
            "z1": z1, "z8": z1, "z9": z9, "z10_1": z10_1, "z11": z1 - z9 - z10_1,
            "z12": _fr(total("quellensteuer")), "ktg": _fr(total("ktg")),
            "netto_ausbezahlt": total("nettolohn")}


def pensum_remark(book: Book, year: int, nr: str) -> str:
    """Ziffer 15: the Beschäftigungsgrad, with von–bis stretches when it changed."""
    slips = {int(p["monat"]): p for p in payslips(book, year)
             if p["mitarbeiter"] == nr and p.get("status") == "abgeschlossen"}
    runs: list[list] = []
    for m in range(1, 13):
        if m not in slips or slips[m]["werte"].get("lohnart") == "stunde":
            continue
        rate = float(slips[m]["werte"].get("pensum") or 100)
        if runs and runs[-1][0] == rate and runs[-1][2] == m - 1:
            runs[-1][2] = m
        else:
            runs.append([rate, m, m])
    if not runs or {r[0] for r in runs} == {100.0}:
        return ""
    if len(runs) == 1:
        return f"Beschäftigungsgrad: {runs[0][0]:g}%"
    parts = [f"{r:g}% ({date(year, a, 1):%d.%m.}–{date(year, b, calendar.monthrange(year, b)[1]):%d.%m.})"
             for r, a, b in runs]
    return "Beschäftigungsgrad: " + ", ".join(parts)


def build_pdf(book: Book, year: int, nr: str, on_date: date | None = None) -> bytes:
    emp = employee(book, nr)
    totals = annual_totals(book, year, nr)
    s = book.settings
    von = date(year, 1, 1)
    if emp.get("eintritt"):
        start = parse_date(emp["eintritt"])
        if start.year == year and start > von:
            von = start
    bis = date(year, 12, 31)
    if emp.get("austritt"):
        end = parse_date(emp["austritt"])
        if end.year == year:
            bis = end
    a = emp.get("adresse") or {}
    emp_block = "\n".join([display_name(emp)] +
                          [l for l in (" ".join(str(x) for x in (a.get("strasse"), a.get("nr")) if x),
                                       " ".join(str(x) for x in (a.get("plz"), a.get("ort")) if x)) if l])
    employer = [s.firma] + s.address_lines + ([f"Tel. {s.get('telefon')}"] if s.get("telefon") else [])
    remarks = [r for r in (pensum_remark(book, year, nr),
                           f"KTG-Abzug Arbeitnehmer: {totals['ktg']:,}".replace(",", "'") if totals["ktg"] else "") if r]
    on_date = on_date or date.today()
    fields = {
        "OptionKreuzOhneRahmen_A": CHECKBOX_ON,
        "OptionKreuzOhneRahmen_13_1_1": CHECKBOX_ON,
        "AHVLinks_C": emp.get("ahv_nr") or "",
        "TextLinks_C-GebDatum": parse_date(emp["geburtsdatum"]).strftime("%d.%m.%Y") if emp.get("geburtsdatum") else "",
        "TextLinks_D": str(year),
        "TextLinks_E-von": von.strftime("%d.%m.%Y"),
        "TextLinks_E-bis": bis.strftime("%d.%m.%Y"),
        "TextMehrzeiligLinks_Empfaenger": emp_block,
        "DezZahlNull_1": _num_field(totals["z1"]),
        "DezZahlNull_8": _num_field(totals["z8"]),
        "DezZahlNull_9": _num_field(totals["z9"]),
        "DezZahlNull_10_1": _num_field(totals["z10_1"]),
        "DezZahlNull_11": _num_field(totals["z11"]),
        "DezZahlNull_12": _num_field(totals["z12"]),
        "TextLinks_15_1": remarks[0] if remarks else "",
        "TextLinks_15_2": remarks[1] if len(remarks) > 1 else "",
        "TextLinks_I": f"{s.adresse.get('ort') or ''}, {on_date:%d.%m.%Y}".strip(", "),
        "TextMehrzeiligLinks_Bestaetigung": "\n".join(employer),
    }
    writer = PdfWriter()
    writer.append(PdfReader(str(FORM_PATH)))
    for page in writer.pages:
        writer.update_page_form_field_values(page, fields)
    writer.set_need_appearances_writer(True)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()

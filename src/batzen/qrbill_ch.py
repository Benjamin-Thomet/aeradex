"""Swiss QR-bill generation.

The bill itself — payload and the certified slip layout — comes from the `qrbill`
library, which tracks the Swiss Implementation Guidelines. This module is the
adapter: it maps the book settings, customer and invoice onto qrbill's inputs,
decides the reference type, and draws the result onto a ReportLab canvas.

Reference type follows the account, as the spec requires:

    QR-IBAN (institution ID 30000-31999) -> QRR + a 27-digit structured reference
    ordinary IBAN                        -> SCOR + an ISO 11649 'RF' reference

A QR-IBAN is a separate account number the bank issues; it is not the ordinary
IBAN and cannot be derived from it. QRR is therefore off the table without one —
but SCOR, the international creditor reference, is allowed on any IBAN and is
what gives every invoice a machine-readable reference. NON (no reference at all)
remains valid and is still honoured on invoices issued before SCOR existed here.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from qrbill.bill import QRBill
from reportlab.graphics import renderPDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas
from svglib.svglib import svg2rlg



REFERENCE_LENGTH = 27  # 26 digits plus a check digit
MAX_ADDITIONAL_INFO = 140

# Mod-10 recursive ("ESR") carry table used for the QRR check digit.
_MOD10_TABLE = (
    (0, 9, 4, 6, 8, 2, 7, 1, 3, 5),
    (9, 4, 6, 8, 2, 7, 1, 3, 5, 0),
    (4, 6, 8, 2, 7, 1, 3, 5, 0, 9),
    (6, 8, 2, 7, 1, 3, 5, 0, 9, 4),
    (8, 2, 7, 1, 3, 5, 0, 9, 4, 6),
    (2, 7, 1, 3, 5, 0, 9, 4, 6, 8),
    (7, 1, 3, 5, 0, 9, 4, 6, 8, 2),
    (1, 3, 5, 0, 9, 4, 6, 8, 2, 7),
    (3, 5, 0, 9, 4, 6, 8, 2, 7, 1),
    (5, 0, 9, 4, 6, 8, 2, 7, 1, 3),
)


# ---------- IBAN ----------

def normalize_iban(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def iban_is_valid(iban: str) -> bool:
    """ISO 13616: format plus the mod-97 checksum."""
    iban = normalize_iban(iban)
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    try:
        digits = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(digits) % 97 == 1


def is_qr_iban(iban: str) -> bool:
    """QR-IBANs carry an institution ID of 30000-31999 and mandate a QRR reference."""
    iban = normalize_iban(iban)
    if len(iban) != 21 or not iban[4:9].isdigit():
        return False
    return 30000 <= int(iban[4:9]) <= 31999


def iban_problem(iban: str) -> str | None:
    """Human-readable reason this IBAN can't produce a valid QR-bill, or None."""
    iban = normalize_iban(iban)
    if not iban:
        return "Keine IBAN hinterlegt."
    if iban[:2] not in ("CH", "LI"):
        return "Eine Swiss-QR-Rechnung braucht eine Schweizer (CH) oder Liechtensteiner (LI) IBAN."
    if len(iban) != 21:
        return f"Eine Schweizer IBAN hat 21 Zeichen; diese hat {len(iban)}."
    if not iban_is_valid(iban):
        return "Die IBAN-Prüfsumme stimmt nicht — prüfen Sie auf Tippfehler."
    return None


def reference_type_for(iban: str) -> str:
    """The reference type this account requires: QRR on a QR-IBAN, else SCOR."""
    return "QRR" if is_qr_iban(iban) else "SCOR"


def reference_is_compatible(reference_type: str, iban: str) -> bool:
    """Whether an already-issued reference still works with this account.

    The one hard rule is QRR: it requires a QR-IBAN, and a QR-IBAN requires it.
    SCOR and NON are both fine on an ordinary IBAN, so an old NON invoice keeps
    rendering after we start issuing SCOR."""
    return ((reference_type or "NON").upper() == "QRR") == is_qr_iban(iban)


# ---------- QRR reference ----------

def mod10r_check_digit(digits: str) -> int:
    """Recursive mod-10 check digit, the Swiss QRR/ESR algorithm."""
    carry = 0
    for ch in digits:
        carry = _MOD10_TABLE[carry][int(ch)]
    return (10 - carry) % 10


def make_qrr_reference(invoice_number: str, prefix: str = "") -> str:
    """Build the 27-digit QRR reference for an invoice.

    The invoice's own sequence number is the payload, zero-padded, optionally
    behind a bank-assigned prefix. Distinct invoices therefore always get
    distinct references, which is what makes payments reconcilable.
    """
    prefix = re.sub(r"\D", "", prefix or "")
    sequence = re.sub(r"\D", "", invoice_number or "") or "0"
    body_len = REFERENCE_LENGTH - 1
    if len(prefix) + len(sequence) > body_len:
        raise ValueError(
            f"Der Referenz-Präfix {prefix!r} lässt keinen Platz für Rechnung {invoice_number}: "
            f"eine QRR-Referenz fasst {body_len} Ziffern plus eine Prüfziffer."
        )
    body = prefix + sequence.zfill(body_len - len(prefix))
    return body + str(mod10r_check_digit(body))


# ---------- SCOR reference (ISO 11649) ----------

SCOR_MAX_PAYLOAD = 21  # 'RF' + 2 check digits + up to 21 alphanumeric characters


def _mod97(value: str) -> int:
    """ISO 7064 mod-97-10 over a string, letters counted as A=10 … Z=35."""
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in value)
    return int(digits) % 97


def scor_check_digits(payload: str) -> str:
    """The two check digits that follow 'RF' for this payload."""
    return f"{98 - _mod97(payload + 'RF00'):02d}"


def make_scor_reference(invoice_number: str, prefix: str = "") -> str:
    """Build the ISO 11649 creditor reference ('RF..') for an invoice.

    Unlike QRR this needs no QR-IBAN: any account can carry it, which is what
    lets every invoice go out with a reference the bank can match a payment
    against. The invoice number itself is the payload, so the reference stays
    readable — INV-0247 becomes RF37INV0247.
    """
    payload = re.sub(r"[^0-9A-Z]", "", (prefix or "").upper() + (invoice_number or "").upper())
    if not payload:
        payload = "0"
    if len(payload) > SCOR_MAX_PAYLOAD:
        raise ValueError(
            f"Die Referenz für Rechnung {invoice_number} wäre {len(payload)} Zeichen lang; "
            f"eine SCOR-Referenz fasst {SCOR_MAX_PAYLOAD} Zeichen. Kürzen Sie das "
            "Rechnungsnummern-Präfix oder den Referenz-Präfix."
        )
    return f"RF{scor_check_digits(payload)}{payload}"


def make_reference(reference_type: str, invoice_number: str, prefix: str = "") -> str:
    """The reference an invoice of this type carries. NON carries none."""
    kind = (reference_type or "NON").upper()
    if kind == "QRR":
        return make_qrr_reference(invoice_number, prefix)
    if kind == "SCOR":
        return make_scor_reference(invoice_number, prefix)
    return ""


def format_reference(reference: str) -> str:
    """References are shown in blocks — QRR 21 00000 00003 13947 14300 09017
    (fives from the right), SCOR RF37 INV0 247 (fours from the left)."""
    ref = re.sub(r"\s", "", reference or "")
    if not ref:
        return ""
    if ref[:2].upper() == "RF":
        return " ".join(ref[i:i + 4] for i in range(0, len(ref), 4))
    head = len(ref) % 5 or 5
    blocks = [ref[:head]] + [ref[i:i + 5] for i in range(head, len(ref), 5)]
    return " ".join(b for b in blocks if b)


# ---------- Bill construction ----------

def _amount(value) -> str:
    """qrbill takes the amount as a string or Decimal, never a float."""
    try:
        amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Der Rechnungsbetrag {value!r} ist keine Zahl.")
    if amount <= 0:
        raise ValueError("Der Betrag einer QR-Rechnung muss grösser als null sein.")
    return f"{amount:.2f}"


CREDITOR_NAME_MAX = 70  # the name field of a QR bill, per the spec


def creditor_name(settings) -> str:
    """The payee name as the QR bill carries it, c/o line included — the Swiss
    standard has no c/o field, so it belongs to the name or it vanishes from the slip."""
    name = settings.firma
    care_of = (settings.get("co") or "").strip()
    if not care_of:
        return name
    if not re.match(r"^c[/\-. ]?o\b", care_of, re.IGNORECASE):
        care_of = f"c/o {care_of}"
    return f"{name} {care_of}".strip()


def _creditor(settings) -> dict:
    a = settings.adresse
    return {
        "name": creditor_name(settings),
        "street": a.get("strasse") or "",
        "house_num": str(a.get("nr") or ""),
        "pcode": str(a.get("plz") or ""),
        "city": a.get("ort") or "",
        "country": a.get("land") or "CH",
    }


def _debtor(customer: dict | None) -> dict | None:
    """The debtor block, or None when we lack a usable address — a bill without
    the block is still payable, an incomplete one is invalid."""
    if not customer:
        return None
    a = customer.get("adresse") or {}
    street = " ".join(str(p) for p in (a.get("strasse"), a.get("nr")) if p).strip()
    if not (a.get("plz") and a.get("ort") and street):
        return None
    return {
        "name": invoice_name(customer)[:70],
        "street": street,
        "pcode": str(a.get("plz")),
        "city": a.get("ort"),
        "country": a.get("land") or "CH",
    }


def invoice_name(customer: dict) -> str:
    """Who the invoice is addressed to: the company, or the person when
    `rechnung_an: person` — letter and QR debtor always agree."""
    firma = (customer.get("firma") or "").strip()
    person = (customer.get("name") or "").strip()
    if customer.get("rechnung_an") == "person" or not firma:
        return person or firma
    return firma


_QRBILL_LANGUAGES = ("en", "de", "fr", "it")


def build_bill(invoice: dict, customer: dict | None, settings, language: str = "de") -> QRBill:
    """Assemble the QRBill. Raises ValueError when the data can't make a valid bill."""
    iban = normalize_iban(settings.get("iban"))
    problem = iban_problem(iban)
    if problem:
        raise ValueError(problem)
    if not settings.firma:
        raise ValueError("Für eine QR-Rechnung ist der Firmenname erforderlich.")
    if len(creditor_name(settings)) > CREDITOR_NAME_MAX:
        raise ValueError(f"Firmenname inkl. c/o ist länger als {CREDITOR_NAME_MAX} Zeichen.")
    stored = (invoice.get("referenz_typ") or "NON").upper()
    if not reference_is_compatible(stored, iban):
        raise ValueError(
            f"{invoice['nummer']} wurde mit einer {stored}-Referenz ausgestellt, aber die IBAN "
            f"verlangt jetzt {reference_type_for(iban)}. Rechnung stornieren und neu ausstellen.")
    lang = (language or "de").lower()
    return QRBill(
        account=iban,
        creditor=_creditor(settings),
        debtor=_debtor(customer),
        amount=_amount(invoice["total"]),
        currency=(invoice.get("waehrung") or "CHF").upper(),
        reference_number=invoice.get("referenz") or None,
        additional_information=f"Rechnung {invoice['nummer']}"[:MAX_ADDITIONAL_INFO],
        language=lang if lang in _QRBILL_LANGUAGES else "de",
    )


def draw_payment_slip(canvas: Canvas, invoice: dict, customer: dict | None, settings,
                      language: str = "de") -> None:
    """Draw the QR-bill across the bottom 105 mm of the current page."""
    bill = build_bill(invoice, customer, settings, language)
    buf = io.StringIO()
    bill.as_svg(buf)
    drawing = svg2rlg(io.BytesIO(buf.getvalue().encode("utf-8")))
    if drawing is None:
        raise ValueError("Die QR-Rechnung konnte nicht aus ihrem SVG gerendert werden.")
    scale = A4[0] / drawing.width
    drawing.scale(scale, scale)
    drawing.width *= scale
    drawing.height *= scale
    renderPDF.draw(drawing, canvas, 0, 0)

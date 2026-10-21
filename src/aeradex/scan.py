"""Scanned pages from the phone → one PDF for the Belegeingang. The browser finds the sheet, lets the person move
the corners and flattens the photo; here the pages (JPEG/PNG) are only checked and put into a PDF of A4 width."""
from __future__ import annotations

import io

from .book import BookError

MAX_PAGES = 20
MAX_PAGE_BYTES = 15 * 1024 * 1024
A4_WIDTH = 595.28                       # points


def kind(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return ""


def pages_to_pdf(pages: list[bytes], title: str = "Beleg") -> bytes:
    """One page per image, each as wide as A4 and as tall as the image's proportions ask."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    if not pages:
        raise BookError("Keine Seite gescannt")
    if len(pages) > MAX_PAGES:
        raise BookError(f"Höchstens {MAX_PAGES} Seiten pro Beleg")
    out = io.BytesIO()
    pdf = canvas.Canvas(out)
    pdf.setTitle(title)
    pdf.setCreator("aeradex")
    for n, data in enumerate(pages, 1):
        if len(data) > MAX_PAGE_BYTES:
            raise BookError(f"Seite {n} ist zu gross")
        if not kind(data):
            raise BookError(f"Seite {n} ist kein Bild (JPEG oder PNG)")
        try:
            img = ImageReader(io.BytesIO(data))
            w, h = img.getSize()
        except Exception:
            raise BookError(f"Seite {n} lässt sich nicht lesen") from None
        if not w or not h:
            raise BookError(f"Seite {n} ist leer")
        height = A4_WIDTH * h / w
        pdf.setPageSize((A4_WIDTH, height))
        pdf.drawImage(img, 0, 0, A4_WIDTH, height)
        pdf.showPage()
    pdf.save()
    return out.getvalue()

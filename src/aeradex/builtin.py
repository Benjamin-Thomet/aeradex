"""What aeradex brings itself, offered through the same hooks as any plugin.

The camt.053 import is the reference implementation of a bank format: a
plugin for another bank works exactly the same way.
"""
from __future__ import annotations

from pathlib import Path

from .plugins import BankFormat, BelegLeser, hookimpl

AERADEX_PLUGIN_API = 1
DATA = Path(__file__).parent / "data"


@hookimpl
def aeradex_kontenplaene():
    return {p.stem: p for p in sorted((DATA / "kontenplaene").glob("*.yaml"))}


@hookimpl
def aeradex_qst_tarife():
    return DATA / "qst_tarife"


def _is_camt(filename: str, data: bytes) -> bool:
    head = data[:4096]
    return b"<" in head[:200] and (b"camt.053" in head or b"BkToCstmrStmt" in data[:20000])


def _parse_camt(data: bytes, book):
    from . import bank
    return bank.parse(data)


@hookimpl
def aeradex_bank_formats():
    return [BankFormat(name="camt053", label="ISO 20022 camt.053 (XML)", suffixes=(".xml",),
                       detect=_is_camt, parse=_parse_camt)]


# ---------- reading supplier bills (Kreditoren drafts) ----------

def _read_qr(book, path, text):
    from . import erfassung
    return erfassung.read_qr(book, path)


def _read_text(book, path, text):
    from . import erfassung
    if path.suffix.lower() in (".txt", ".md"):
        found = path.read_text(encoding="utf-8", errors="replace")
        return {**erfassung.parse_text(found, book.settings.firma), "_text": found} if found.strip() else None
    found = erfassung.pdf_text(path)
    if len(found.strip()) < 40:
        return None
    return {**erfassung.parse_text(found, book.settings.firma), "_text": found}


def _read_ocr(book, path, text):
    from . import erfassung
    if text:
        return None                     # the PDF had a text layer: no OCR needed
    found = erfassung.ocr_text(path)
    if len(found.strip()) < 20:
        return None
    return {**erfassung.parse_text(found, book.settings.firma), "_text": found}


@hookimpl
def aeradex_beleg_leser():
    return [BelegLeser("qr", "QR", 10, _read_qr),
            BelegLeser("text", "Text", 50, _read_text),
            BelegLeser("ocr", "OCR", 80, _read_ocr)]

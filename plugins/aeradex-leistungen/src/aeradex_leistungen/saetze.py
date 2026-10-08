"""Which price applies: hourly rates and product prices.

Hourly rate (fixed on the entry when it is recorded, so later changes never alter history):
project `satz` > the customer's own price of the Leistungsart > customer `stundensatz` > Leistungsart price >
the person's `satz`.
Product price: the customer's own price > the product's price.
"""
from __future__ import annotations

from decimal import Decimal

from aeradex import invoices
from aeradex.book import Book, BookError

from . import daten


def hourly_rate(book: Book, wer: str, kunde: str = "", projekt: str = "", leistung: str = "") -> Decimal:
    if projekt:
        p = daten.project(book, projekt)
        if p.get("satz") not in (None, ""):
            return daten.money(p["satz"])
    art = daten.product(book, leistung) if leistung else None
    if art and kunde and (art.get("kundenpreise") or {}).get(kunde) not in (None, ""):
        return daten.money(art["kundenpreise"][kunde])
    if kunde:
        c = invoices.customer(book, kunde)
        if c.get("stundensatz") not in (None, ""):
            return daten.money(c["stundensatz"])
    if art and art.get("preis") not in (None, ""):
        return daten.money(art["preis"])
    rate = daten.person(book, wer)["satz"]
    if rate is None:
        raise BookError(f"Kein Stundensatz für {wer} — unter Leistungen → Stammdaten den Verrechnungssatz erfassen "
                        "(oder beim Kunden/Projekt einen Satz hinterlegen)")
    return rate


def rate_source(book: Book, wer: str, kunde: str = "", projekt: str = "", leistung: str = "") -> str:
    """Where the rate comes from, for the UI: 'Projekt', 'Kunde', 'Leistungsart', 'Person' or ''."""
    if projekt and daten.project(book, projekt).get("satz") not in (None, ""):
        return "Projekt"
    art = daten.product(book, leistung) if leistung else None
    if art and kunde and (art.get("kundenpreise") or {}).get(kunde) not in (None, ""):
        return "Kundenpreis"
    if kunde and invoices.customer(book, kunde).get("stundensatz") not in (None, ""):
        return "Kunde"
    if art and art.get("preis") not in (None, ""):
        return "Leistungsart"
    return "Person" if daten.person(book, wer)["satz"] is not None else ""


def product_price(prod: dict, kunde: str = "") -> Decimal:
    own = (prod.get("kundenpreise") or {}).get(kunde)
    return daten.money(own if own not in (None, "") else prod.get("preis"))

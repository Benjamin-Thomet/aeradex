"""Which price applies: hourly rates and product prices.

Hourly rate (fixed on the entry when it is recorded, so later changes never alter history):
project `satz` > customer `stundensatz` > the person's `satz`.
Product price: the customer's own price > the product's price.
"""
from __future__ import annotations

from decimal import Decimal

from aeradex import invoices
from aeradex.book import Book, BookError

from . import daten


def hourly_rate(book: Book, wer: str, kunde: str = "", projekt: str = "") -> Decimal:
    if projekt:
        p = daten.project(book, projekt)
        if p.get("satz") not in (None, ""):
            return daten.money(p["satz"])
    if kunde:
        c = invoices.customer(book, kunde)
        if c.get("stundensatz") not in (None, ""):
            return daten.money(c["stundensatz"])
    rate = daten.person(book, wer)["satz"]
    if rate is None:
        raise BookError(f"Kein Stundensatz für {wer} — unter Leistungen → Stammdaten den Verrechnungssatz erfassen "
                        "(oder beim Kunden/Projekt einen Satz hinterlegen)")
    return rate


def rate_source(book: Book, wer: str, kunde: str = "", projekt: str = "") -> str:
    """Where the rate comes from, for the UI: 'Projekt', 'Kunde', 'Person' or ''."""
    if projekt and daten.project(book, projekt).get("satz") not in (None, ""):
        return "Projekt"
    if kunde and invoices.customer(book, kunde).get("stundensatz") not in (None, ""):
        return "Kunde"
    return "Person" if daten.person(book, wer)["satz"] is not None else ""


def product_price(prod: dict, kunde: str = "") -> Decimal:
    own = (prod.get("kundenpreise") or {}).get(kunde)
    return daten.money(own if own not in (None, "") else prod.get("preis"))

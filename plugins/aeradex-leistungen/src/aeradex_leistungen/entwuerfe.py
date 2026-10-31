"""Invoice drafts (Rechnungsentwürfe): open work of one customer (and object) collected for an invoice that a person
checks and completes before it is issued — e.g. every Regie- and Material-Rapport of a building site.

A draft names entries, it does not copy them: the positions are computed from the entries each time, so a price set
on an entry (or a new product price) shows at once and the invoice is exactly what `abrechnung.bill` issues.
`leistungen/rechnungsentwuerfe/RE-0001.yaml`; issued or discarded drafts stay as a record.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from aeradex import invoices
from aeradex.book import Book, BookError
from aeradex.files import read_yaml, write_yaml

from . import abrechnung, daten

ZERO = Decimal("0")
STATUS = ("offen", "ausgestellt", "verworfen")


def folder(book: Book) -> Path:
    return book.root / "leistungen" / "rechnungsentwuerfe"


def drafts(book: Book) -> dict[str, dict]:
    out = {}
    for path in sorted(folder(book).glob("RE-*.yaml")) if folder(book).exists() else []:
        meta = read_yaml(path) or {}
        out[path.stem] = {"nummer": path.stem, "eintraege": [], "rapporte": [], **meta, "_pfad": path}
    return out


def draft(book: Book, nr: str) -> dict:
    found = drafts(book).get(str(nr).strip().upper())
    if found is None:
        raise BookError(f"Rechnungsentwurf {nr} nicht gefunden")
    return found


def _save(meta: dict) -> Path:
    path = meta["_pfad"]
    keys = ("kunde", "projekt", "status", "angelegt", "text", "rabatt", "eintraege", "rapporte", "rechnung",
            "ausgestellt")
    write_yaml(path, {k: meta[k] for k in keys if meta.get(k) not in (None, "", [])})
    return path


def in_drafts(book: Book) -> dict[str, str]:
    """Entry id → the open draft that holds it (an entry is in one open draft at most)."""
    return {i: nr for nr, m in drafts(book).items() if m.get("status", "offen") == "offen" for i in m["eintraege"]}


def open_for(book: Book, kunde: str, projekt: str = "") -> dict | None:
    for m in drafts(book).values():
        if m.get("status", "offen") == "offen" and m.get("kunde") == kunde and (m.get("projekt") or "") == projekt:
            return m
    return None


def _open_entries(book: Book, ids: list[str], kunde: str) -> list[dict]:
    items = abrechnung.select(book, status="", ids=ids)
    found = {e["id"] for e in items}
    if missing := [i for i in ids if i not in found]:
        raise BookError(f"Nicht gefunden: {', '.join(missing)}")
    if wrong := [f"{e['id']} ({e['status']})" for e in items if e["status"] != "offen"]:
        raise BookError(f"Nicht offen: {', '.join(wrong)}")
    if other := sorted({e["kunde"] for e in items} - {kunde}):
        raise BookError(f"Ein Entwurf gehört zu einem Kunden — {', '.join(other)} passt nicht")
    return items


def add(book: Book, kunde: str, ids: list[str], projekt: str = "", rapporte: list[str] | None = None,
        text: str = "") -> tuple[dict, list[Path]]:
    """Put entries into the open draft of the customer (and object), or start one. → the draft."""
    kunde = invoices.customer(book, kunde)["nummer"]
    projekt = str(projekt or "").strip().upper()
    if projekt:
        daten.project(book, projekt)
    ids = list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))
    _open_entries(book, ids, kunde)
    held = in_drafts(book)
    meta = open_for(book, kunde, projekt)
    if busy := [f"{i} ({held[i]})" for i in ids if i in held and (meta is None or held[i] != meta["nummer"])]:
        raise BookError(f"Schon in einem anderen Entwurf: {', '.join(busy)}")
    if meta is None:
        nums = [int(m.group(1)) for k in drafts(book) if (m := re.fullmatch(r"RE-(\d+)", k))]
        nr = f"RE-{max(nums, default=0) + 1:04d}"
        folder(book).mkdir(parents=True, exist_ok=True)
        meta = {"nummer": nr, "kunde": kunde, "projekt": projekt, "status": "offen",
                "angelegt": date.today().isoformat(), "eintraege": [], "rapporte": [], "_pfad": folder(book) / f"{nr}.yaml"}
    meta["eintraege"] = list(dict.fromkeys(list(meta["eintraege"]) + ids))
    meta["rapporte"] = list(dict.fromkeys(list(meta.get("rapporte") or []) + list(rapporte or [])))
    if text:
        meta["text"] = text
    return _public(meta), [_save(meta)] + fill_prices(book, ids)


def fill_prices(book: Book, ids: list[str], wer: str = "", produkt: str = "") -> list[Path]:
    """Entries without a price get the one that is known by now (a person's rate or a product price set later), as
    when they were recorded; optionally only one person's hours or one product. Prices already set stay."""
    from .saetze import hourly_rate, product_price
    touched: list[Path] = []
    for e in abrechnung.select(book, status="offen", ids=ids) if ids else []:
        if not missing_price(e) or (wer and e["wer"] != wer) or (produkt and e.get("produkt") != produkt):
            continue
        try:
            price = (hourly_rate(book, e["wer"], e["kunde"], e["projekt"], e.get("produkt") or "") if e["art"] == "Zeit"
                     else product_price(daten.product(book, e["produkt"]), e["kunde"]) if e.get("produkt") else None)
        except BookError:
            continue
        if price:
            _, paths = daten.update_entry(book, e["id"], preis=str(price))
            touched += paths
    return touched


def update(book: Book, nr: str, text: str | None = None, rabatt=None, entfernen: list[str] | None = None,
           hinzufuegen: list[str] | None = None) -> tuple[dict, list[Path]]:
    meta = _open(book, nr)
    if text is not None:
        meta["text"] = str(text).strip()
    if rabatt is not None:
        meta["rabatt"] = str(invoices._rabatt_percent(rabatt) or "") if str(rabatt).strip() else ""
    if entfernen:
        meta["eintraege"] = [i for i in meta["eintraege"] if i not in set(entfernen)]
    if hinzufuegen:
        _open_entries(book, hinzufuegen, meta["kunde"])
        held = in_drafts(book)
        if busy := [i for i in hinzufuegen if held.get(i) not in (None, meta["nummer"])]:
            raise BookError(f"Schon in einem anderen Entwurf: {', '.join(busy)}")
        meta["eintraege"] = list(dict.fromkeys(list(meta["eintraege"]) + list(hinzufuegen)))
    return _public(meta), [_save(meta)]


def _open(book: Book, nr: str) -> dict:
    meta = draft(book, nr)
    if meta.get("status", "offen") != "offen":
        raise BookError(f"{meta['nummer']} ist {meta['status']}")
    return meta


def missing_price(e: dict) -> bool:
    """A billable entry without a price (or 0: a product or Leistungsart whose price is not set yet)."""
    return e["abrechenbar"] and not e.get("preis")


def view(book: Book, nr: str) -> dict:
    """The draft with its entries, the positions and totals of the invoice it would issue, and what is missing."""
    meta = draft(book, nr)
    items = abrechnung.select(book, status="", ids=list(meta["eintraege"])) if meta["eintraege"] else []
    gone = [e for e in items if e["status"] != "offen"]
    live = [e for e in items if e["status"] == "offen"]
    out = {**_public(meta), "eintraege_offen": live, "nicht_mehr_offen": gone, "warnungen": [], "fehler": []}
    if meta.get("status", "offen") != "offen":
        return out
    names = {k: p["name"] for k, p in daten.people(book).items()}
    prods = daten.products(book)
    for e in live:
        e["wer_name"] = names.get(e["wer"], e["wer"])
        e["einheit"] = "h" if e["art"] == "Zeit" else prods.get(e["produkt"], {}).get("einheit", "")
        e["preis_fehlt"] = missing_price(e)
    if no_price := [e for e in live if e["preis_fehlt"]]:
        n = len(no_price)
        out["fehler"].append(f"{n} {'Eintrag' if n == 1 else 'Einträge'} ohne Preis — Preis bzw. Ansatz setzen")
    if not live:
        out["fehler"].append("Keine offenen Einträge im Entwurf")
    if gone:
        out["warnungen"].append(f"{len(gone)} Einträge sind inzwischen anderswo abgerechnet und fallen weg")
    if days := abrechnung.undescribed_days(book, live):
        out["warnungen"].append("Ohne Beschreibung: " + ", ".join(d["datum"].strftime("%d.%m.%Y") for d in days))
    from . import rapporte as rp
    for r in meta.get("rapporte") or []:
        info = rp.reports(book).get(r)
        if info and not info.get("unterschrieben_kunde", True):
            out["warnungen"].append(f"Rapport {r}: ohne Unterschrift des Auftraggebers")
    if live and not no_price:
        raw = abrechnung.positions(book, live)
        out["vorschau"] = abrechnung.totals(book, raw, meta["kunde"], meta.get("rabatt") or None)
        out["objekt"] = abrechnung.object_line(book, live)
    out["ausstellbar"] = not out["fehler"]
    return out


def issue(book: Book, nr: str, datum=None) -> tuple[dict, list[Path]]:
    """Issue the invoice of the draft (`abrechnung.bill`: number, booking, PDF, Leistungsrapport)."""
    meta = _open(book, nr)
    v = view(book, nr)
    if v["fehler"]:
        raise BookError(f"{meta['nummer']}: " + "; ".join(v["fehler"]))
    ids = [e["id"] for e in v["eintraege_offen"]]
    res, touched = abrechnung.bill(book, ids, datum=datum, text=meta.get("text") or "",
                                   rabatt=meta.get("rabatt") or None)
    meta.update({"status": "ausgestellt", "rechnung": res["rechnung"], "eintraege": ids,
                 "ausgestellt": date.today().isoformat()})
    return {**res, "entwurf": meta["nummer"]}, touched + [_save(meta)]


def discard(book: Book, nr: str) -> tuple[dict, list[Path]]:
    """The entries stay open (for another draft or the normal billing)."""
    meta = _open(book, nr)
    meta["status"] = "verworfen"
    return _public(meta), [_save(meta)]


def _public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def overview(book: Book) -> list[dict]:
    """Open drafts with customer, object, number of entries and the amount so far (entries with a price)."""
    names = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    projs = daten.projects(book)
    out = []
    for m in drafts(book).values():
        if m.get("status", "offen") != "offen":
            continue
        items = [e for e in abrechnung.select(book, status="offen", ids=list(m["eintraege"]))] if m["eintraege"] else []
        out.append({**_public(m), "kunde_name": names.get(m["kunde"], m["kunde"]),
                    "projekt_name": projs.get(m.get("projekt") or "", {}).get("name", ""),
                    "anzahl": len(items), "betrag": sum((e["betrag"] for e in items), ZERO),
                    "ohne_preis": sum(1 for e in items if missing_price(e))})
    return sorted(out, key=lambda m: m["nummer"])

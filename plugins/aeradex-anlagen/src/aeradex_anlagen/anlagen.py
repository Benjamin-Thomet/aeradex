"""Fixed assets: documents, depreciation, disposal, the asset schedule (Anlagenspiegel).

    anlagen/A0001-<name>.yaml   one asset: acquisition, method, rate, the depreciation booked per year

Depreciation for a year is booked at 31.12. (pro rata temporis in full months in the
year of acquisition) and recorded in the asset's file; the asset owns those journal rows
(Quelle abschreibung:A0001:2026), so `aeradex check` sees any change. A disposal books the
remaining book value away (with the sale proceeds, the gain or loss).
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from aeradex.book import Book, BookError, Row
from aeradex.files import parse_amount, parse_date, read_yaml, slug, write_yaml

ZERO = Decimal("0")
CENT = Decimal("0.01")

# Maximum rates of the ESTV (Merkblatt A 1995, Geschäftsbetriebe), degressive on the book value;
# linear on the acquisition value is half. Check against the current Merkblatt before use.
KATEGORIEN = {
    "mobiliar": ("Mobiliar, Büro-, Werkstatt- und Lagereinrichtungen", "1510", 25),
    "bueromaschinen": ("Büromaschinen", "1520", 40),
    "edv": ("EDV-Anlagen (Hardware und Software)", "1520", 40),
    "fahrzeuge": ("Fahrzeuge aller Art", "1530", 40),
    "maschinen": ("Maschinen und Apparate für die Produktion", "1500", 30),
    "werkzeuge": ("Werkzeuge, Werkgeschirr", "1540", 45),
}


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def folder(book: Book) -> Path:
    return book.root / "anlagen"


def assets(book: Book) -> dict[str, dict]:
    f = folder(book)
    out = {}
    for path in sorted(f.glob("A*.yaml")) if f.exists() else []:
        meta = read_yaml(path) or {}
        meta["_pfad"] = path
        out[str(meta["nummer"])] = meta
    return out


def asset(book: Book, nr: str) -> dict:
    found = assets(book).get(nr.upper())
    if found is None:
        raise BookError(f"Anlage {nr} nicht gefunden")
    return found


def booked(meta: dict) -> dict[int, Decimal]:
    return {int(y): money(v) for y, v in (meta.get("abschreibungen") or {}).items()}


def book_value(meta: dict, end_of: int | None = None) -> Decimal:
    """Acquisition value less the depreciation booked up to (and including) year `end_of`."""
    value = money(meta["wert"])
    value -= sum((v for y, v in booked(meta).items() if end_of is None or y <= end_of), ZERO)
    return value


def depreciation(meta: dict, year: int) -> Decimal:
    """What year `year` should take: pro rata temporis in full months in the year of acquisition,
    never below the residual value, nothing once disposed of."""
    acquired = parse_date(meta["datum"])
    if acquired.year > year or meta.get("abgang") and parse_date(meta["abgang"]["datum"]).year <= year:
        return ZERO
    months = 12 - acquired.month + 1 if acquired.year == year else 12
    start = book_value(meta, year - 1)
    rest = money(meta.get("restwert"))
    rate = Decimal(str(meta["satz"])) / 100
    base = money(meta["wert"]) if meta.get("methode") == "linear" else start
    amount = money(base * rate * months / 12)
    return max(ZERO, min(amount, start - rest))


def rows(book: Book) -> dict[str, list[Row]]:
    """Every journal row the assets own, by Quelle (for aeradex check)."""
    s = book.settings
    out: dict[str, list[Row]] = {}
    for nr, meta in assets(book).items():
        label = f"{nr} {meta['bezeichnung']}"
        for year, amount in sorted(booked(meta).items()):
            if amount:
                q = f"abschreibung:{nr}:{year}"
                out[q] = [Row(date(year, 12, 31), f"AB-{year}", f"Abschreibung {label} {year}",
                              str(meta["aufwandkonto"]), str(meta["konto"]), amount, q)]
        if meta.get("abgang"):
            a = meta["abgang"]
            d = parse_date(a["datum"])
            q = f"abschreibung:{nr}:abgang"
            value, proceeds = money(a["buchwert"]), money(a.get("erloes"))
            group = []
            if proceeds:
                group.append(Row(d, f"AB-{nr}", f"Verkauf {label}", str(a["zahlkonto"]), str(meta["konto"]), proceeds, q))
            diff = value - proceeds
            if diff > 0:
                group.append(Row(d, f"AB-{nr}", f"Abgang {label}: Verlust", str(a.get("verlustkonto") or "8500"),
                                 str(meta["konto"]), diff, q))
            elif diff < 0:
                group.append(Row(d, f"AB-{nr}", f"Abgang {label}: Gewinn", str(meta["konto"]),
                                 str(a.get("gewinnkonto") or "8510"), -diff, q))
            if group:
                out[q] = group
    return out


def add(book: Book, bezeichnung: str, datum, wert, kategorie: str = "", konto: str = "", methode: str = "degressiv",
        satz=None, restwert=0, aufwandkonto: str = "6800", beleg: str = "") -> tuple[dict, list[Path]]:
    if not bezeichnung.strip():
        raise BookError("Bezeichnung fehlt")
    d = parse_date(datum, "datum")
    amount = parse_amount(wert, "wert")
    if amount <= 0:
        raise BookError("Anschaffungswert muss positiv sein")
    if kategorie and kategorie not in KATEGORIEN:
        raise BookError(f"Kategorie: {', '.join(KATEGORIEN)}")
    if methode not in ("linear", "degressiv"):
        raise BookError("Methode: linear oder degressiv")
    default_konto, default_rate = (KATEGORIEN[kategorie][1], KATEGORIEN[kategorie][2]) if kategorie else ("", None)
    konto = str(konto or default_konto)
    if not konto:
        raise BookError("Konto (Sachanlage) fehlt — oder eine Kategorie wählen")
    acct = book.account(konto)
    if acct.klasse != "aktiv":
        raise BookError(f"Konto {konto} ist kein Aktivkonto")
    book.account(aufwandkonto)
    if satz in (None, ""):
        if default_rate is None:
            raise BookError("Satz fehlt — oder eine Kategorie wählen (ESTV-Höchstsatz)")
        satz = default_rate if methode == "degressiv" else Decimal(default_rate) / 2
    rate = Decimal(str(satz))
    if not 0 < rate <= 100:
        raise BookError("Satz in Prozent, z.B. 25")
    nums = [int(k[1:]) for k in assets(book) if k[1:].isdigit()]
    nr = f"A{max(nums, default=0) + 1:04d}"
    meta = {"nummer": nr, "bezeichnung": bezeichnung.strip(), "kategorie": kategorie, "konto": konto,
            "aufwandkonto": str(aufwandkonto), "datum": d.isoformat(), "wert": f"{amount:.2f}", "methode": methode,
            "satz": f"{rate.normalize():f}", "restwert": f"{money(restwert):.2f}", "beleg": beleg,
            "abschreibungen": {}}
    path = folder(book) / f"{nr}-{slug(bezeichnung)}.yaml"
    write_yaml(path, meta)
    return meta, [path]


def preview(book: Book, year: int) -> list[dict]:
    out = []
    for nr, meta in assets(book).items():
        if int(year) in booked(meta):
            continue
        amount = depreciation(meta, int(year))
        if amount:
            out.append({"nummer": nr, "bezeichnung": meta["bezeichnung"], "konto": meta["konto"], "betrag": amount,
                        "buchwert_vorher": book_value(meta, int(year) - 1)})
    return out


def depreciate(book: Book, year: int) -> tuple[list[dict], list[Path]]:
    """Book the year's depreciation of every asset that has not had it yet."""
    from aeradex.journal import ensure_open, post
    year = int(year)
    ensure_open(book, date(year, 12, 31))
    todo = preview(book, year)
    if not todo:
        raise BookError(f"Für {year} ist nichts abzuschreiben (oder schon gebucht)")
    touched: list[Path] = []
    for item in todo:
        meta = asset(book, item["nummer"])
        path = meta.pop("_pfad")
        meta.setdefault("abschreibungen", {})[str(year)] = f"{item['betrag']:.2f}"
        write_yaml(path, meta)
        touched.append(path)
    all_rows = rows(book)
    new = [r for item in todo for r in all_rows[f"abschreibung:{item['nummer']}:{year}"]]
    touched += post(book, new)
    return todo, touched


def dispose(book: Book, nr: str, datum, erloes=0, zahlkonto: str = "") -> tuple[dict, list[Path]]:
    """Sale or scrapping: the remaining book value leaves the books, the proceeds come in."""
    from aeradex.journal import ensure_open, post
    meta = asset(book, nr)
    if meta.get("abgang"):
        raise BookError(f"{nr} ist bereits ausgeschieden")
    d = parse_date(datum, "datum")
    ensure_open(book, d)
    if any(y >= d.year for y in booked(meta)):
        raise BookError(f"Für {nr} ist im Jahr {d.year} schon abgeschrieben — Abgang danach nicht möglich")
    path = meta.pop("_pfad")
    meta["abgang"] = {"datum": d.isoformat(), "buchwert": f"{book_value(meta):.2f}", "erloes": f"{money(erloes):.2f}",
                      "zahlkonto": str(zahlkonto or book.settings.konto("bank")), "verlustkonto": "8500",
                      "gewinnkonto": "8510"}
    write_yaml(path, meta)
    new = rows(book).get(f"abschreibung:{nr}:abgang", [])
    return meta, [path] + (post(book, new) if new else [])


def schedule(book: Book, year: int) -> dict:
    """Anlagenspiegel: per asset start, additions, depreciation, disposals, end of the year."""
    year = int(year)
    lines, totals = [], {k: ZERO for k in ("anfang", "zugang", "abschreibung", "abgang", "ende")}
    for nr, meta in assets(book).items():
        acquired = parse_date(meta["datum"])
        if acquired.year > year:
            continue
        gone = parse_date(meta["abgang"]["datum"]) if meta.get("abgang") else None
        if gone and gone.year < year:
            continue
        anfang = ZERO if acquired.year == year else book_value(meta, year - 1)
        zugang = money(meta["wert"]) if acquired.year == year else ZERO
        abschreibung = booked(meta).get(year, ZERO)
        abgang = money(meta["abgang"]["buchwert"]) if gone and gone.year == year else ZERO
        ende = anfang + zugang - abschreibung - abgang
        line = {"nummer": nr, "bezeichnung": meta["bezeichnung"], "konto": meta["konto"], "methode": meta["methode"],
                "satz": meta["satz"], "anfang": anfang, "zugang": zugang, "abschreibung": abschreibung,
                "abgang": abgang, "ende": ende, "offen": year not in booked(meta) and bool(depreciation(meta, year))}
        lines.append(line)
        for k in totals:
            totals[k] += line[k]
    return {"jahr": year, "anlagen": lines, "total": totals}

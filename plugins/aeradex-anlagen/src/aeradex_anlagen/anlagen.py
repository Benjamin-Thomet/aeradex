"""Fixed assets: documents, depreciation, disposal, the asset schedule (Anlagenspiegel).

    anlagen/A0001-<name>.yaml   one asset: acquisition, method, rate, the depreciation booked per year
    anlagen/ignoriert.yaml      purchases on an asset account a person marked as «keine Anlage»
    anlagen/einstellungen.yaml  rundung: book values at the year end rounded to 100 CHF (or 1, 10, 1000)

Depreciation for a year is booked at 31.12. (pro rata temporis in full months in the
year of acquisition) and recorded in the asset's file; the asset owns those journal rows
(Quelle abschreibung:A0001:2026), so `aeradex check` sees any change. A disposal books the
remaining book value away (with the sale proceeds, the gain or loss).

The purchase itself is an ordinary booking (supplier bill, receipt, bank) on the asset account;
`kandidaten` finds those that are not an asset yet, `add_from_booking` turns one into an asset
with the booked net amount. Assets owned before the book starts are taken over with their book
value (`add_existing`): already in the opening balance, or booked as a contribution (Einlage)
against equity, a row the asset owns (Quelle abschreibung:A0001:uebernahme).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
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
    "geruest": ("Gerüste, Leitern, Abdeckmaterial", "1540", 45),
}
METHODEN = ("linear", "degressiv")
ASSET_GROUPS = ("sachanlagen", "immaterielle")
_STORNO = re.compile(r"^Storno (\S+):")


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
    found = assets(book).get(str(nr).upper())
    if found is None:
        raise BookError(f"Anlage {nr} nicht gefunden")
    return found


def _save(meta: dict) -> Path:
    path = meta.pop("_pfad")
    write_yaml(path, meta)
    meta["_pfad"] = path
    return path


def booked(meta: dict) -> dict[int, Decimal]:
    return {int(y): money(v) for y, v in (meta.get("abschreibungen") or {}).items()}


def start_of(meta: dict) -> date:
    """From when the asset is in the books: the takeover date, else the acquisition."""
    u = meta.get("uebernahme")
    return parse_date(u["per"]) if u else parse_date(meta["datum"])


def start_value(meta: dict) -> Decimal:
    u = meta.get("uebernahme")
    return money(u["buchwert"]) if u else money(meta["wert"])


def book_value(meta: dict, end_of: int | None = None) -> Decimal:
    """Value when it entered the books less the depreciation booked up to (and including) year `end_of`."""
    value = start_value(meta)
    value -= sum((v for y, v in booked(meta).items() if end_of is None or y <= end_of), ZERO)
    return value


def default_rate(kategorie: str, methode: str) -> Decimal | None:
    if kategorie not in KATEGORIEN:
        return None
    rate = Decimal(KATEGORIEN[kategorie][2])
    return rate if methode == "degressiv" else rate / 2


def useful_life(kategorie: str) -> Decimal | None:
    """Years of straight-line depreciation at half the ESTV rate (Werkzeuge 45 % → 22.5 % → 4.4 years)."""
    rate = default_rate(kategorie, "linear")
    return (Decimal(100) / rate).quantize(Decimal("0.1"), ROUND_HALF_UP) if rate else None


ROUNDINGS = (ZERO, Decimal(1), Decimal(10), Decimal(100), Decimal(1000))


def settings(book: Book) -> dict:
    """anlagen/einstellungen.yaml: `rundung` — book values at the year end rounded to this step in CHF (0 = to the
    cent), so the asset accounts show round figures."""
    path = folder(book) / "einstellungen.yaml"
    data = (read_yaml(path) or {}) if path.exists() else {}
    return {"rundung": Decimal(str(data.get("rundung") or 0))}


def save_settings(book: Book, rundung) -> tuple[dict, list[Path]]:
    step = Decimal(str(rundung or 0))
    if step not in ROUNDINGS:
        raise BookError("Rundung: 0, 1, 10, 100 oder 1000")
    path = folder(book) / "einstellungen.yaml"
    data = (read_yaml(path) or {}) if path.exists() else {}
    data["rundung"] = int(step)
    write_yaml(path, data)
    return {"rundung": int(step)}, [path]


def round_to_step(start: Decimal, amount: Decimal, floor: Decimal, step: Decimal) -> Decimal:
    """Adjust a depreciation so the book value after it is a multiple of `step` (nearest; down when the
    nearest would leave the value unchanged, so a small asset still goes down), never below `floor`."""
    if not step or amount <= 0:
        return amount
    end = start - amount
    rounded = (end / step).quantize(Decimal(1), ROUND_HALF_UP) * step
    if rounded >= start:
        rounded = (end / step).to_integral_value(rounding=ROUND_FLOOR) * step
    rounded = max(rounded, floor)
    return max(ZERO, min(start - rounded, start - floor))


def depreciation(meta: dict, year: int, rundung: Decimal = ZERO) -> Decimal:
    """What year `year` should take: pro rata temporis in full months in the first year,
    never below the residual value, nothing once disposed of; with `rundung` the book value
    after it is a round figure."""
    begin = start_of(meta)
    if begin.year > year or meta.get("abgang") and parse_date(meta["abgang"]["datum"]).year <= year:
        return ZERO
    months = 12 - begin.month + 1 if begin.year == year else 12
    start = book_value(meta, year - 1)
    rest = money(meta.get("restwert"))
    rate = Decimal(str(meta["satz"])) / 100
    base = money(meta["wert"]) if meta.get("methode") == "linear" else start
    amount = money(base * rate * months / 12)
    amount = max(ZERO, min(amount, start - rest))
    return round_to_step(start, amount, rest, rundung)


def equity_account(book: Book) -> str:
    """Where an Einzelfirma books a contribution of the owner (Eigenkapital); a company has no default."""
    return book.settings.konto("gewinnvortrag") if book.settings.rechtsform_art == "einzelfirma" else ""


def rows(book: Book) -> dict[str, list[Row]]:
    """Every journal row the assets own, by Quelle (for aeradex check)."""
    out: dict[str, list[Row]] = {}
    for nr, meta in assets(book).items():
        label = f"{nr} {meta['bezeichnung']}"
        u = meta.get("uebernahme") or {}
        if u.get("einlage"):
            q = f"abschreibung:{nr}:uebernahme"
            out[q] = [Row(parse_date(u["per"]), f"AB-{nr}", f"Einlage {label} (Buchwert)", str(meta["konto"]),
                          str(u.get("gegenkonto") or equity_account(book)), money(u["buchwert"]), q)]
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


# ---------- recording ----------

def _rate_and_life(kategorie: str, methode: str, satz, nutzungsdauer) -> tuple[Decimal, str]:
    """The yearly rate in percent; a useful life (years) sets a straight-line rate of 100 / years."""
    if nutzungsdauer not in (None, "", 0, "0"):
        years = Decimal(str(nutzungsdauer).replace(",", "."))
        if years <= 0:
            raise BookError("Nutzungsdauer in Jahren, z.B. 5")
        if methode != "linear":
            raise BookError("Eine Nutzungsdauer gibt es nur bei linearer Abschreibung — sonst den Satz angeben")
        return (Decimal(100) / years).quantize(Decimal("0.01"), ROUND_HALF_UP), f"{years.normalize():f}"
    if satz in (None, ""):
        rate = default_rate(kategorie, methode)
        if rate is None:
            raise BookError("Satz oder Nutzungsdauer fehlt — oder eine Kategorie wählen (ESTV-Höchstsatz)")
    else:
        rate = Decimal(str(satz).replace(",", "."))
    if not 0 < rate <= 100:
        raise BookError("Satz in Prozent, z.B. 25")
    life = (Decimal(100) / rate).quantize(Decimal("0.1"), ROUND_HALF_UP) if methode == "linear" else None
    return rate, f"{life.normalize():f}" if life else ""


def _new(book: Book, bezeichnung: str, datum, wert, kategorie: str, konto: str, methode: str, satz, nutzungsdauer,
         restwert, aufwandkonto: str, beleg: str, notiz: str = "") -> dict:
    if not str(bezeichnung or "").strip():
        raise BookError("Bezeichnung fehlt")
    d = parse_date(datum, "datum")
    amount = parse_amount(wert, "wert")
    if amount <= 0:
        raise BookError("Anschaffungswert muss positiv sein")
    if kategorie and kategorie not in KATEGORIEN:
        raise BookError(f"Kategorie: {', '.join(KATEGORIEN)}")
    methode = methode or "linear"
    if methode not in METHODEN:
        raise BookError("Methode: linear oder degressiv")
    konto = str(konto or (KATEGORIEN[kategorie][1] if kategorie else ""))
    if not konto:
        raise BookError("Konto (Sachanlage) fehlt — oder eine Kategorie wählen")
    if book.account(konto).klasse != "aktiv":
        raise BookError(f"Konto {konto} ist kein Aktivkonto")
    book.account(aufwandkonto)
    rate, life = _rate_and_life(kategorie, methode, satz, nutzungsdauer)
    rest = money(restwert)
    if rest < 0 or rest >= amount:
        raise BookError("Restwert muss kleiner als der Anschaffungswert sein")
    nums = [int(k[1:]) for k in assets(book) if k[1:].isdigit()]
    nr = f"A{max(nums, default=0) + 1:04d}"
    meta = {"nummer": nr, "bezeichnung": str(bezeichnung).strip(), "kategorie": kategorie, "konto": konto,
            "aufwandkonto": str(aufwandkonto), "datum": d.isoformat(), "wert": f"{amount:.2f}", "methode": methode,
            "satz": f"{rate.normalize():f}", "restwert": f"{rest:.2f}", "beleg": beleg or "",
            "abschreibungen": {}}
    if life:
        meta["nutzungsdauer"] = life
    if notiz:
        meta["notiz"] = notiz.strip()
    meta["_pfad"] = folder(book) / f"{nr}-{slug(meta['bezeichnung'])}.yaml"
    return meta


def add(book: Book, bezeichnung: str, datum, wert, kategorie: str = "", konto: str = "", methode: str = "linear",
        satz=None, restwert=0, aufwandkonto: str = "6800", beleg: str = "", nutzungsdauer=None,
        notiz: str = "") -> tuple[dict, list[Path]]:
    """A new asset whose purchase is (or will be) booked on its account."""
    from aeradex.journal import ensure_open
    meta = _new(book, bezeichnung, datum, wert, kategorie, konto, methode, satz, nutzungsdauer, restwert,
                aufwandkonto, beleg, notiz)
    ensure_open(book, parse_date(meta["datum"]))
    return _public(meta), [_save(meta)]


def _public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if not k.startswith("_")}


# ---------- purchases on asset accounts ----------

def asset_accounts(book: Book) -> set[str]:
    return {a.nr for a in book.accounts.values() if a.klasse == "aktiv" and a.gruppe in ASSET_GROUPS}


def ignored(book: Book) -> set[tuple[str, str]]:
    data = read_yaml(folder(book) / "ignoriert.yaml") if (folder(book) / "ignoriert.yaml").exists() else None
    return {(str(i["beleg"]), str(i["konto"])) for i in (data or [])}


def purchases(book: Book) -> dict[tuple[str, str], dict]:
    """Net amounts booked onto asset accounts per (Beleg, account), stornos netted against their Beleg;
    the rows assets own themselves (depreciation, contribution, disposal) do not count."""
    accts = asset_accounts(book)
    out: dict[tuple[str, str], dict] = {}
    for r in book.rows:
        if r.quelle.startswith("abschreibung:"):
            continue
        for konto, sign in ((r.soll, 1), (r.haben, -1)):
            if konto not in accts:
                continue
            m = _STORNO.match(r.text) if sign < 0 else None
            key = (m.group(1) if m else r.beleg, konto)
            item = out.setdefault(key, {"beleg": key[0], "konto": konto, "datum": r.datum, "text": r.text,
                                        "quelle": r.quelle, "betrag": ZERO})
            item["betrag"] += sign * r.betrag
            if sign > 0 and r.datum < item["datum"]:
                item["datum"] = r.datum
    return out


def suggested_category(konto: str) -> str:
    for key, (_, acct, _) in KATEGORIEN.items():
        if acct == konto:
            return key
    return ""


def _name_from(text: str) -> str:
    """'Kreditor E-2026-0003 – Malerbedarf AG (R-77) · Spritzgerät' → 'Spritzgerät'; else the supplier."""
    parts = [p.strip() for p in text.split(" · ") if p.strip() and not p.strip().startswith("Leistung ")]
    if len(parts) > 1:
        return parts[-1]
    text = parts[0] if parts else text
    m = re.match(r"Kreditor \S+ – ([^(]+)", text)
    return (m.group(1) if m else text).strip()


def kandidaten(book: Book, beleg: str = "") -> list[dict]:
    """Purchases on an asset account that are not (fully) an asset yet: what is left of the booked amount
    after the assets that name this Beleg, unless a person marked it «keine Anlage»."""
    taken: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for meta in assets(book).values():
        if meta.get("beleg") and not meta.get("uebernahme"):
            taken[(str(meta["beleg"]), str(meta["konto"]))] += money(meta["wert"])
    skip = ignored(book)
    out = []
    for key, item in sorted(purchases(book).items(), key=lambda kv: (kv[1]["datum"], kv[0])):
        if beleg and key[0] != beleg or key in skip:
            continue
        rest = item["betrag"] - taken[key]
        if rest <= 0:
            continue
        kat = suggested_category(item["konto"])
        acct = book.accounts.get(item["konto"])
        out.append({**item, "rest": rest, "erfasst": taken[key], "kategorie": kat,
                    "konto_name": acct.name if acct else "", "bezeichnung": _name_from(item["text"]),
                    "nutzungsdauer": useful_life(kat)})
    return out


def add_from_booking(book: Book, beleg: str, konto: str, bezeichnung: str = "", kategorie: str = "", wert=None,
                     nutzungsdauer=None, methode: str = "linear", satz=None, restwert=0,
                     notiz: str = "") -> tuple[dict, list[Path]]:
    """An asset from a booked purchase: date and account from the booking, the value is what is left of the
    booked net amount (or a part of it, for a bill with several items)."""
    konto = str(konto)
    found = [k for k in kandidaten(book, str(beleg)) if k["konto"] == konto]
    if not found:
        raise BookError(f"Beleg {beleg} hat auf Konto {konto} keine Anschaffung, die noch keine Anlage ist")
    item = found[0]
    value = item["rest"] if wert in (None, "") else parse_amount(wert, "wert")
    if value > item["rest"]:
        raise BookError(f"Wert {value:.2f} ist höher als der noch nicht erfasste Betrag {item['rest']:.2f} "
                        f"von Beleg {beleg}")
    kategorie = kategorie or item["kategorie"]
    return add(book, bezeichnung or item["bezeichnung"], item["datum"], value, kategorie, konto, methode, satz,
               restwert, beleg=str(beleg), nutzungsdauer=nutzungsdauer, notiz=notiz)


def ignore(book: Book, beleg: str, konto: str) -> tuple[dict, list[Path]]:
    """Mark a purchase on an asset account as «keine Anlage» (e.g. a transfer between asset accounts)."""
    path = folder(book) / "ignoriert.yaml"
    items = (read_yaml(path) or []) if path.exists() else []
    if not any(str(i["beleg"]) == str(beleg) and str(i["konto"]) == str(konto) for i in items):
        items.append({"beleg": str(beleg), "konto": str(konto)})
    write_yaml(path, items)
    return {"beleg": beleg, "konto": konto}, [path]


# ---------- takeover of assets owned before ----------

def add_existing(book: Book, bezeichnung: str, datum, wert, buchwert, per=None, kategorie: str = "", konto: str = "",
                 methode: str = "linear", satz=None, nutzungsdauer=None, restwert=0, einlage: bool = False,
                 gegenkonto: str = "", notiz: str = "") -> tuple[dict, list[Path]]:
    """An asset owned before: bought on `datum` for `wert`, in the books from `per` (default: the first day
    of the book) at `buchwert`. With `einlage` the value is booked as a contribution against equity;
    otherwise it is already part of the opening balance."""
    from aeradex.journal import ensure_open, post
    d_per = parse_date(per, "per") if per else date(book.settings.erstes_jahr, 1, 1)
    meta = _new(book, bezeichnung, datum, wert, kategorie, konto, methode, satz, nutzungsdauer, restwert, "6800",
                "", notiz)
    if parse_date(meta["datum"]) > d_per:
        raise BookError("Das Kaufdatum liegt nach der Übernahme — dann ist es eine normale Anschaffung")
    value = parse_amount(buchwert, "buchwert")
    if not 0 < value <= money(meta["wert"]):
        raise BookError("Buchwert muss positiv und höchstens der Kaufwert sein")
    if value < money(meta["restwert"]):
        raise BookError("Buchwert ist schon tiefer als der Restwert")
    if not einlage and (d_per.month, d_per.day) != (1, 1):
        raise BookError("Ohne Einlage steht die Anlage in der Eröffnungsbilanz — Übernahme per 1. Januar")
    meta["uebernahme"] = {"per": d_per.isoformat(), "buchwert": f"{value:.2f}", "einlage": bool(einlage)}
    touched = []
    if einlage:
        ensure_open(book, d_per)
        meta["uebernahme"]["gegenkonto"] = str(gegenkonto or equity_account(book))
        if not meta["uebernahme"]["gegenkonto"]:
            raise BookError("Gegenkonto der Einlage angeben (z.B. Darlehen des Gesellschafters)")
        book.account(meta["uebernahme"]["gegenkonto"])
    touched.append(_save(meta))
    if einlage:
        touched += post(book, rows(book)[f"abschreibung:{meta['nummer']}:uebernahme"])
    return _public(meta), touched


# ---------- changes ----------

EDITABLE = ("bezeichnung", "kategorie", "methode", "satz", "nutzungsdauer", "restwert", "notiz")
EDITABLE_UNBOOKED = ("datum", "wert", "konto")


def update(book: Book, nr: str, **fields) -> tuple[dict, list[Path]]:
    """Change an asset. Method and rate apply to the years not yet booked; date, value and account only
    while nothing is booked for it."""
    meta = asset(book, nr)
    fields = {k: v for k, v in fields.items() if v is not None}
    unknown = set(fields) - set(EDITABLE) - set(EDITABLE_UNBOOKED)
    if unknown:
        raise BookError(f"Nicht änderbar: {', '.join(sorted(unknown))}")
    if meta.get("abgang"):
        raise BookError(f"{nr} ist ausgeschieden — nicht mehr änderbar")
    locked = [k for k in EDITABLE_UNBOOKED if k in fields and str(fields[k]) != str(meta.get(k))]
    if locked and (booked(meta) or meta.get("uebernahme", {}).get("einlage") or meta.get("beleg")):
        raise BookError(f"{', '.join(locked)}: nur änderbar, solange die Anlage weder abgeschrieben noch mit "
                        "einer Buchung verknüpft ist")
    if "bezeichnung" in fields and not str(fields["bezeichnung"]).strip():
        raise BookError("Bezeichnung fehlt")
    if "kategorie" in fields and fields["kategorie"] and fields["kategorie"] not in KATEGORIEN:
        raise BookError(f"Kategorie: {', '.join(KATEGORIEN)}")
    methode = fields.get("methode") or meta.get("methode") or "linear"
    if methode not in METHODEN:
        raise BookError("Methode: linear oder degressiv")
    if any(fields.get(k) not in (None, "") for k in ("methode", "satz", "nutzungsdauer")):
        life = fields.get("nutzungsdauer") if methode == "linear" else None
        satz = fields.get("satz")
        if life in (None, "") and satz in (None, "") and methode == meta.get("methode"):
            satz = meta.get("satz")                       # only the life was cleared or nothing new: keep the rate
        rate, life_text = _rate_and_life(fields.get("kategorie", meta.get("kategorie", "")), methode,
                                         None if life not in (None, "") else satz, life)
        meta["methode"], meta["satz"] = methode, f"{rate.normalize():f}"
        meta.pop("nutzungsdauer", None)
        if life_text:
            meta["nutzungsdauer"] = life_text
    for k in ("bezeichnung", "kategorie", "notiz"):
        if k in fields:
            meta[k] = str(fields[k]).strip()
    if "restwert" in fields:
        rest = money(parse_amount(fields["restwert"] or 0, "restwert"))
        if rest < 0 or rest > book_value(meta):
            raise BookError("Restwert muss zwischen 0 und dem Buchwert liegen")
        meta["restwert"] = f"{rest:.2f}"
    if "datum" in fields:
        meta["datum"] = parse_date(fields["datum"], "datum").isoformat()
    if "wert" in fields:
        value = parse_amount(fields["wert"], "wert")
        if value <= 0:
            raise BookError("Anschaffungswert muss positiv sein")
        meta["wert"] = f"{value:.2f}"
    if "konto" in fields:
        if book.account(str(fields["konto"])).klasse != "aktiv":
            raise BookError(f"Konto {fields['konto']} ist kein Aktivkonto")
        meta["konto"] = str(fields["konto"])
    if not meta.get("notiz"):
        meta.pop("notiz", None)
    return _public(meta), [_save(meta)]


def remove(book: Book, nr: str) -> tuple[dict, list[Path]]:
    """Delete an asset recorded by mistake: only while no depreciation or disposal is booked. A contribution
    it booked goes with it; the purchase booking stays and is a candidate again."""
    meta = asset(book, nr)
    if booked(meta) or meta.get("abgang"):
        raise BookError(f"{nr} ist schon abgeschrieben — statt löschen einen Abgang buchen")
    touched = []
    if (meta.get("uebernahme") or {}).get("einlage"):
        from aeradex.journal import ensure_open
        ensure_open(book, start_of(meta))
        q = f"abschreibung:{meta['nummer']}:uebernahme"
        touched += book.remove_rows(lambda r: r.quelle == q)
    path = meta["_pfad"]
    path.unlink()
    return _public(meta), touched + [path]


# ---------- year end ----------

def preview(book: Book, year: int) -> list[dict]:
    out = []
    step = settings(book)["rundung"]
    for nr, meta in assets(book).items():
        if int(year) in booked(meta):
            continue
        amount = depreciation(meta, int(year), step)
        if amount:
            vorher = book_value(meta, int(year) - 1) if start_of(meta).year < int(year) else start_value(meta)
            out.append({"nummer": nr, "bezeichnung": meta["bezeichnung"], "konto": meta["konto"], "betrag": amount,
                        "buchwert_vorher": vorher, "maximal": vorher - money(meta.get("restwert"))})
    return out


def depreciate(book: Book, year: int, betraege: dict | None = None) -> tuple[list[dict], list[Path]]:
    """Book the year's depreciation of every asset that has not had it yet. `betraege` may set another amount
    per asset (a lower or an additional depreciation), between 0 and the book value less the residual value."""
    from aeradex.journal import ensure_open, post
    year = int(year)
    ensure_open(book, date(year, 12, 31))
    todo = preview(book, year)
    if not todo:
        raise BookError(f"Für {year} ist nichts abzuschreiben (oder schon gebucht)")
    wanted = {str(k).upper(): v for k, v in (betraege or {}).items() if v not in (None, "")}
    unknown = set(wanted) - {t["nummer"] for t in todo}
    if unknown:
        raise BookError(f"{', '.join(sorted(unknown))}: für {year} nicht abzuschreiben")
    for item in todo:
        if item["nummer"] in wanted:
            amount = money(parse_amount(wanted[item["nummer"]], f"Betrag {item['nummer']}"))
            if not ZERO <= amount <= item["maximal"]:
                raise BookError(f"{item['nummer']}: Abschreibung zwischen 0 und {item['maximal']:.2f} "
                                "(Buchwert abzüglich Restwert)")
            item["berechnet"], item["betrag"] = item["betrag"], amount
    touched: list[Path] = []
    for item in todo:
        meta = asset(book, item["nummer"])
        meta.setdefault("abschreibungen", {})[str(year)] = f"{item['betrag']:.2f}"
        touched.append(_save(meta))
    all_rows = rows(book)
    new = [r for item in todo for r in all_rows.get(f"abschreibung:{item['nummer']}:{year}", [])]
    if new:
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
    if d < start_of(meta):
        raise BookError(f"{nr}: Abgang vor der Anschaffung")
    if any(y >= d.year for y in booked(meta)):
        raise BookError(f"Für {nr} ist im Jahr {d.year} schon abgeschrieben — Abgang danach nicht möglich")
    meta["abgang"] = {"datum": d.isoformat(), "buchwert": f"{book_value(meta):.2f}", "erloes": f"{money(erloes):.2f}",
                      "zahlkonto": str(zahlkonto or book.settings.konto("bank")), "verlustkonto": "8500",
                      "gewinnkonto": "8510"}
    path = _save(meta)
    new = rows(book).get(f"abschreibung:{nr}:abgang", [])
    return _public(meta), [path] + (post(book, new) if new else [])


def schedule(book: Book, year: int) -> dict:
    """Anlagenspiegel: per asset start, additions, depreciation, disposals, end of the year."""
    year = int(year)
    step = settings(book)["rundung"]
    lines, totals = [], {k: ZERO for k in ("anfang", "zugang", "abschreibung", "abgang", "ende")}
    for nr, meta in assets(book).items():
        begin = start_of(meta)
        if begin.year > year:
            continue
        gone = parse_date(meta["abgang"]["datum"]) if meta.get("abgang") else None
        if gone and gone.year < year:
            continue
        u = meta.get("uebernahme") or {}
        entered_now = begin.year == year
        # taken over into the opening balance: there at the start; bought or contributed: an addition
        opening_takeover = entered_now and u and not u.get("einlage") and begin == date(year, 1, 1)
        anfang = (start_value(meta) if opening_takeover else ZERO) if entered_now else book_value(meta, year - 1)
        zugang = start_value(meta) if entered_now and not opening_takeover else ZERO
        abschreibung = booked(meta).get(year, ZERO)
        abgang = money(meta["abgang"]["buchwert"]) if gone and gone.year == year else ZERO
        ende = anfang + zugang - abschreibung - abgang
        line = {"nummer": nr, "bezeichnung": meta["bezeichnung"], "konto": meta["konto"], "methode": meta["methode"],
                "satz": meta["satz"], "nutzungsdauer": meta.get("nutzungsdauer", ""), "datum": meta["datum"],
                "wert": money(meta["wert"]), "beleg": meta.get("beleg", ""), "kategorie": meta.get("kategorie", ""),
                "restwert": money(meta.get("restwert")), "notiz": meta.get("notiz", ""),
                "uebernahme": bool(u), "einlage": bool(u.get("einlage")), "abgegangen": bool(gone),
                "anfang": anfang, "zugang": zugang, "abschreibung": abschreibung, "abgang": abgang, "ende": ende,
                "offen": year not in booked(meta) and bool(depreciation(meta, year, step)),
                "aenderbar": not booked(meta) and not gone,
                "fest": bool(booked(meta) or u.get("einlage") or meta.get("beleg"))}
        lines.append(line)
        for k in totals:
            totals[k] += line[k]
    return {"jahr": year, "anlagen": lines, "total": totals}


def report(book: Book, year: int) -> dict:
    """The Anlagenspiegel in the common report shape (Berichte, PDF, CSV)."""
    from aeradex.reports import Column, _report
    data = schedule(book, int(year))
    start, end = date(int(year), 1, 1), date(int(year), 12, 31)
    keys = [("anfang", "Anfang"), ("zugang", "Zugang"), ("abschreibung", "Abschreibung"), ("abgang", "Abgang"),
            ("ende", "Ende")]
    cols = [Column(k, label, "ist", start, end) for k, label in keys]
    rows_ = []
    for line in data["anlagen"]:
        how = f"linear {line['nutzungsdauer']} J" if line["methode"] == "linear" and line["nutzungsdauer"] \
            else f"{line['methode']} {line['satz']} %"
        rows_.append({"label": f"{line['nummer']} {line['bezeichnung']} ({line['konto']}, {how})", "stil": "line",
                      "werte": {k: line[k] for k, _ in keys}, "konten": []})
    rows_.append({"label": "Total", "stil": "total", "werte": {k: data["total"][k] for k, _ in keys}, "konten": []})
    hints = [f"{l['nummer']}: Abschreibung {year} noch nicht gebucht" for l in data["anlagen"] if l["offen"]]
    return _report("anlagenspiegel", "Anlagenspiegel", f"Geschäftsjahr {year}", cols, rows_, hinweise=hints)

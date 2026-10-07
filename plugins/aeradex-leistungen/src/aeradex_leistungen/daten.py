"""The plugin's documents in the book, all plain text:

    leistungen/einstellungen.yaml        rates per person, people without payroll, categories, holidays, accounts
    leistungen/produkte.yaml             products with price, unit, account, MWST code, customer prices
    leistungen/erfassung/<JJJJ-MM>.md    time and products, one Markdown table per month
    projekte/P0001-<name>.md             projects of a customer (budget, flat rate or time and material)
    offerten/<JJJJ>/O-….md (+ .pdf)      quotes (see offerten.py)

Nothing here owns journal rows: billing goes through the core's invoices
(`Quelle rechnung:…`). Whether an entry is billed is derived from the invoice it
names, so a voided invoice makes its entries open again.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from aeradex import invoices, payroll
from aeradex.book import Book, BookError
from aeradex.files import (MdTable, parse_amount, parse_date, read_frontmatter, read_table, read_yaml, slug,
                          write_frontmatter, write_table, write_yaml)

ZERO = Decimal("0")
CENT = Decimal("0.01")

COLUMNS = ["ID", "Datum", "Art", "Wer", "Kunde", "Projekt", "Produkt", "Menge", "Preis", "Text",
           "Abrechenbar", "Kategorie", "Rechnung"]
ARTEN = ("Zeit", "Produkt")
KATEGORIEN = ["Intern", "Administration", "Weiterbildung", "Garantie", "Ferien", "Krank", "Feiertag"]
ABRECHNUNG = ("aufwand", "pauschal")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def num(value) -> str:
    """Quantity for a file: 1.5, 2, 0.25 (no trailing zeros)."""
    d = Decimal(str(value)).normalize()
    return f"{d:f}"


# ---------- settings ----------

def settings_path(book: Book) -> Path:
    return book.root / "leistungen" / "einstellungen.yaml"


def settings(book: Book) -> dict:
    path = settings_path(book)
    raw = (read_yaml(path) or {}) if path.exists() else {}
    ertrag = book.settings.konto("ertrag")
    return {
        "personen": {str(k): dict(v or {}) for k, v in (raw.get("personen") or {}).items()},
        "kategorien": list(raw.get("kategorien") or KATEGORIEN),
        "feiertage": [str(d) for d in raw.get("feiertage") or []],
        "konto_stunden": str(raw.get("konto_stunden") or ertrag),
        "konto_produkte": str(raw.get("konto_produkte") or ("3200" if "3200" in book.accounts else ertrag)),
        "gueltig_tage": int(raw.get("gueltig_tage") or 30),
        "kontrolle_ab": str(raw.get("kontrolle_ab") or ""),
    }


def save_settings(book: Book, cfg: dict) -> Path:
    path = settings_path(book)
    out = {k: v for k, v in cfg.items()}
    out["personen"] = {k: cfg["personen"][k] for k in sorted(cfg["personen"])}
    write_yaml(path, out)
    return path


def people(book: Book) -> dict[str, dict]:
    """Everyone who records time: employees from personal/ plus people without payroll (owner …),
    with their billing rate (`satz`), internal cost rate (`kostensatz`) and weekly target hours."""
    cfg = settings(book)
    out = {}
    for nr, emp in payroll.employees(book).items():
        own = cfg["personen"].get(nr, {})
        out[nr] = {"nummer": nr, "name": payroll.display_name(emp), "lohn": True,
                   "aktiv": bool(emp.get("aktiv", True)), "stundenlohn": payroll.is_hourly(emp),
                   "satz": money(own["satz"]) if own.get("satz") not in (None, "") else None,
                   "kostensatz": money(own["kostensatz"]) if own.get("kostensatz") not in (None, "") else None,
                   "woche": (Decimal(str(emp.get("vollzeit_stunden_woche") or 0))
                             * Decimal(str(emp.get("pensum") or 100)) / 100),
                   "eintritt": emp.get("eintritt"), "austritt": emp.get("austritt"),
                   "vortrag": own.get("vortrag") or {}}
    for nr, own in cfg["personen"].items():
        if nr in out:
            continue
        out[nr] = {"nummer": nr, "name": own.get("name") or nr, "lohn": False, "aktiv": own.get("aktiv", True),
                   "stundenlohn": False,
                   "satz": money(own["satz"]) if own.get("satz") not in (None, "") else None,
                   "kostensatz": money(own["kostensatz"]) if own.get("kostensatz") not in (None, "") else None,
                   "woche": Decimal(str(own["soll_woche"])) if own.get("soll_woche") not in (None, "") else None,
                   "eintritt": own.get("eintritt"), "austritt": own.get("austritt"),
                   "vortrag": own.get("vortrag") or {}}
    return out


def person(book: Book, nr: str) -> dict:
    found = people(book).get(str(nr).strip().upper())
    if found is None:
        raise BookError(f"Person {nr} unbekannt — Mitarbeitende unter Lohn erfassen oder hier ohne Lohn anlegen")
    return found


def set_person(book: Book, nummer: str = "", satz=None, kostensatz=None, name: str = "", soll_woche=None,
               vortrag_jahr: int | None = None, vortrag=None) -> tuple[dict, list[Path]]:
    """Set the rates of an employee (M0001) — or create/update a person without payroll (name, no number)."""
    cfg = settings(book)
    employees = payroll.employees(book)
    nr = str(nummer or "").strip().upper()
    if not nr:
        if not name.strip():
            raise BookError("Nummer (Mitarbeitende) oder Name (Person ohne Lohn) angeben")
        nums = [int(k[1:]) for k in cfg["personen"] if re.fullmatch(r"X\d+", k)]
        nr = f"X{max(nums, default=0) + 1:02d}"
    elif nr not in employees and nr not in cfg["personen"]:
        raise BookError(f"{nr} ist weder Mitarbeitende/r noch eine erfasste Person")
    entry = cfg["personen"].setdefault(nr, {})
    if nr not in employees and name.strip():
        entry["name"] = name.strip()
    for key, value in (("satz", satz), ("kostensatz", kostensatz)):
        if value not in (None, ""):
            amount = parse_amount(value, key)
            if amount < 0:
                raise BookError(f"{key} darf nicht negativ sein")
            entry[key] = money(amount)
    if soll_woche not in (None, "") and nr not in employees:
        entry["soll_woche"] = Decimal(str(soll_woche))
    if vortrag_jahr and vortrag not in (None, ""):
        entry.setdefault("vortrag", {})[int(vortrag_jahr)] = Decimal(str(vortrag))
    return {"nummer": nr, **entry}, [save_settings(book, cfg)]


def set_holidays(book: Book, feiertage: list | None = None, kontrolle_ab=None) -> tuple[dict, list[Path]]:
    """Holidays (reduce the Soll) and the date the hours control starts (earlier months are not counted)."""
    cfg = settings(book)
    if feiertage is not None:
        cfg["feiertage"] = sorted({parse_date(d, "feiertag").isoformat() for d in feiertage if str(d).strip()})
    if kontrolle_ab is not None:
        cfg["kontrolle_ab"] = parse_date(kontrolle_ab, "kontrolle_ab").isoformat() if str(kontrolle_ab).strip() else ""
    return {"feiertage": cfg["feiertage"], "kontrolle_ab": cfg["kontrolle_ab"]}, [save_settings(book, cfg)]


# ---------- products ----------

def products_path(book: Book) -> Path:
    return book.root / "leistungen" / "produkte.yaml"


def products(book: Book) -> dict[str, dict]:
    path = products_path(book)
    raw = (read_yaml(path) or {}) if path.exists() else {}
    return {str(k): {"nummer": str(k), **(v or {})} for k, v in raw.items()}


def product(book: Book, nr: str) -> dict:
    found = products(book).get(str(nr).strip().upper())
    if found is None:
        raise BookError(f"Produkt {nr} nicht gefunden")
    return found


def _save_products(book: Book, items: dict) -> Path:
    path = products_path(book)
    write_yaml(path, {k: {f: v for f, v in items[k].items() if f != "nummer"} for k in sorted(items)})
    return path


def _check_mwst(book: Book, code: str) -> str:
    from aeradex import mwst as vat
    code = str(code or "").strip().upper()
    if not code:
        return ""
    if vat.config(book)["methode"] == "keine":
        raise BookError("Dieses Buch ist nicht MWST-pflichtig — Produkte ohne MWST-Code erfassen")
    c = vat.code(code)
    if c.kind not in ("umsatz", "befreit", "ausgenommen"):
        raise BookError(f"{code}: auf Rechnungen nur Umsatz-Codes (U81, U26, U38, U0, UA)")
    return c.code


def add_product(book: Book, text: str, preis, einheit: str = "Stk", konto: str = "", mwst: str = "",
                nummer: str = "") -> tuple[dict, list[Path]]:
    items = products(book)
    if not text.strip():
        raise BookError("Produkt: Text fehlt")
    nr = str(nummer or "").strip().upper()
    if nr and nr in items:
        raise BookError(f"Produkt {nr} gibt es schon")
    if not nr:
        nums = [int(k[1:]) for k in items if re.fullmatch(r"P\d+", k)]
        nr = f"P{max(nums, default=0) + 1:03d}"
    konto = str(konto or settings(book)["konto_produkte"])
    if book.account(konto).klasse != "ertrag":
        raise BookError(f"Konto {konto} ist kein Ertragskonto")
    items[nr] = {"nummer": nr, "text": text.strip(), "einheit": einheit.strip() or "Stk",
                 "preis": money(parse_amount(preis, "preis")), "konto": konto, "mwst": _check_mwst(book, mwst),
                 "kundenpreise": {}, "aktiv": True}
    return items[nr], [_save_products(book, items)]


def update_product(book: Book, nummer: str, text: str | None = None, preis=None, einheit: str | None = None,
                   konto: str | None = None, mwst: str | None = None, aktiv: bool | None = None,
                   kunde: str = "", kundenpreis=None) -> tuple[dict, list[Path]]:
    """Change a product. A changed price applies to new entries only (entries keep their price).
    `kunde` + `kundenpreis` sets (or with an empty price removes) a customer's own price."""
    items = products(book)
    p = items.get(str(nummer).strip().upper())
    if p is None:
        raise BookError(f"Produkt {nummer} nicht gefunden")
    if text is not None and text.strip():
        p["text"] = text.strip()
    if preis not in (None, ""):
        p["preis"] = money(parse_amount(preis, "preis"))
    if einheit is not None and einheit.strip():
        p["einheit"] = einheit.strip()
    if konto:
        if book.account(str(konto)).klasse != "ertrag":
            raise BookError(f"Konto {konto} ist kein Ertragskonto")
        p["konto"] = str(konto)
    if mwst is not None:
        p["mwst"] = _check_mwst(book, mwst)
    if aktiv is not None:
        p["aktiv"] = bool(aktiv)
    if kunde:
        invoices.customer(book, kunde)
        prices = dict(p.get("kundenpreise") or {})
        if kundenpreis in (None, ""):
            prices.pop(kunde, None)
        else:
            prices[kunde] = money(parse_amount(kundenpreis, "kundenpreis"))
        p["kundenpreise"] = {k: prices[k] for k in sorted(prices)}
    return p, [_save_products(book, items)]


# ---------- projects ----------

def projects(book: Book) -> dict[str, dict]:
    folder = book.root / "projekte"
    out = {}
    for path in sorted(folder.glob("P*.md")) if folder.exists() else []:
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        meta["_notizen"] = body.strip()
        out[str(meta.get("nummer"))] = meta
    return out


def project(book: Book, nr: str) -> dict:
    found = projects(book).get(str(nr).strip().upper())
    if found is None:
        raise BookError(f"Projekt {nr} nicht gefunden")
    return found


def add_project(book: Book, kunde: str, name: str, abrechnung: str = "aufwand", budget_stunden=None,
                budget_chf=None, satz=None, offerte: str = "", notizen: str = "") -> tuple[dict, list[Path]]:
    cust = invoices.customer(book, kunde)
    if not name.strip():
        raise BookError("Projekt: Name fehlt")
    if abrechnung not in ABRECHNUNG:
        raise BookError("Abrechnung: aufwand oder pauschal")
    nums = [int(k[1:]) for k in projects(book) if re.fullmatch(r"P\d+", k)]
    nr = f"P{max(nums, default=0) + 1:04d}"
    meta = {"nummer": nr, "name": name.strip(), "kunde": cust["nummer"], "abrechnung": abrechnung,
            "budget_stunden": Decimal(str(budget_stunden)) if budget_stunden not in (None, "") else None,
            "budget_chf": money(parse_amount(budget_chf, "budget_chf")) if budget_chf not in (None, "") else None,
            "satz": money(parse_amount(satz, "satz")) if satz not in (None, "") else None,
            "offerte": offerte, "status": "offen", "erstellt": date.today().isoformat()}
    path = book.root / "projekte" / f"{nr}-{slug(name)}.md"
    write_frontmatter(path, meta, notizen)
    return meta, [path]


def update_project(book: Book, nummer: str, status: str | None = None, **fields) -> tuple[dict, list[Path]]:
    meta = project(book, nummer)
    path, notes = meta.pop("_pfad"), meta.pop("_notizen", "")
    if status is not None:
        if status not in ("offen", "abgeschlossen"):
            raise BookError("Status: offen oder abgeschlossen")
        meta["status"] = status
    for key in ("budget_stunden", "budget_chf", "satz"):
        if fields.get(key) not in (None, ""):
            meta[key] = Decimal(str(fields[key])) if key == "budget_stunden" else money(parse_amount(fields[key], key))
    if fields.get("name"):
        meta["name"] = str(fields["name"]).strip()
    if fields.get("abrechnung"):
        if fields["abrechnung"] not in ABRECHNUNG:
            raise BookError("Abrechnung: aufwand oder pauschal")
        meta["abrechnung"] = fields["abrechnung"]
    write_frontmatter(path, meta, notes)
    return meta, [path]


# ---------- entries ----------

def entry_files(book: Book) -> list[Path]:
    folder = book.root / "leistungen" / "erfassung"
    return sorted(folder.glob("*.md")) if folder.exists() else []


def _parse(cells: dict, path: Path) -> dict:
    where = f"{path.name} {cells.get('ID')}"
    return {"id": cells["ID"], "datum": parse_date(cells["Datum"], where), "art": cells["Art"],
            "wer": cells.get("Wer", ""), "kunde": cells.get("Kunde", ""), "projekt": cells.get("Projekt", ""),
            "produkt": cells.get("Produkt", ""), "menge": Decimal(cells.get("Menge") or "0"),
            "preis": parse_amount(cells["Preis"], where) if cells.get("Preis") else None,
            "text": cells.get("Text", ""), "abrechenbar": cells.get("Abrechenbar", "ja") == "ja",
            "kategorie": cells.get("Kategorie", ""), "rechnung": cells.get("Rechnung", ""), "_datei": path}


def _cells(e: dict) -> dict:
    return {"ID": e["id"], "Datum": e["datum"].isoformat(), "Art": e["art"], "Wer": e.get("wer", ""),
            "Kunde": e.get("kunde", ""), "Projekt": e.get("projekt", ""), "Produkt": e.get("produkt", ""),
            "Menge": num(e["menge"]), "Preis": f"{e['preis']:.2f}" if e.get("preis") is not None else "",
            "Text": e.get("text", ""), "Abrechenbar": "ja" if e["abrechenbar"] else "nein",
            "Kategorie": e.get("kategorie", ""), "Rechnung": e.get("rechnung", "")}


def entries(book: Book) -> list[dict]:
    out = []
    for path in entry_files(book):
        out += [_parse(cells, path) for cells in read_table(path).rows]
    return out


def amount(e: dict) -> Decimal:
    return money(e["menge"] * e["preis"]) if e.get("preis") is not None and e["abrechenbar"] else ZERO


def state(e: dict, invoice_status: dict[str, str], pauschal: set[str]) -> str:
    """offen | abgerechnet | pauschal (in a flat-rate project) | intern (not billable)."""
    if not e["abrechenbar"]:
        return "intern"
    if e.get("rechnung") and invoice_status.get(e["rechnung"]) not in (None, "storniert"):
        return "abgerechnet"
    if e.get("projekt") in pauschal:
        return "pauschal"
    return "offen"


def context_maps(book: Book) -> tuple[dict[str, str], set[str]]:
    status = {nr: str(m.get("status") or "aktiv") for nr, m in invoices.invoices(book).items()}
    pauschal = {nr for nr, p in projects(book).items() if p.get("abrechnung") == "pauschal"}
    return status, pauschal


def with_state(book: Book, items: list[dict] | None = None) -> list[dict]:
    status, pauschal = context_maps(book)
    out = []
    for e in entries(book) if items is None else items:
        out.append({**e, "status": state(e, status, pauschal), "betrag": amount(e)})
    return out


def _write_month(path: Path, items: list[dict]) -> None:
    items = sorted(items, key=lambda e: (e["datum"], e["id"]))
    d = items[0]["datum"] if items else None
    head = f"# Leistungen {d.month:02d}/{d.year}" if d else "# Leistungen"
    write_table(path, MdTable(COLUMNS, [_cells(e) for e in items], head=head,
                              align={"Menge": "right", "Preis": "right"}))


def month_path(book: Book, d: date) -> Path:
    return book.root / "leistungen" / "erfassung" / f"{d.year}-{d.month:02d}.md"


def _next_id(book: Book, year: int, taken: set[str]) -> str:
    prefix = f"L-{year}-"
    nums = [int(i[len(prefix):]) for i in taken if i.startswith(prefix) and i[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:04d}"


def _resolve_customer(book: Book, kunde: str, projekt: str) -> tuple[str, str]:
    kunde, projekt = str(kunde or "").strip().upper(), str(projekt or "").strip().upper()
    if projekt:
        p = project(book, projekt)
        if p.get("status") == "abgeschlossen":
            raise BookError(f"Projekt {projekt} ist abgeschlossen")
        if kunde and kunde != p["kunde"]:
            raise BookError(f"Projekt {projekt} gehört zu Kunde {p['kunde']}, nicht {kunde}")
        kunde = p["kunde"]
    if kunde:
        invoices.customer(book, kunde)
    return kunde, projekt


def add_time(book: Book, datum, wer: str, stunden, text: str, kunde: str = "", projekt: str = "",
             abrechenbar: bool = True, kategorie: str = "", satz=None) -> tuple[dict, list[Path]]:
    """Record hours. Billable hours need a customer (or project) and get the rate fixed now:
    given > project > customer (`stundensatz`) > person. Not billable: a category (Intern, Ferien …)."""
    from .saetze import hourly_rate
    d = parse_date(datum, "datum")
    p = person(book, wer)
    hours = Decimal(str(stunden or 0).replace(",", "."))
    if hours <= 0 or hours > 24:
        raise BookError("Stunden: mehr als 0 und höchstens 24")
    kunde, projekt = _resolve_customer(book, kunde, projekt)
    if abrechenbar:
        if not kunde:
            raise BookError("Abrechenbare Stunden brauchen einen Kunden oder ein Projekt")
        rate = money(parse_amount(satz, "satz")) if satz not in (None, "") else hourly_rate(book, p["nummer"], kunde, projekt)
        kategorie = ""
    else:
        rate = None
        kategorie = kategorie or "Intern"
        if kategorie not in settings(book)["kategorien"]:
            raise BookError(f"Kategorie: {', '.join(settings(book)['kategorien'])}")
    if not str(text or "").strip() and abrechenbar:
        raise BookError("Text fehlt (erscheint auf der Rechnung)")
    return _add(book, {"datum": d, "art": "Zeit", "wer": p["nummer"], "kunde": kunde, "projekt": projekt,
                       "produkt": "", "menge": hours, "preis": rate, "text": str(text or "").strip(),
                       "abrechenbar": bool(abrechenbar), "kategorie": kategorie, "rechnung": ""})


def add_material(book: Book, datum, kunde: str, produkt: str, menge, projekt: str = "", preis=None,
                 text: str = "", wer: str = "", abrechenbar: bool = True) -> tuple[dict, list[Path]]:
    """Record products used for a customer; price from the product (customer price first) unless given."""
    from .saetze import product_price
    d = parse_date(datum, "datum")
    kunde, projekt = _resolve_customer(book, kunde, projekt)
    if not kunde:
        raise BookError("Produkte brauchen einen Kunden oder ein Projekt")
    prod = product(book, produkt)
    qty = Decimal(str(menge or 0).replace(",", "."))
    if qty == 0:
        raise BookError("Menge fehlt")
    price = money(parse_amount(preis, "preis")) if preis not in (None, "") else product_price(prod, kunde)
    if wer:
        wer = person(book, wer)["nummer"]
    return _add(book, {"datum": d, "art": "Produkt", "wer": wer, "kunde": kunde, "projekt": projekt,
                       "produkt": prod["nummer"], "menge": qty, "preis": price,
                       "text": str(text or "").strip() or prod["text"], "abrechenbar": bool(abrechenbar),
                       "kategorie": "" if abrechenbar else "Garantie", "rechnung": ""})


def _add(book: Book, e: dict) -> tuple[dict, list[Path]]:
    all_ = entries(book)
    e["id"] = _next_id(book, e["datum"].year, {x["id"] for x in all_})
    path = month_path(book, e["datum"])
    same = [x for x in all_ if x["_datei"] == path]
    _write_month(path, same + [e])
    e["_datei"] = path
    return {k: v for k, v in e.items() if not k.startswith("_")}, [path]


def entry(book: Book, eid: str) -> dict:
    for e in entries(book):
        if e["id"] == eid:
            return e
    raise BookError(f"Eintrag {eid} nicht gefunden")


def _editable(book: Book, e: dict) -> None:
    status, pauschal = context_maps(book)
    if state(e, status, pauschal) == "abgerechnet":
        raise BookError(f"{e['id']} ist mit {e['rechnung']} abgerechnet — zuerst die Rechnung stornieren")


def delete_entry(book: Book, eid: str) -> tuple[dict, list[Path]]:
    e = entry(book, eid)
    _editable(book, e)
    rest = [x for x in entries(book) if x["_datei"] == e["_datei"] and x["id"] != eid]
    if rest:
        _write_month(e["_datei"], rest)
    else:
        e["_datei"].unlink()
    return {"id": eid}, [e["_datei"]]


def update_entry(book: Book, eid: str, **fields) -> tuple[dict, list[Path]]:
    """Change an entry that is not billed yet (datum, menge, preis, text, abrechenbar, kategorie, kunde, projekt)."""
    e = entry(book, eid)
    _editable(book, e)
    old_path = e["_datei"]
    if fields.get("datum"):
        e["datum"] = parse_date(fields["datum"], "datum")
    if fields.get("menge") not in (None, ""):
        e["menge"] = Decimal(str(fields["menge"]).replace(",", "."))
        if e["menge"] == 0 or (e["art"] == "Zeit" and not 0 < e["menge"] <= 24):
            raise BookError("Menge/Stunden ungültig")
    if fields.get("text") is not None:
        e["text"] = str(fields["text"]).strip()
    if "kunde" in fields or "projekt" in fields:
        e["kunde"], e["projekt"] = _resolve_customer(book, fields.get("kunde", e["kunde"]),
                                                     fields.get("projekt", e["projekt"]))
    if fields.get("abrechenbar") is not None:
        e["abrechenbar"] = bool(fields["abrechenbar"])
        if not e["abrechenbar"]:
            e["kategorie"] = fields.get("kategorie") or e["kategorie"] or "Intern"
        else:
            e["kategorie"] = ""
    if fields.get("preis") not in (None, ""):
        e["preis"] = money(parse_amount(fields["preis"], "preis"))
    if e["abrechenbar"]:
        if not e["kunde"]:
            raise BookError("Abrechenbare Einträge brauchen einen Kunden")
        if e["preis"] is None:
            from .saetze import hourly_rate
            e["preis"] = hourly_rate(book, e["wer"], e["kunde"], e["projekt"])
    elif e["art"] == "Zeit":
        e["preis"] = None
    new_path = month_path(book, e["datum"])
    others = [x for x in entries(book) if x["id"] != eid]
    touched = {old_path, new_path}
    for p in touched:
        items = [x for x in others if x["_datei"] == p] + ([e] if p == new_path else [])
        if items:
            _write_month(p, items)
        elif p.exists():
            p.unlink()
    return {k: v for k, v in e.items() if not k.startswith("_")}, sorted(touched)


def mark_billed(book: Book, ids: list[str], rechnung: str) -> list[Path]:
    wanted = set(ids)
    touched = []
    by_file: dict[Path, list[dict]] = {}
    for e in entries(book):
        by_file.setdefault(e["_datei"], []).append(e)
    for path, items in by_file.items():
        if any(e["id"] in wanted for e in items):
            for e in items:
                if e["id"] in wanted:
                    e["rechnung"] = rechnung
            _write_month(path, items)
            touched.append(path)
    return touched


def add_customer(book: Book, **fields) -> tuple[dict, list[Path]]:
    """A new customer from inside the plugin (same as `aeradex customer add`)."""
    clean = {k: str(v).strip() for k, v in fields.items() if v not in (None, "")}
    if not clean.get("name"):
        raise BookError("Kunde: Name fehlt")
    if "stundensatz" in clean:
        clean["stundensatz"] = parse_amount(clean["stundensatz"], "stundensatz")
    meta, path = invoices.add_customer(book, **clean)
    return meta, [path]

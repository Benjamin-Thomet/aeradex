"""Swiss monthly payroll: payslip calculation, employer contributions, booking.

Files:
    lohn/einstellungen.yaml          rates and accounts (Ausgleichskasse, UVG, KTG, BVG)
    personal/M0001-<name>.md         one employee per file
    lohn/<JJJJ>/<MM>/M0001.md        one payslip per employee and month

A payslip is a draft until it is closed. Closing freezes every figure,
including the employer contributions, and writes a fingerprint. The journal gets
one Sammelbuchung per month (Beleg L-JJJJ-MM) over all closed payslips; closing
or reopening a payslip rebuilds it (only while the month is not locked).
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from copy import deepcopy
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import qst
from .book import Book, BookError, Row
from .files import (CENT, parse_date, read_frontmatter, read_yaml, slug, write_frontmatter,
                    write_yaml)
from .journal import ensure_open, post

ZERO = Decimal("0")

DEFAULT_CONFIG = {
    "buchen": True,
    "konten": {
        "lohnaufwand": "5000",
        "auszahlung": "1020",
        # Kinderzulagen are advanced on behalf of the Familienausgleichskasse and
        # credited back against what the employer owes it — not an expense.
        "kinderzulagen": "2270",
        "ahv": "2270", "alv": "2270", "bvg": "2272", "ktg": "2271", "uvg": "2271",
        "quellensteuer": "2279",
    },
    # Arbeitnehmer-Abzüge, als Anteil des Bruttolohns.
    "saetze_an": {"ahv": 0.053, "alv": 0.011, "uvg": 0.007, "ktg": 0.007},
    # Arbeitgeber-Beiträge. Verwaltungskosten bemessen sich auf den AHV/IV/EO-Beiträgen.
    "saetze_ag": {"ahv": 0.053, "alv": 0.011, "fak": 0.015, "uvg": 0.005, "ktg": 0.007,
                  "verwaltung": 0.002},
    # BVG ist nie ein Satz: gleich_an = AG zahlt denselben Betrag wie AN (paritätisch),
    # betrag = AG zahlt den beim Mitarbeiter hinterlegten ag_bvg_betrag.
    "ag_bvg": "gleich_an",
    # (Aufwandkonto, Verbindlichkeitskonto) je Arbeitgeberbeitrag.
    "ag_konten": {
        "ahv": ["5700", "2270"], "alv": ["5700", "2270"], "fak": ["5710", "2270"],
        "verwaltung": ["5700", "2270"], "bvg": ["5720", "2272"], "uvg": ["5730", "2271"],
        "ktg": ["5740", "2271"],
    },
}

AG_ORDER = ["ahv", "alv", "uvg", "ktg", "bvg", "fak", "verwaltung"]
AG_LABEL = {"ahv": "AHV/IV/EO", "alv": "ALV", "uvg": "UVG (BU)", "ktg": "KTG", "bvg": "BVG",
            "fak": "FAK", "verwaltung": "Verwaltungskosten"}


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def D(value) -> Decimal:
    return Decimal(str(value or 0))


# ---------- configuration ----------

def config_path(book: Book) -> Path:
    return book.root / "lohn" / "einstellungen.yaml"


def config(book: Book) -> dict:
    cfg = deepcopy(DEFAULT_CONFIG)
    path = config_path(book)
    if path.exists():
        data = read_yaml(path)
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def write_default_config(book: Book) -> Path:
    path = config_path(book)
    if not path.exists():
        write_yaml(path, DEFAULT_CONFIG)
    return path


# ---------- employees ----------

def employees(book: Book) -> dict[str, dict]:
    out = {}
    folder = book.root / "personal"
    for path in sorted(folder.glob("*.md")) if folder.exists() else []:
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        out[str(meta.get("nummer"))] = meta
    return out


def employee(book: Book, nr: str) -> dict:
    found = employees(book).get(str(nr))
    if found is None:
        raise BookError(f"Mitarbeiter {nr} nicht gefunden")
    return found


def display_name(emp: dict) -> str:
    return f"{emp.get('vorname', '')} {emp.get('nachname', '')}".strip() or str(emp.get("nummer"))


def add_employee(book: Book, vorname: str, nachname: str, **fields) -> tuple[dict, Path]:
    nums = [int(k[1:]) for k in employees(book) if re.fullmatch(r"M\d+", k)]
    number = f"M{max(nums, default=0) + 1:04d}"
    meta = {"nummer": number, "vorname": vorname, "nachname": nachname,
            "adresse": {"strasse": fields.pop("strasse", ""), "nr": str(fields.pop("nr", "") or ""),
                        "plz": str(fields.pop("plz", "") or ""), "ort": fields.pop("ort", ""),
                        "land": fields.pop("land", "CH")},
            "ahv_nr": fields.pop("ahv_nr", ""), "geburtsdatum": fields.pop("geburtsdatum", None),
            "eintritt": fields.pop("eintritt", None), "austritt": None,
            "lohnart": fields.pop("lohnart", "monat"),
            "monatslohn": fields.pop("monatslohn", 0), "pensum": fields.pop("pensum", 100),
            "stundenlohn": fields.pop("stundenlohn", 0), "standard_stunden": fields.pop("standard_stunden", 0),
            "vollzeit_stunden_woche": fields.pop("vollzeit_stunden_woche", 42),
            "ferienzuschlag_satz": fields.pop("ferienzuschlag_satz", 0),
            "ferien_inbegriffen": fields.pop("ferien_inbegriffen", False),
            "bvg_betrag": fields.pop("bvg_betrag", 0), "ag_bvg_betrag": fields.pop("ag_bvg_betrag", 0),
            "kinderzulagen": fields.pop("kinderzulagen", 0),
            "qst": fields.pop("qst", None), "qst_satz": fields.pop("qst_satz", 0),
            "iban": fields.pop("iban", ""), "aktiv": True}
    if fields:
        raise BookError(f"Unbekannte Felder: {', '.join(fields)}")
    for key in ("geburtsdatum", "eintritt"):
        if meta[key]:
            meta[key] = parse_date(meta[key], key).isoformat()
    for key in ("monatslohn", "pensum", "stundenlohn", "standard_stunden", "vollzeit_stunden_woche",
                "ferienzuschlag_satz", "bvg_betrag", "ag_bvg_betrag", "kinderzulagen", "qst_satz"):
        meta[key] = Decimal(str(meta[key] or 0))
    if meta["qst"]:
        qst.parse_code(meta["qst"].get("code", ""))
    meta["ferien_inbegriffen"] = bool(meta["ferien_inbegriffen"])
    path = book.root / "personal" / f"{number}-{slug(vorname + ' ' + nachname)}.md"
    write_frontmatter(path, meta, "Notizen zum Arbeitsverhältnis.")
    return meta, path


def is_hourly(emp: dict) -> bool:
    return (emp.get("lohnart") or "monat") == "stunde"


def uses_tarif(emp: dict) -> bool:
    t = emp.get("qst") or {}
    return bool(t.get("kanton") and t.get("jahr") and t.get("code"))


def monatsstunden(emp: dict) -> Decimal:
    return D(emp.get("vollzeit_stunden_woche")) * Decimal("52") / Decimal("12")


# ---------- calculation (ported 1:1 from the Command Station) ----------

def eigenes_pensum(emp: dict, hours=None) -> Decimal:
    """This employer's own workload for the month in percent (KS 45 Ziff. 6.4)."""
    if not is_hourly(emp):
        return D(emp.get("pensum") if emp.get("pensum") is not None else 100)
    ms = monatsstunden(emp)
    if not ms:
        return ZERO
    worked = D(hours if hours is not None else emp.get("standard_stunden"))
    return worked / ms * Decimal("100")


def satzbestimmendes_einkommen(gross, eigenes, gesamtpensum, fallback=None) -> Decimal:
    """satzbestimmend = Brutto / eigenes Pensum × Gesamtpensum (KS 45 Ziff. 6.4).
    A zero total means the typed amount stands (Ziff. 6.5, hourly 180 h rule)."""
    eigen, gesamt = D(eigenes), D(gesamtpensum)
    if eigen <= 0 or gesamt <= 0:
        return money(fallback or 0)
    return money(D(gross) / eigen * gesamt)


def quellensteuer_rate(emp: dict, satzbestimmend=None, year=None) -> Decimal:
    if not uses_tarif(emp):
        return D(emp.get("qst_satz"))
    t = emp["qst"]
    try:
        return qst.rate(t["kanton"], int(year or t["jahr"]), t["code"], satzbestimmend or 0)
    except qst.UnknownTariff as exc:
        raise BookError(str(exc)) from exc


def calculate(emp: dict, cfg: dict, year: int, month: int, inputs: dict, tariff: dict | None = None) -> dict:
    """All wage, deduction and net amounts for one month.

    Monthly wage = Monatslohn × Pensum, prorated by calendar days on a mid-month
    start; hourly wage = Stundenlohn × Stunden. The Ferienzuschlag is added on top,
    or split back out when `ferien_inbegriffen`. QST additionally taxes allowances."""
    an = cfg["saetze_an"]
    full_time = money(emp.get("monatslohn"))
    pensum = D(emp.get("pensum") if emp.get("pensum") is not None else 100) / Decimal("100")
    rate = money(emp.get("stundenlohn"))
    hours = D(inputs.get("stunden") if inputs.get("stunden") is not None else emp.get("standard_stunden"))

    if inputs.get("lohn") is not None:
        wage = money(inputs["lohn"])
    elif is_hourly(emp):
        wage = money(rate * hours)
    else:
        wage = money(full_time * pensum)
        start = parse_date(emp["eintritt"]) if emp.get("eintritt") else None
        days = calendar.monthrange(year, month)[1]
        if start and start > date(year, month, days):
            wage = money(0)
        elif start and start > date(year, month, 1):
            wage = money(wage * Decimal(days - start.day + 1) / Decimal(days))
        end = parse_date(emp["austritt"]) if emp.get("austritt") else None
        if end and end < date(year, month, 1):
            wage = money(0)
        elif end and end < date(year, month, days):
            first = max(start, date(year, month, 1)) if start else date(year, month, 1)
            wage = money(full_time * pensum * Decimal((end - first).days + 1) / Decimal(days))

    ferien_rate = D(emp.get("ferienzuschlag_satz"))
    if emp.get("ferien_inbegriffen") and ferien_rate:
        gross = wage
        base = money(gross / (Decimal("1") + ferien_rate))
        ferien = money(gross - base)
    else:
        base = wage
        ferien = money(base * ferien_rate)
        gross = money(base + ferien)

    def pct(r):
        return money(gross * D(r))

    ahv, alv, ktg, uvg = pct(an.get("ahv")), pct(an.get("alv")), pct(an.get("ktg")), pct(an.get("uvg"))
    bvg = money(inputs.get("bvg") if inputs.get("bvg") is not None else emp.get("bvg_betrag"))
    kz = money(inputs.get("kinderzulagen") if inputs.get("kinderzulagen") is not None else emp.get("kinderzulagen"))
    # KS 45 3.2 / 6.3: family allowances are taxable, although AHV-exempt.
    taxable = money(gross + kz)
    eigen = eigenes_pensum(emp, hours)
    explicit = inputs.get("qst_satzbestimmend")
    total_workload = D(inputs.get("qst_gesamtpensum"))
    if explicit is not None and total_workload:
        raise BookError("QST: entweder Gesamtpensum oder satzbestimmendes Einkommen angeben")
    if explicit is not None:
        satzb = money(explicit)
    else:
        satzb = taxable  # KS 45 6.3/6.4: one employment, monthly payment
        if uses_tarif(emp):
            from .qst_estv import ANNUAL
            if emp['qst']['kanton'].upper() in ANNUAL:
                raise BookError("QST-Jahresmodell: geprüftes satzbestimmendes Jahreseinkommen / 12 eingeben; Jahresausgleich separat prüfen")
            # KS 45 6.6: only periodic components are currently modelled here.
            first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
            start = max(parse_date(emp['eintritt']), first) if emp.get('eintritt') else first
            end = min(parse_date(emp['austritt']), last) if emp.get('austritt') else last
            start_day = min(start.day, 30)
            if start == last:
                start_day = 30
            end_day = 30 if end == last else min(end.day, 30)
            tax_days = end_day - start_day + 1 if first <= start <= end <= last else 0
            if taxable > 0 and 0 < tax_days < 30:
                satzb = money(taxable * 30 / tax_days)
        if total_workload:
            if eigen <= 0 or total_workload < eigen:
                raise BookError("QST-Gesamtpensum muss mindestens dem eigenen Pensum entsprechen")
            satzb = money(satzb / eigen * total_workload)
    minimum = ZERO
    if emp.get('qst') and not uses_tarif(emp):
        raise BookError("Quellensteuer: Kanton, Jahr und Tarifcode vollständig erfassen")
    if uses_tarif(emp) and taxable > 0 and satzb <= 0:
        raise BookError("QST: satzbestimmendes Einkommen muss positiv sein")
    if uses_tarif(emp) and tariff is not None:
        from .qst_estv import lookup
        try:
            qst_rate, minimum = lookup(tariff, emp['qst']['code'], satzb)
        except qst.UnknownTariff as exc:
            raise BookError(str(exc)) from exc
    else:
        qst_rate = quellensteuer_rate(emp, satzb, year)
    quellensteuer = money(max(taxable * qst_rate, minimum)) if taxable > 0 else ZERO
    korrektur = money(inputs.get("korrektur"))
    abzuege = ahv + alv + bvg + ktg + uvg + quellensteuer
    net = gross + kz + korrektur - abzuege
    return {
        "lohnart": "stunde" if is_hourly(emp) else "monat",
        "pensum": money(D(emp.get("pensum") if emp.get("pensum") is not None else 100)), "stundenlohn": rate, "stunden": money(hours),
        "grundlohn": base, "ferienzuschlag_satz": ferien_rate, "ferienzuschlag": ferien,
        "bruttolohn": gross,
        "ahv": ahv, "alv": alv, "uvg": uvg, "ktg": ktg, "bvg": bvg,
        "qst_code": emp["qst"]["code"] if uses_tarif(emp) else "",
        "qst_quelle": (tariff or {}).get("sha256", ""), "qst_basis": taxable, "qst_jahr": year,
        "qst_kanton": (emp.get("qst") or {}).get("kanton", ""),
        "qst_satz": qst_rate, "qst_satzbestimmend": satzb, "quellensteuer": quellensteuer,
        "kinderzulagen": kz, "korrektur": korrektur, "total_abzuege": abzuege, "nettolohn": net,
    }


def employer_contributions(cfg: dict, gross, employee_bvg=0, employer_bvg=0) -> dict[str, Decimal]:
    """AG-Beiträge on top of gross. Verwaltungskosten are levied on the AHV/IV/EO
    contributions (both halves), BVG is a franc amount from the Vorsorgeplan."""
    ag, an = cfg["saetze_ag"], cfg["saetze_an"]
    gross = money(gross)
    out = {code: money(gross * D(ag.get(code))) for code in ("ahv", "alv", "uvg", "ktg", "fak")}
    out["bvg"] = money(employer_bvg if cfg.get("ag_bvg") == "betrag" else employee_bvg)
    basis = money(gross * D(an.get("ahv"))) + out["ahv"]
    out["verwaltung"] = money(basis * D(ag.get("verwaltung")))
    return {code: out[code] for code in AG_ORDER}


# ---------- payslip files ----------

def payslip_path(book: Book, year: int, month: int, nr: str) -> Path:
    return book.root / "lohn" / str(year) / f"{month:02d}" / f"{nr}.md"


def payslips(book: Book, year: int | None = None) -> list[dict]:
    folder = book.root / "lohn"
    pattern = f"{year}/*/M*.md" if year else "*/*/M*.md"
    out = []
    for path in sorted(folder.glob(pattern)) if folder.exists() else []:
        meta, _ = read_frontmatter(path)
        meta["_pfad"] = path
        out.append(meta)
    return out


def load_payslip(book: Book, year: int, month: int, nr: str) -> dict:
    path = payslip_path(book, year, month, nr)
    if not path.exists():
        raise BookError(f"Keine Lohnabrechnung {nr} {month:02d}/{year} — zuerst `aeradex payroll run {year}-{month:02d}`")
    meta, _ = read_frontmatter(path)
    meta["_pfad"] = path
    return meta


def _fingerprint(meta: dict) -> str:
    frozen = {k: meta.get(k) for k in ("mitarbeiter", "jahr", "monat", "werte", "ag")}
    blob = json.dumps(frozen, sort_keys=True, default=lambda v: f"{Decimal(str(v)):.5f}")
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def payslip_fingerprint_ok(meta: dict) -> bool:
    return meta.get("fingerprint") == _fingerprint(_numeric(meta))


def _numeric(meta: dict) -> dict:
    """Make the fingerprint independent of Decimal/float/str representation."""
    out = dict(meta)
    for key in ("werte", "ag"):
        out[key] = {k: (f"{Decimal(str(v)):.5f}" if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v)
                    for k, v in (meta.get(key) or {}).items()}
    return out


def _body(emp: dict, meta: dict) -> str:
    w = meta["werte"]
    lines = [f"# Lohnabrechnung {display_name(emp)} — {meta['monat']:02d}/{meta['jahr']}", "",
             "| Position | Betrag |", "| --- | ---: |"]

    def add(label, value, sign=""):
        if Decimal(str(value or 0)):
            lines.append(f"| {label} | {sign}{Decimal(str(value)):,.2f} |".replace(",", "'"))

    if w["lohnart"] == "stunde":
        add(f"Grundlohn {w['stunden']} h", w["grundlohn"])
    else:
        add(f"Grundlohn ({w['pensum']} %)", w["grundlohn"])
    add(f"Ferienzuschlag {Decimal(str(w['ferienzuschlag_satz'])) * 100:.2f} %", w["ferienzuschlag"])
    add("**Bruttolohn**", w["bruttolohn"])
    for key, label in (("ahv", "AHV/IV/EO"), ("alv", "ALV"), ("uvg", "NBU"), ("ktg", "KTG"),
                       ("bvg", "BVG"), ("quellensteuer", f"Quellensteuer {w.get('qst_code') or ''}".strip())):
        add(label, w[key], "−")
    add("Kinderzulagen", w["kinderzulagen"])
    add("Korrektur", w["korrektur"])
    if Decimal(str(w.get("spesen") or 0)):
        add("**Nettolohn**", w["nettolohn"])
        add(f"Spesen ({', '.join(meta['eingaben'].get('spesen') or [])})", w["spesen"])
        add("**Auszahlung**", w["auszahlung"])
    else:
        add("**Nettolohn (Auszahlung)**", w["nettolohn"])
    lines += ["", "Status: " + meta["status"] + ". Diese Tabelle wird von aeradex erzeugt; "
              "Eingaben im Frontmatter unter `eingaben:` ändern und `aeradex payroll run` erneut ausführen."]
    return "\n".join(lines)


def _save(book: Book, emp: dict, meta: dict) -> Path:
    path = payslip_path(book, meta["jahr"], meta["monat"], meta["mitarbeiter"])
    clean = {k: v for k, v in meta.items() if not k.startswith("_")}
    write_frontmatter(path, clean, _body(emp, clean))
    return path


def _with_expenses(book: Book, meta: dict) -> None:
    """Add the employee's expense claims to a draft: those it already lists plus every open one up to the
    month's end. They are paid out with the net wage and are not part of the gross (no social insurance)."""
    from . import spesen
    year, month = int(meta["jahr"]), int(meta["monat"])
    end = date(year, month, calendar.monthrange(year, month)[1])
    listed = [str(n) for n in (meta["eingaben"].get("spesen") or []) if str(n) in spesen.claims(book)]
    fresh = [m["nummer"] for m in spesen.open_claims(book, meta["mitarbeiter"], end) if m["nummer"] not in listed]
    numbers = sorted(set(listed + fresh))
    if numbers:
        meta["eingaben"]["spesen"] = numbers
        meta["spesenkonto"] = spesen.konto(book)
        total = sum((spesen.amount_chf(book, spesen.claim(book, n)) for n in numbers), ZERO)
        meta["werte"]["spesen"] = money(total)
        meta["werte"]["auszahlung"] = money(D(meta["werte"]["nettolohn"]) + total)
    else:
        meta["eingaben"].pop("spesen", None)
        meta.pop("spesenkonto", None)


def _tariff(book, emp, year):
    from .qst_estv import table
    return table(book, emp['qst']['kanton'], year) if uses_tarif(emp) else None


def run(book: Book, year: int, month: int, nr: str | None = None, inputs: dict | None = None) -> list[tuple[dict, Path]]:
    """Create or recalculate draft payslips for the month (all active employees,
    or one). Closed payslips are left alone."""
    from .payroll_payments import active
    if active(book, year, month):
        raise BookError("Lohnzahlungsdatei besteht bereits — zuerst zurückziehen")
    cfg = config(book)
    out = []
    targets = [employee(book, nr)] if nr else [e for e in employees(book).values() if e.get("aktiv", True)]
    for emp in targets:
        path = payslip_path(book, year, month, emp["nummer"])
        if path.exists():
            meta, _ = read_frontmatter(path)
            if meta.get("status") == "abgeschlossen":
                if inputs:
                    raise BookError(f"Lohnabrechnung {emp['nummer']} {month:02d}/{year} ist abgeschlossen — zuerst reopen")
                continue
        else:
            meta = {"mitarbeiter": emp["nummer"], "name": display_name(emp), "jahr": year,
                    "monat": month, "status": "entwurf",
                    "eingaben": {"stunden": None, "bvg": None, "kinderzulagen": None, "korrektur": 0,
                                 "korrektur_text": "", "qst_satzbestimmend": None, "qst_gesamtpensum": None}}
        if inputs:
            meta["eingaben"].update({k: v for k, v in inputs.items() if v is not None})
        meta["werte"] = calculate(emp, cfg, year, month, meta["eingaben"], _tariff(book, emp, year))
        _with_expenses(book, meta)
        meta.pop("ag", None)
        meta.pop("fingerprint", None)
        out.append((emp, meta))
    # Validate every employee before writing any draft.
    return [(meta, _save(book, emp, meta)) for emp, meta in out]


def _lines(meta: dict, cfg: dict) -> list[tuple[str, str, Decimal, str]]:
    """Signed (soll, haben, amount, label) lines of one closed payslip."""
    k = cfg["konten"]
    w, ag = meta["werte"], meta.get("ag") or {}
    lines = [
        (k["lohnaufwand"], k["auszahlung"], D(w["nettolohn"]) - D(w["kinderzulagen"]), "Auszahlung"),
        (k["kinderzulagen"], k["auszahlung"], D(w["kinderzulagen"]), "Kinderzulagen"),
        (k["lohnaufwand"], k["ahv"], D(w["ahv"]), "AN AHV/IV/EO"),
        (k["lohnaufwand"], k["alv"], D(w["alv"]), "AN ALV"),
        (k["lohnaufwand"], k["bvg"], D(w["bvg"]), "AN BVG"),
        (k["lohnaufwand"], k["ktg"], D(w["ktg"]), "AN KTG"),
        (k["lohnaufwand"], k["uvg"], D(w["uvg"]), "AN NBU"),
        (k["lohnaufwand"], k["quellensteuer"], D(w["quellensteuer"]), "Quellensteuer"),
    ]
    if D(w.get("spesen")):
        lines.append((str(meta.get("spesenkonto") or "2210"), k["auszahlung"], D(w["spesen"]),
                      "Spesen (effektiv)"))
    for code in AG_ORDER:
        expense, payable = cfg["ag_konten"][code]
        lines.append((str(expense), str(payable), D(ag.get(code)), f"AG {AG_LABEL[code]}"))
    return [(str(s), str(h), money(a), label) for s, h, a, label in lines]


def _rows(lines, d: date, beleg: str, desc: str, quelle: str) -> list[Row]:
    rows = []
    for soll, haben, amount, label in lines:
        if not amount:
            continue
        if amount < 0:
            soll, haben, amount = haben, soll, -amount
        rows.append(Row(d, beleg, f"{desc} · {label}", soll, haben, amount, quelle))
    return rows


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def month_source(year: int, month: int) -> str:
    return f"lohn:{year}-{month:02d}"


def booking_rows(meta: dict, cfg: dict) -> list[Row]:
    """Per-payslip rows: how payroll was booked before the monthly Sammelbuchung (legacy books)."""
    year, month, nr = int(meta["jahr"]), int(meta["monat"]), meta["mitarbeiter"]
    return _rows(_lines(meta, cfg), month_end(year, month), f"L-{year}-{month:02d}-{nr}",
                 f"Lohn {meta.get('name') or nr} {month:02d}/{year}", f"{month_source(year, month)}:{nr}")


def month_rows(slips: list[dict], cfg: dict, year: int, month: int) -> list[Row]:
    """One Sammelbuchung for all closed payslips of the month: the journal shows totals per
    account pair, never individual salaries. Beleg L-JJJJ-MM; its net effect on the bank
    account equals the batch debit of the salary payment file."""
    totals: dict[tuple[str, str, str], Decimal] = {}
    for meta in slips:
        if meta.get("status") != "abgeschlossen":
            continue
        for soll, haben, amount, label in _lines(meta, cfg):
            totals[(soll, haben, label)] = totals.get((soll, haben, label), ZERO) + amount
    count = sum(1 for m in slips if m.get("status") == "abgeschlossen")
    lines = [(s, h, a, label) for (s, h, label), a in totals.items()]
    return _rows(lines, month_end(year, month), f"L-{year}-{month:02d}",
                 f"Löhne {month:02d}/{year} ({count} MA)", month_source(year, month))


def _month_slips(book: Book, year: int, month: int) -> list[dict]:
    return [p for p in payslips(book, year) if int(p["monat"]) == month]


def _rebook(book: Book, year: int, month: int) -> list[Path]:
    """Replace the month's payroll booking (including per-payslip rows of older books) by one
    Sammelbuchung over every closed payslip."""
    cfg = config(book)
    ensure_open(book, month_end(year, month))
    src = month_source(year, month)
    touched = book.remove_rows(lambda r: r.quelle == src or r.quelle.startswith(src + ":"))
    rows = month_rows(_month_slips(book, year, month), cfg, year, month) if cfg.get("buchen", True) else []
    if rows:
        touched += post(book, rows)
    return touched


def _freeze(book: Book, cfg: dict, meta: dict) -> dict:
    """Recalculate and freeze a draft in memory; returns the employee. The caller saves it."""
    year, month, nr = int(meta["jahr"]), int(meta["monat"]), meta["mitarbeiter"]
    emp = employee(book, nr)
    # Recalculate once more so the frozen figures match the current inputs.
    meta["werte"] = calculate(emp, cfg, year, month, meta.get("eingaben") or {}, _tariff(book, emp, year))
    meta.setdefault("eingaben", {})
    _with_expenses(book, meta)
    w = meta["werte"]
    meta["ag"] = employer_contributions(cfg, w["bruttolohn"], w["bvg"], emp.get("ag_bvg_betrag"))
    meta["status"] = "abgeschlossen"
    meta["abgeschlossen_am"] = date.today().isoformat()
    meta["fingerprint"] = _fingerprint(_numeric(meta))
    return emp


def close(book: Book, year: int, month: int, nr: str) -> tuple[dict, list[Path]]:
    meta = load_payslip(book, year, month, nr)
    if meta.get("status") == "abgeschlossen":
        raise BookError(f"Lohnabrechnung {nr} {month:02d}/{year} ist bereits abgeschlossen")
    ensure_open(book, month_end(year, month))
    path = _save(book, _freeze(book, config(book), meta), meta)
    return meta, _rebook(book, year, month) + [path]


def close_month(book: Book, year: int, month: int) -> tuple[list[dict], list[Path]]:
    """Close every draft of the month at once and book them as one Sammelbuchung."""
    drafts = [m for m in _month_slips(book, year, month) if m.get("status") != "abgeschlossen"]
    if not drafts:
        raise BookError(f"Keine offenen Lohnabrechnungen {month:02d}/{year} — zuerst den Lohnlauf rechnen")
    ensure_open(book, month_end(year, month))
    cfg = config(book)
    frozen = [(_freeze(book, cfg, meta), meta) for meta in drafts]   # validate all before writing
    paths = [_save(book, emp, meta) for emp, meta in frozen]
    return drafts, _rebook(book, year, month) + paths


def reopen(book: Book, year: int, month: int, nr: str) -> tuple[dict, list[Path]]:
    from .payroll_payments import active
    if active(book, year, month):
        raise BookError("Zuerst die Lohnzahlungsdatei zurückziehen und einen Bankauftrag gegebenenfalls bei der Bank stornieren")
    meta = load_payslip(book, year, month, nr)
    if meta.get("status") != "abgeschlossen":
        raise BookError(f"Lohnabrechnung {nr} {month:02d}/{year} ist nicht abgeschlossen")
    ensure_open(book, month_end(year, month))
    meta["status"] = "entwurf"
    for key in ("ag", "fingerprint", "abgeschlossen_am"):
        meta.pop(key, None)
    path = _save(book, employee(book, nr), meta)
    return meta, _rebook(book, year, month) + [path]


def lohnkonto(book: Book, year: int, nr: str) -> dict:
    """Lohnkonto: one employee's closed months side by side with year totals."""
    keys = ["bruttolohn", "ahv", "alv", "uvg", "ktg", "bvg", "quellensteuer", "kinderzulagen",
            "korrektur", "nettolohn"]
    months = {int(p["monat"]): p for p in payslips(book, year)
              if p["mitarbeiter"] == nr and p.get("status") == "abgeschlossen"}
    totals = {k: sum((D(m["werte"].get(k)) for m in months.values()), ZERO) for k in keys}
    ag_totals = {k: sum((D((m.get("ag") or {}).get(k)) for m in months.values()), ZERO) for k in AG_ORDER}
    return {"mitarbeiter": nr, "jahr": year,
            "monate": {m: {k: D(p["werte"].get(k)) for k in keys} for m, p in sorted(months.items())},
            "total": totals, "ag_total": ag_totals}

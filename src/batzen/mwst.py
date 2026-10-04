"""Mehrwertsteuer: codes, automatic tax split, MWST-Abrechnung (ESTV).

Settings in batzen.yaml:

    mwst:
      methode: keine | effektiv | saldo
      periode: quartal | semester          # effektiv: Quartal, saldo: Semester
      saldosteuersatz: 6.2                 # nur Saldo-Methode, in Prozent
      konten: {vorsteuer: "1170", vorsteuer_inv: "1171", umsatzsteuer: "2200",
               abrechnung: "2201", saldosteuer: "3809"}

Amounts are always entered gross (as on the receipt). With the effective
method a booking with a code is split: the net amount stays on the expense or
revenue account, the tax goes to the Vorsteuer or Umsatzsteuer account; both
rows carry the code in the journal's `MWST` column. With the Saldosteuersatz
method revenue stays gross and the tax is booked once per period.

Bezugsteuer (Art. 45 MWSTG, Ziffern 382/383): services bought from companies
abroad (codes B81, B26). The expense is booked as invoiced; the tax is owed on
it (`konten.bezugsteuer`, default the Umsatzsteuer account) and — with the
effective method — deducted again as Vorsteuer (Ziffer 400 for 4xxx accounts,
else 405), so it usually nets to zero. With the Saldosteuersatz method it is
owed without deduction and becomes a cost on the same account.

The ESTV form numbers (Ziffern) follow the rates valid since 1.1.2024
(8.1 / 2.6 / 3.8 %). Check them against the current form before filing.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .book import Book, BookError, Row
from .files import CENT, read_yaml, write_yaml

ZERO = Decimal("0")

RATES = {"81": Decimal("8.1"), "26": Decimal("2.6"), "38": Decimal("3.8")}
RATE_LABEL = {"81": "Normalsatz 8.1 %", "26": "reduzierter Satz 2.6 %", "38": "Beherbergung 3.8 %"}


@dataclass(frozen=True)
class Code:
    code: str
    kind: str            # umsatz | befreit | ausgenommen | vorsteuer | investition | bezug
    rate: Decimal
    label: str


def _codes() -> dict[str, Code]:
    out = {}
    for key, rate in RATES.items():
        out[f"U{key}"] = Code(f"U{key}", "umsatz", rate, f"Umsatz {RATE_LABEL[key]}")
        out[f"V{key}"] = Code(f"V{key}", "vorsteuer", rate, f"Vorsteuer Material/Dienstleistungen {rate} %")
        out[f"I{key}"] = Code(f"I{key}", "investition", rate, f"Vorsteuer Investitionen/übriger Aufwand {rate} %")
    for key in ("81", "26"):
        out[f"B{key}"] = Code(f"B{key}", "bezug", RATES[key], f"Bezugsteuer {RATES[key]} % (Dienstleistungen aus dem Ausland)")
    out["U0"] = Code("U0", "befreit", ZERO, "Umsatz steuerbefreit (z.B. Export, Ziff. 220)")
    out["UA"] = Code("UA", "ausgenommen", ZERO, "Umsatz von der Steuer ausgenommen (Ziff. 230)")
    return out


CODES = _codes()
DEFAULT_KONTEN = {"vorsteuer": "1170", "vorsteuer_inv": "1171", "umsatzsteuer": "2200",
                  "abrechnung": "2201", "saldosteuer": "3809", "differenz": "3800"}
BEZUG_ZIFFERN = {"81": "382", "26": "383"}


def config(book: Book) -> dict:
    raw = book.settings.get("mwst") or {}
    methode = raw.get("methode") or "keine"
    return {"methode": methode,
            "periode": raw.get("periode") or ("semester" if methode == "saldo" else "quartal"),
            "saldosteuersatz": Decimal(str(raw.get("saldosteuersatz") or 0)),
            "taetigkeit": str(raw.get("taetigkeit") or ""),        # ESTV-Tätigkeits-ID (SSS ab 2025)
            "abrechnungsart": raw.get("abrechnungsart") or "vereinbart",
            "konten": _konten(raw)}


def _konten(raw: dict) -> dict:
    konten = {**DEFAULT_KONTEN, **{k: str(v) for k, v in (raw.get("konten") or {}).items() if v}}
    konten.setdefault("bezugsteuer", konten["umsatzsteuer"])
    return konten


def code(value: str) -> Code:
    c = CODES.get((value or "").strip().upper())
    if c is None:
        raise BookError(f"Unbekannter MWST-Code '{value}'. Gültig: {', '.join(CODES)}")
    return c


def tax_from_gross(gross: Decimal, rate: Decimal) -> Decimal:
    return (gross * rate / (Decimal(100) + rate)).quantize(CENT, rounding=ROUND_HALF_UP)


def tax_from_net(net: Decimal, rate: Decimal) -> Decimal:
    return (net * rate / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)


def tax_account(cfg: dict, c: Code) -> str | None:
    if c.kind == "umsatz" and c.rate:
        return cfg["konten"]["umsatzsteuer"]
    if c.kind == "vorsteuer":
        return cfg["konten"]["vorsteuer"]
    if c.kind == "investition":
        return cfg["konten"]["vorsteuer_inv"]
    return None


def split(book: Book, row: Row, mwst: str) -> list[Row]:
    """Turn one gross booking with a code into the rows the method requires."""
    if not mwst:
        return [row]
    cfg = config(book)
    c = code(mwst)
    if cfg["methode"] == "keine":
        raise BookError("Dieses Buch ist nicht MWST-pflichtig (Einstellungen → MWST). Ohne Code buchen.")
    if cfg["methode"] == "saldo" and c.kind in ("vorsteuer", "investition"):
        raise BookError("Bei der Saldosteuersatzmethode gibt es keinen Vorsteuerabzug — ohne Code buchen.")
    if not (row.soll and row.haben):
        raise BookError("MWST-Codes gehen nur bei einfachen Buchungen (Soll und Haben); "
                        "bei Sammelbuchungen den Code je Zeile setzen.")
    row.mwst = c.code
    if c.kind == "bezug":
        return bezug_rows(cfg, row, c)
    account = tax_account(cfg, c)
    if cfg["methode"] == "saldo" or account is None:
        return [row]                                  # gross stays on the revenue account
    tax = tax_from_gross(row.betrag, c.rate)
    net = row.betrag - tax
    if c.kind == "umsatz":
        # Money side keeps the gross; revenue gets the net, Umsatzsteuer the tax.
        return [Row(row.datum, row.beleg, row.text, row.soll, "", row.betrag, row.quelle, mwst=""),
                Row(row.datum, row.beleg, row.text, "", row.haben, net, row.quelle, mwst=c.code),
                Row(row.datum, row.beleg, row.text, "", account, tax, row.quelle, mwst=c.code)]
    return [Row(row.datum, row.beleg, row.text, row.soll, "", net, row.quelle, mwst=c.code),
            Row(row.datum, row.beleg, row.text, account, "", tax, row.quelle, mwst=c.code),
            Row(row.datum, row.beleg, row.text, "", row.haben, row.betrag, row.quelle, mwst="")]


def bezug_rows(cfg: dict, row: Row, c: Code) -> list[Row]:
    """A service from abroad: the expense as invoiced (no Swiss VAT in it), plus the
    Bezugsteuer owed on it and — effective method — its deduction as Vorsteuer."""
    tax = tax_from_net(row.betrag, c.rate)
    owed = Row(row.datum, row.beleg, row.text, "", cfg["konten"]["bezugsteuer"], tax, row.quelle, mwst=c.code)
    if cfg["methode"] == "saldo":
        cost = Row(row.datum, row.beleg, row.text, row.soll, "", tax, row.quelle)      # no deduction: a cost
        return [row, cost, owed]
    vst = cfg["konten"]["vorsteuer"] if row.soll.startswith("4") else cfg["konten"]["vorsteuer_inv"]
    return [row, Row(row.datum, row.beleg, row.text, vst, "", tax, row.quelle, mwst=c.code), owed]


# ---------- periods ----------

def resolve(periode: str) -> tuple[date, date, str]:
    """'2026-Q1', '2026-S2', '2026' → (start, end, normalised label)."""
    p = (periode or "").strip().upper()
    m = re.fullmatch(r"(\d{4})-Q([1-4])", p)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        start = date(y, 3 * q - 2, 1)
        end = date(y, 3 * q, 31 if q in (1, 4) else 30)
        return start, end, f"{y}-Q{q}"
    m = re.fullmatch(r"(\d{4})-S([12])", p)
    if m:
        y, s = int(m.group(1)), int(m.group(2))
        return (date(y, 1, 1), date(y, 6, 30), f"{y}-S1") if s == 1 else (date(y, 7, 1), date(y, 12, 31), f"{y}-S2")
    if re.fullmatch(r"\d{4}", p):
        y = int(p)
        return date(y, 1, 1), date(y, 12, 31), str(y)
    raise BookError(f"Periode '{periode}': erwartet z.B. 2026-Q1, 2026-S2 oder 2026")


def periods(book: Book, year: int) -> list[str]:
    cfg = config(book)
    return [f"{year}-S{i}" for i in (1, 2)] if cfg["periode"] == "semester" else [f"{year}-Q{i}" for i in (1, 2, 3, 4)]


# ---------- the Abrechnung ----------

def report(book: Book, periode: str) -> dict:
    """The MWST-Abrechnung for a period, by ESTV Ziffer, plus the rows behind each code."""
    start, end, label = resolve(periode)
    cfg = config(book)
    k = cfg["konten"]
    tax_accounts = {k["umsatzsteuer"], k["vorsteuer"], k["vorsteuer_inv"], k["bezugsteuer"]}
    base: dict[str, Decimal] = defaultdict(lambda: ZERO)
    tax: dict[str, Decimal] = defaultdict(lambda: ZERO)
    bezug_vst = {"400": ZERO, "405": ZERO}
    belege: dict[str, set] = defaultdict(set)
    for r in book.rows:
        if not (start <= r.datum <= end) or not r.mwst:
            continue
        c = CODES.get(r.mwst)
        if c is None:
            continue
        belege[c.code].add(r.beleg)
        if c.kind == "bezug":
            if r.soll and r.haben:                         # the expense: the base of the Bezugsteuer
                base[c.code] += r.betrag
            elif r.haben == k["bezugsteuer"]:
                tax[c.code] += r.betrag                    # owed
            elif r.soll in (k["vorsteuer"], k["vorsteuer_inv"]):
                bezug_vst["400" if r.soll == k["vorsteuer"] else "405"] += r.betrag
            continue
        revenue = c.kind in ("umsatz", "befreit", "ausgenommen")
        if r.soll and r.haben:
            # Two-sided row (Saldo method): the side on a P&L account is the revenue/cost side.
            pl = lambda nr: nr in book.accounts and book.accounts[nr].is_pl  # noqa: E731
            account = r.haben if pl(r.haben) else r.soll
            sign = (1 if account == r.haben else -1) if revenue else (1 if account == r.soll else -1)
        else:
            account = r.soll or r.haben
            # Revenue and Umsatzsteuer count on the Haben side, costs and Vorsteuer on the Soll side.
            sign = (1 if r.haben else -1) if revenue else (1 if r.soll else -1)
        if account in tax_accounts:
            tax[c.code] += sign * r.betrag
        else:
            base[c.code] += sign * r.betrag

    ziffern: dict[str, Decimal] = {}
    bezug_exact = {key: base[f"B{key}"] * RATES[key] / Decimal(100) for key in BEZUG_ZIFFERN}
    if cfg["methode"] == "saldo":
        rate = cfg["saldosteuersatz"]
        gross = sum((base[k] for k in ("U81", "U26", "U38")), ZERO)
        exempt = base["U0"] + base["UA"]
        ziffern = {"200": gross + exempt, "220": base["U0"], "230": base["UA"],
                   "289": exempt, "299": gross, "322": gross,
                   "322_steuer": tax_from_net(gross, rate) if rate else ZERO}
        ziffern["399"] = ziffern["322_steuer"] + sum(bezug_exact.values(), ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
        if tax["B81"] or tax["B26"]:          # only then: booked Abrechnungen keep their fingerprint
            ziffern["399_gebucht"] = ziffern["322_steuer"] + tax["B81"] + tax["B26"]
        ziffern["479"] = ZERO
    else:
        taxable = {k: base[f"U{k}"] for k in RATES}
        exempt = base["U0"] + base["UA"]
        # The ESTV computes the tax from the turnover (eCH-0217 Kap. 6.2): rate × turnover, unrounded
        # until the end. Tax booked per document may differ by Rappen; that difference is booked with
        # the Abrechnung.
        exact = {k: taxable[k] * RATES[k] / Decimal(100) for k in RATES}
        ziffern = {"200": sum(taxable.values(), ZERO) + exempt, "220": base["U0"], "230": base["UA"],
                   "289": exempt, "299": sum(taxable.values(), ZERO),
                   "303": taxable["81"], "303_steuer": exact["81"].quantize(CENT, rounding=ROUND_HALF_UP),
                   "313": taxable["26"], "313_steuer": exact["26"].quantize(CENT, rounding=ROUND_HALF_UP),
                   "343": taxable["38"], "343_steuer": exact["38"].quantize(CENT, rounding=ROUND_HALF_UP)}
        ziffern["399"] = (sum(exact.values(), ZERO) + sum(bezug_exact.values(), ZERO)).quantize(CENT, rounding=ROUND_HALF_UP)
        ziffern["399_gebucht"] = tax["U81"] + tax["U26"] + tax["U38"] + tax["B81"] + tax["B26"]
        ziffern["400"] = tax["V81"] + tax["V26"] + tax["V38"] + bezug_vst["400"]
        ziffern["405"] = tax["I81"] + tax["I26"] + tax["I38"] + bezug_vst["405"]
        ziffern["479"] = ziffern["400"] + ziffern["405"]
    for key, nr in BEZUG_ZIFFERN.items():
        if base[f"B{key}"]:
            ziffern[nr] = base[f"B{key}"]
            ziffern[f"{nr}_steuer"] = bezug_exact[key].quantize(CENT, rounding=ROUND_HALF_UP)
    ziffern["differenz"] = ziffern["399"] - ziffern.get("399_gebucht", ziffern["399"])
    saldo = ziffern["399"] - ziffern["479"]
    ziffern["500"] = saldo if saldo > 0 else ZERO
    ziffern["510"] = -saldo if saldo < 0 else ZERO
    saved = load(book, label)
    return {"periode": label, "von": start, "bis": end, "methode": cfg["methode"],
            "bezug_gebucht": tax["B81"] + tax["B26"],
            "saldosteuersatz": cfg["saldosteuersatz"], "ziffern": ziffern,
            "codes": {k: {"code": k, "label": CODES[k].label, "entgelt": base[k], "steuer": tax[k],
                          "belege": sorted(belege[k])} for k in CODES if base[k] or tax[k]},
            "zahllast": saldo, "gebucht": saved is not None,
            "veraendert": bool(saved and saved.get("fingerprint") != fingerprint(ziffern))}


def fingerprint(ziffern: dict) -> str:
    blob = json.dumps({k: f"{Decimal(str(v)):.2f}" for k, v in sorted(ziffern.items())})
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def path(book: Book, label: str) -> Path:
    return book.root / "mwst" / f"{label}.yaml"


def load(book: Book, label: str) -> dict | None:
    p = path(book, label)
    return read_yaml(p) if p.exists() else None


def booking_rows(book: Book, label: str, saved: dict) -> list[Row]:
    """Rows a booked Abrechnung owns: clear the MWST accounts into the settlement account."""
    cfg = config(book)
    k = cfg["konten"]
    _, end, _ = resolve(label)
    z = {key: Decimal(str(v)) for key, v in saved["ziffern"].items()}
    beleg, quelle = f"MWST-{label}", f"mwst:{label}"
    rows = []

    def add(soll, haben, amount, text):
        amount = amount.quantize(CENT)
        if not amount:
            return
        if amount < 0:
            soll, haben, amount = haben, soll, -amount
        rows.append(Row(end, beleg, f"MWST-Abrechnung {label}: {text}", soll, haben, amount, quelle))

    owed_bezug = Decimal(str(saved.get("bezug_gebucht") or 0))
    if saved.get("methode") == "saldo":
        add(k["saldosteuer"], k["abrechnung"], z["322_steuer"] if "322_steuer" in z else z["399"],
            f"Saldosteuer {saved.get('saldosteuersatz')} %")
        add(k["bezugsteuer"], k["abrechnung"], owed_bezug, "Bezugsteuer")
        if "399_gebucht" in z:
            add(k.get("differenz", "3800"), k["abrechnung"], z["399"] - z["399_gebucht"], "Rundungsdifferenz Steuer")
    else:
        booked = z.get("399_gebucht", z["399"]) - owed_bezug
        add(k["umsatzsteuer"], k["abrechnung"], booked, "Umsatzsteuer")
        add(k["bezugsteuer"], k["abrechnung"], owed_bezug, "Bezugsteuer")
        add(k.get("differenz", "3800"), k["abrechnung"], z["399"] - z.get("399_gebucht", z["399"]),
            "Rundungsdifferenz Steuer")
        add(k["abrechnung"], k["vorsteuer"], z.get("400", ZERO), "Vorsteuer Material/DL")
        add(k["abrechnung"], k["vorsteuer_inv"], z.get("405", ZERO), "Vorsteuer Investitionen")
    return rows


def book_report(book: Book, periode: str) -> tuple[dict, list[Path]]:
    from .journal import ensure_open, post
    rep = report(book, periode)
    label = rep["periode"]
    if rep["gebucht"]:
        raise BookError(f"MWST-Abrechnung {label} ist bereits gebucht")
    if rep["methode"] == "keine":
        raise BookError("Dieses Buch ist nicht MWST-pflichtig")
    cfg = config(book)
    for role, nr in cfg["konten"].items():
        if role == "saldosteuer" and rep["methode"] != "saldo":
            continue
        if role == "differenz" and not rep["ziffern"].get("differenz"):
            continue
        book.account(nr)
    ensure_open(book, rep["bis"])
    saved = {"periode": label, "von": rep["von"].isoformat(), "bis": rep["bis"].isoformat(),
             "methode": rep["methode"], "saldosteuersatz": rep["saldosteuersatz"],
             "ziffern": rep["ziffern"], "fingerprint": fingerprint(rep["ziffern"]),
             "gebucht_am": date.today().isoformat()}
    if rep["bezug_gebucht"]:
        saved["bezug_gebucht"] = rep["bezug_gebucht"]
    rows = booking_rows(book, label, saved)
    touched = post(book, rows) if rows else []
    write_yaml(path(book, label), saved)
    return {**rep, "gebucht": True}, touched + [path(book, label)]


# ---------- eMWST: eCH-0217 v2.0.0 for the ESTV portal ----------

ECH_NS = "http://www.ech.ch/xmlns/eCH-0217/2"
ECH0058_NS = "http://www.ech.ch/xmlns/eCH-0058/5"


def _uid(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 9:
        raise BookError("Für die eMWST-Datei braucht es die UID/MWST-Nr. (CHE-123.456.789) in den Einstellungen")
    return "CHE" + digits


def ech0217(book: Book, periode: str, korrektur: bool = False) -> bytes:
    """The Abrechnung as eCH-0217 v2.0.0 XML (upload in ESTV SuisseTax, «MWST abrechnen»)."""
    from datetime import datetime
    from xml.sax.saxutils import escape
    from . import __version__
    rep = report(book, periode)
    cfg = config(book)
    if rep["methode"] == "keine":
        raise BookError("Dieses Buch ist nicht MWST-pflichtig")
    z = rep["ziffern"]
    a = lambda v: f"{Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"  # noqa: E731
    e = lambda tag, value: f"<eCH-0217:{tag}>{value}</eCH-0217:{tag}>"  # noqa: E731
    general = (
        "<eCH-0217:generalInformation>"
        + e("uid", _uid(book.settings.get("uid") or ""))
        + e("organisationName", escape(book.settings.firma)[:255])
        + e("generationTime", datetime.now().replace(microsecond=0).isoformat())
        + e("reportingPeriodFrom", rep["von"].isoformat())
        + e("reportingPeriodTill", rep["bis"].isoformat())
        + e("typeOfSubmission", "2" if korrektur else "1")
        + e("formOfReporting", "2" if cfg["abrechnungsart"] == "vereinnahmt" else "1")
        + e("businessReferenceId", f"batzen-{rep['periode']}{'-K' if korrektur else ''}")
        + "<eCH-0217:sendingApplication>"
        + f"<eCH-0058:manufacturer>batzen</eCH-0058:manufacturer><eCH-0058:product>batzen</eCH-0058:product>"
        + f"<eCH-0058:productVersion>{__version__}</eCH-0058:productVersion>"
        + "</eCH-0217:sendingApplication></eCH-0217:generalInformation>")
    turnover = "<eCH-0217:turnoverComputation>" + e("totalConsideration", a(z["200"]))
    if z.get("220"):
        turnover += e("suppliesToForeignCountries", a(z["220"]))
    if z.get("230"):
        turnover += e("suppliesExemptFromTax", a(z["230"]))
    turnover += "</eCH-0217:turnoverComputation>"

    def acquisition():
        out = ""
        for key, nr in BEZUG_ZIFFERN.items():
            if z.get(nr):
                out += (f"<eCH-0217:acquisitionTax>{e('taxRate', f'{RATES[key]:.2f}')}"
                        f"{e('turnover', a(z[nr]))}</eCH-0217:acquisitionTax>")
        return out

    bezug_tax = sum((RATES[key] * Decimal(str(z[nr])) / 100 for key, nr in BEZUG_ZIFFERN.items() if z.get(nr)), ZERO)

    def supply(rate, amount, tag="turnoverTaxRateType"):
        return (f"<eCH-0217:suppliesPerTaxRate>{e('taxRate', f'{Decimal(rate):.2f}')}"
                f"{e('turnover', a(amount))}</eCH-0217:suppliesPerTaxRate>")

    if rep["methode"] == "effektiv":
        method = "<eCH-0217:effectiveReportingMethod>" + e("grossOrNet", "1")
        for nr, key in (("303", "81"), ("313", "26"), ("343", "38")):
            if z.get(nr):
                method += supply(RATES[key], z[nr])
        method += acquisition()
        if z.get("400"):
            method += e("inputTaxMaterialAndServices", a(z["400"]))
        if z.get("405"):
            method += e("inputTaxInvestments", a(z["405"]))
        method += "</eCH-0217:effectiveReportingMethod>"
        payable = sum((RATES[k] * Decimal(str(z[n])) / 100 for n, k in (("303", "81"), ("313", "26"), ("343", "38"))
                       if z.get(n)), ZERO) - Decimal(str(z.get("400") or 0)) - Decimal(str(z.get("405") or 0)) + bezug_tax
    else:
        rate = cfg["saldosteuersatz"]
        if rep["von"].year >= 2025:
            if not re.fullmatch(r"\S{5}", cfg["taetigkeit"]):
                raise BookError("Für die eMWST-Datei (Saldosteuersatz ab 2025) die 5-stellige ESTV-Tätigkeits-ID "
                                "in den Einstellungen erfassen (steht in der Bewilligung des Saldosteuersatzes)")
            method = ("<eCH-0217:simpleTaxRateMethod><eCH-0217:suppliesPerTaxRate>" + e("activityID", cfg["taetigkeit"])
                      + e("taxRate", f"{rate:.2f}") + e("turnover", a(z["322"]))
                      + "</eCH-0217:suppliesPerTaxRate>" + acquisition() + "</eCH-0217:simpleTaxRateMethod>")
        else:
            method = ("<eCH-0217:netTaxRateMethod>" + supply(rate, z["322"]) + acquisition()
                      + "</eCH-0217:netTaxRateMethod>")
        payable = rate * Decimal(str(z["322"])) / 100 + bezug_tax
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<eCH-0217:VATDeclaration xmlns:eCH-0217="{ECH_NS}" xmlns:eCH-0058="{ECH0058_NS}">'
           + general + turnover + method + e("payableTax", a(payable)) + "</eCH-0217:VATDeclaration>")
    return xml.encode("utf-8")

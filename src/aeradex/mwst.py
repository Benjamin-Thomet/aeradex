"""Mehrwertsteuer: codes, automatic tax split, MWST-Abrechnung (ESTV).

Settings in aeradex.yaml:

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

Abrechnungsart (`abrechnungsart: vereinbart | vereinnahmt`, Art. 39 MWSTG): the books always
record invoices and supplier bills with their tax on the document date. With vereinnahmte
Entgelte the Abrechnung counts an invoice's (or bill's) coded rows only when money comes in
(goes out): pro rata of each payment, on its date (`cash_rows`). The tax on what is still open
stays on the MWST accounts.

Umsatzabstimmung (Art. 72 MWSTG, MWST-Info 15): once a year the turnover in the accounts is
reconciled with what the four (two) Abrechnungen declared; a difference is corrected with a
Jahresabstimmung (Berichtigungsabrechnung); the ESTV finalises the year after 240 days. With vereinnahmte Entgelte the books show
more turnover than was declared (open Debitoren); `abstimmung` bridges that, and
`book_abgrenzung` moves the tax on open Debitoren/Kreditoren from 2200/1170/1171 to their own
balance sheet accounts per 31.12. (reversed on 1.1.), so the balance sheet shows what is owed now.

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
                  "abrechnung": "2201", "saldosteuer": "3809", "differenz": "3800",
                  "umsatzsteuer_offen": "2209", "vorsteuer_offen": "1172"}
ABGRENZUNG_KONTEN = {"umsatzsteuer_offen": ("MWST auf offenen Debitoren (vereinnahmte Entgelte)", "passiv"),
                     "vorsteuer_offen": ("Vorsteuer auf offenen Kreditoren (vereinnahmte Entgelte)", "aktiv")}
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

def _deferral(book: Book) -> tuple[list[Row], list[Row], list[Row]]:
    """Split the journal for vereinnahmte Entgelte: (rows that count on their date, coded rows of
    invoices and bills that wait for payment, those rows recognised pro rata on payment dates).

    An invoice is recognised by its settlements (payments and credit notes; a credit note's own
    coded rows count on its date), a bill by its payments. Shares are rounded cumulatively, so a
    fully settled document is recognised exactly as booked."""
    from . import invoices as inv
    from . import kreditoren as kred
    plain, deferred = [], defaultdict(list)
    for r in book.rows:
        kind = r.quelle.partition(":")[0]
        if r.mwst and kind in ("rechnung", "kreditor"):
            deferred[r.quelle].append(r)
        else:
            plain.append(r)
    if not deferred:
        return plain, [], []
    all_inv, settled = inv.invoices(book), inv.settlements(book)
    all_bills, paid = kred.bills(book), kred.payments(book)
    recognised = []
    for quelle, rows in deferred.items():
        kind, _, nr = quelle.partition(":")
        if kind == "rechnung":
            meta = all_inv.get(nr)
            events, total, foreign = settled.get(nr, []), meta and Decimal(str(meta.get("total") or 0)), meta and inv.is_foreign(meta)
        else:
            meta = all_bills.get(nr)
            events, total, foreign = paid.get(nr, []), meta and Decimal(str(meta.get("betrag") or 0)), meta and kred.is_foreign(meta)
        if not meta or not total:
            continue
        done = [ZERO] * len(rows)
        cum = ZERO
        for ev in sorted(events, key=lambda e: (e.datum, e.beleg)):
            cum += (ev.fw or ZERO) if foreign else ev.betrag
            share = min(cum / total, Decimal(1))
            for i, r in enumerate(rows):
                target = (r.betrag * share).quantize(CENT, rounding=ROUND_HALF_UP)
                if target != done[i]:
                    recognised.append(Row(ev.datum, r.beleg, r.text, r.soll, r.haben, target - done[i], r.quelle,
                                          mwst=r.mwst))
                    done[i] = target
    return plain, [r for g in deferred.values() for r in g], recognised


def cash_rows(book: Book) -> list[Row]:
    """The journal as the ESTV sees it with vereinnahmte Entgelte."""
    plain, _, recognised = _deferral(book)
    return plain + recognised


def report_rows(book: Book) -> list[Row]:
    return cash_rows(book) if config(book)["abrechnungsart"] == "vereinnahmt" else book.rows


def _contributions(book: Book, rows: list[Row], start: date, end: date) -> list[dict]:
    """What each coded row of start..end adds to the Abrechnung: to the Entgelt (`base`), to the
    tax (`tax`), or as Vorsteuer on Bezugsteuer (`vst`: "400"/"405"). Sign: revenue/Umsatzsteuer
    and costs/Vorsteuer positive. The Abrechnung is the sum of these; the MWST report lists them."""
    k = config(book)["konten"]
    tax_accounts = {k["umsatzsteuer"], k["vorsteuer"], k["vorsteuer_inv"], k["bezugsteuer"]}
    pl = lambda nr: nr in book.accounts and book.accounts[nr].is_pl  # noqa: E731
    out = []
    for r in rows:
        if not (start <= r.datum <= end) or not r.mwst:
            continue
        c = CODES.get(r.mwst)
        if c is None:
            continue
        item = {"row": r, "code": c.code, "base": ZERO, "tax": ZERO, "vst": None, "konto": r.soll or r.haben}
        if c.kind == "bezug":
            if r.soll and r.haben:                         # the expense: the base of the Bezugsteuer
                item.update(base=r.betrag, konto=r.soll)
            elif r.haben == k["bezugsteuer"]:
                item.update(tax=r.betrag, konto=r.haben)   # owed
            elif r.soll in (k["vorsteuer"], k["vorsteuer_inv"]):
                item.update(vst="400" if r.soll == k["vorsteuer"] else "405", tax=r.betrag, konto=r.soll)
            else:
                continue
            out.append(item)
            continue
        revenue = c.kind in ("umsatz", "befreit", "ausgenommen")
        if r.soll and r.haben:
            # Two-sided row (Saldo method): the side on a P&L account is the revenue/cost side.
            account = r.haben if pl(r.haben) else r.soll
            sign = (1 if account == r.haben else -1) if revenue else (1 if account == r.soll else -1)
        else:
            account = r.soll or r.haben
            # Revenue and Umsatzsteuer count on the Haben side, costs and Vorsteuer on the Soll side.
            sign = (1 if r.haben else -1) if revenue else (1 if r.soll else -1)
        item["konto"] = account
        item["tax" if account in tax_accounts else "base"] = sign * r.betrag
        out.append(item)
    return out


def _aggregate(book: Book, rows: list[Row], start: date, end: date) -> dict:
    """Entgelt and tax per code for rows dated start..end (see `_contributions`)."""
    base: dict[str, Decimal] = defaultdict(lambda: ZERO)
    tax: dict[str, Decimal] = defaultdict(lambda: ZERO)
    bezug_vst = {"400": ZERO, "405": ZERO}
    belege: dict[str, set] = defaultdict(set)
    for item in _contributions(book, rows, start, end):
        belege[item["code"]].add(item["row"].beleg)
        if item["vst"]:
            bezug_vst[item["vst"]] += item["tax"]
        else:
            base[item["code"]] += item["base"]
            tax[item["code"]] += item["tax"]
    return {"base": base, "tax": tax, "bezug_vst": bezug_vst, "belege": belege}


def _ziffern(cfg: dict, agg: dict) -> dict[str, Decimal]:
    base, tax, bezug_vst = agg["base"], agg["tax"], agg["bezug_vst"]
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
    return ziffern


def report(book: Book, periode: str) -> dict:
    """The MWST-Abrechnung for a period, by ESTV Ziffer, plus the rows behind each code."""
    start, end, label = resolve(periode)
    cfg = config(book)
    agg = _aggregate(book, report_rows(book), start, end)
    base, tax, belege = agg["base"], agg["tax"], agg["belege"]
    ziffern = _ziffern(cfg, agg)
    saldo = ziffern["399"] - ziffern["479"]
    saved = load(book, label)
    return {"periode": label, "von": start, "bis": end, "methode": cfg["methode"],
            "abrechnungsart": cfg["abrechnungsart"],
            "bezug_gebucht": tax["B81"] + tax["B26"],
            "saldosteuersatz": cfg["saldosteuersatz"], "ziffern": ziffern,
            "codes": {k: {"code": k, "label": CODES[k].label, "entgelt": base[k], "steuer": tax[k],
                          "belege": sorted(belege[k])} for k in CODES if base[k] or tax[k]},
            "zahllast": saldo, "gebucht": saved is not None,
            "veraendert": bool(saved and saved.get("fingerprint") != fingerprint(ziffern))}


ZIFFER_LABEL = {
    "220": "Steuerbefreite Leistungen (Exporte u.a.)", "230": "Von der Steuer ausgenommene Leistungen",
    "303": "Leistungen zum Normalsatz 8.1 %", "313": "Leistungen zum reduzierten Satz 2.6 %",
    "343": "Leistungen zum Beherbergungssatz 3.8 %", "322": "Leistungen zum Saldosteuersatz",
    "382": "Bezugsteuer 8.1 %", "383": "Bezugsteuer 2.6 %",
    "400": "Vorsteuer auf Material- und Dienstleistungsaufwand",
    "405": "Vorsteuer auf Investitionen und übrigem Betriebsaufwand",
}


def _ziffer_of(methode: str, item: dict) -> str:
    if item["vst"]:
        return item["vst"]
    code = item["code"]
    if code in ("U0", "UA"):
        return "220" if code == "U0" else "230"
    if code.startswith("U"):
        return "322" if methode == "saldo" else {"81": "303", "26": "313", "38": "343"}[code[1:]]
    if code.startswith("B"):
        return BEZUG_ZIFFERN[code[1:]]
    return "405" if code.startswith("I") else "400"


def herkunft(book: Book, periode: str) -> dict:
    """The MWST report: every journal row behind the Abrechnung, grouped by ESTV Ziffer, each group
    reconciled with the Ziffer it explains. Built from the same contributions as `report`."""
    start, end, label = resolve(periode)
    cfg = config(book)
    rep = report(book, periode)
    z = rep["ziffern"]
    cash = cfg["abrechnungsart"] == "vereinnahmt"
    recognised = {id(r) for r in _deferral(book)[2]} if cash else set()
    rows = cash_rows(book) if cash else book.rows
    groups: dict[str, dict] = {}
    for item in _contributions(book, rows, start, end):
        nr = _ziffer_of(cfg["methode"], item)
        g = groups.setdefault(nr, {"ziffer": nr, "label": ZIFFER_LABEL.get(nr, nr), "zeilen": [],
                                   "entgelt": ZERO, "steuer": ZERO})
        r = item["row"]
        g["zeilen"].append({"datum": r.datum, "beleg": r.beleg, "text": r.text, "konto": item["konto"],
                            "code": item["code"], "entgelt": item["base"], "steuer": item["tax"], "quelle": r.quelle,
                            "hinweis": f"Anteil bezahlt am {r.datum:%d.%m.%Y}" if id(r) in recognised else ""})
        g["entgelt"] += item["base"]
        g["steuer"] += item["tax"]
    for nr, g in groups.items():
        g["zeilen"].sort(key=lambda x: (x["datum"], x["beleg"]))
        # What the Ziffer shows, to compare with the sum of its rows.
        if nr in ("400", "405"):
            g["ziffer_steuer"], g["ziffer_entgelt"] = z.get(nr, ZERO), None
        elif nr in ("220", "230"):
            g["ziffer_entgelt"], g["ziffer_steuer"] = z.get(nr, ZERO), None
        elif nr == "322":
            g["ziffer_entgelt"], g["ziffer_steuer"] = z.get("322", ZERO), z.get("322_steuer", ZERO)
        else:
            g["ziffer_entgelt"], g["ziffer_steuer"] = z.get(nr, ZERO), z.get(f"{nr}_steuer", ZERO)
        g["differenz_entgelt"] = (g["ziffer_entgelt"] - g["entgelt"]) if g["ziffer_entgelt"] is not None else ZERO
        # Umsatz-/Bezugsteuer: the ESTV computes rate × Entgelt; booked tax may differ by Rappen.
        g["differenz_steuer"] = (g["ziffer_steuer"] - g["steuer"]) if g["ziffer_steuer"] is not None else ZERO
    if cfg["methode"] == "saldo":
        for g in groups.values():
            if g["ziffer"] == "322":
                g["hinweis"] = (f"Steuer = Entgelt × Saldosteuersatz {cfg['saldosteuersatz']} %; auf den Belegen "
                                "wird keine Steuer verbucht.")
    order = ["303", "313", "343", "322", "220", "230", "382", "383", "400", "405"]
    out = sorted(groups.values(), key=lambda g: order.index(g["ziffer"]) if g["ziffer"] in order else 99)
    return {"periode": label, "von": start, "bis": end, "methode": cfg["methode"], "abrechnungsart": cfg["abrechnungsart"],
            "ziffern": z, "zahllast": rep["zahllast"], "gruppen": out,
            "zeilen": sum(len(g["zeilen"]) for g in out)}


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
        if role == "differenz" and not rep["ziffern"].get("differenz") or role in ABGRENZUNG_KONTEN:
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


# ---------- vereinnahmte Entgelte: open items, Abgrenzung ----------

EPOCH = date(1900, 1, 1)


def open_items(book: Book, stichtag: date) -> dict:
    """Tax on invoices and bills booked up to `stichtag` but not yet paid — with vereinnahmte
    Entgelte not yet owed (deductible), so it is still on the MWST accounts."""
    k = config(book)["konten"]
    _, deferred, recognised = _deferral(book)
    a, b = _aggregate(book, deferred, EPOCH, stichtag), _aggregate(book, recognised, EPOCH, stichtag)
    agg = {"base": defaultdict(lambda: ZERO), "tax": defaultdict(lambda: ZERO),
           "bezug_vst": {z: a["bezug_vst"][z] - b["bezug_vst"][z] for z in a["bezug_vst"]}, "belege": defaultdict(set)}
    for key in set(a["base"]) | set(a["tax"]):
        agg["base"][key] = a["base"][key] - b["base"][key]
        agg["tax"][key] = a["tax"][key] - b["tax"][key]
    t = agg["tax"]
    accounts: dict[str, Decimal] = defaultdict(lambda: ZERO)     # signed like the ledger: Aktiven +, Passiven −
    accounts[k["umsatzsteuer"]] -= sum((t[c] for c in CODES if CODES[c].kind == "umsatz"), ZERO)
    accounts[k["bezugsteuer"]] -= sum((t[c] for c in CODES if CODES[c].kind == "bezug"), ZERO)
    accounts[k["vorsteuer"]] += sum((t[c] for c in CODES if CODES[c].kind == "vorsteuer"), ZERO) + agg["bezug_vst"]["400"]
    accounts[k["vorsteuer_inv"]] += sum((t[c] for c in CODES if CODES[c].kind == "investition"), ZERO) + agg["bezug_vst"]["405"]
    # Which documents are open, for the list in the Abstimmung.
    docs: dict[str, Decimal] = defaultdict(lambda: ZERO)
    tax_accounts = {k["umsatzsteuer"], k["vorsteuer"], k["vorsteuer_inv"], k["bezugsteuer"]}
    for rows, sign in ((deferred, 1), (recognised, -1)):
        for r in rows:
            if r.datum <= stichtag and (r.soll in tax_accounts and not r.haben or r.haben in tax_accounts and not r.soll):
                docs[r.quelle] += sign * r.betrag
    return {"stichtag": stichtag, "aggregat": agg, "konten": {nr: v for nr, v in accounts.items() if v},
            "umsatzsteuer": -accounts[k["umsatzsteuer"]] - (accounts[k["bezugsteuer"]] if k["bezugsteuer"] != k["umsatzsteuer"] else ZERO),
            "vorsteuer": accounts[k["vorsteuer"]] + accounts[k["vorsteuer_inv"]],
            "belege": [{"quelle": q, "art": "Debitor" if q.startswith("rechnung:") else "Kreditor",
                        "nummer": q.partition(":")[2], "steuer": v} for q, v in sorted(docs.items()) if v]}


def abgrenzung_path(book: Book, year: int) -> Path:
    return book.root / "mwst" / "abgrenzung" / f"{year}.yaml"


def load_abgrenzung(book: Book, year: int) -> dict | None:
    p = abgrenzung_path(book, year)
    return read_yaml(p) if p.exists() else None


def abgrenzung_rows(book: Book, year: int, saved: dict) -> list[Row]:
    """Rows a booked Abgrenzung owns: per 31.12. the tax on open items onto its own accounts,
    reversed on 1.1. of the next year (when payment makes it owed / deductible)."""
    k = config(book)["konten"]
    beleg, quelle = f"MWST-ABGR-{year}", f"mwst:abgrenzung-{year}"
    end, start = date(year, 12, 31), date(year + 1, 1, 1)
    rows = []

    def add(d, ref, soll, haben, amount, text):
        amount = Decimal(str(amount)).quantize(CENT)
        if not amount:
            return
        if amount < 0:
            soll, haben, amount = haben, soll, -amount
        rows.append(Row(d, ref, text, soll, haben, amount, quelle))

    for nr, value in sorted((saved.get("konten") or {}).items()):
        value = Decimal(str(value))
        target = k["umsatzsteuer_offen"] if book.accounts.get(nr) and book.accounts[nr].klasse == "passiv" else k["vorsteuer_offen"]
        # value is the ledger sign of the open tax on nr: clear nr, move it to target.
        add(end, beleg, target, nr, value, f"MWST vereinnahmt: Steuer auf offenen Posten {year} auf {target}")
        add(start, f"{beleg}-R", nr, target, value, f"MWST vereinnahmt: Rückbuchung Abgrenzung {year}")
    return rows


def _ensure_abgrenzung_accounts(book: Book) -> list[Path]:
    from .book import Account
    from .statements import default_group
    k = config(book)["konten"]
    added = False
    for role, (name, klasse) in ABGRENZUNG_KONTEN.items():
        if k[role] not in book.accounts:
            book.accounts[k[role]] = Account(nr=k[role], name=name, klasse=klasse, gruppe=default_group(k[role], klasse))
            added = True
    if added:
        book.save_accounts()
        return [book.root / "kontenplan.yaml"]
    return []


def book_abgrenzung(book: Book, year: int, neu: bool = False) -> tuple[dict, list[Path]]:
    """Book the year-end Abgrenzung (vereinnahmte Entgelte only). `neu` replaces an earlier one."""
    from .journal import ensure_open, post
    cfg = config(book)
    if cfg["methode"] == "keine":
        raise BookError("Dieses Buch ist nicht MWST-pflichtig")
    if cfg["abrechnungsart"] != "vereinnahmt":
        raise BookError("Die Abgrenzung offener Posten braucht es nur bei der Abrechnung nach vereinnahmten Entgelten")
    end = date(year, 12, 31)
    ensure_open(book, end)
    ensure_open(book, date(year + 1, 1, 1))
    old = load_abgrenzung(book, year)
    if old and not neu:
        raise BookError(f"Die MWST-Abgrenzung {year} ist bereits gebucht (neu berechnen: --neu)")
    items = open_items(book, end)
    saved = {"jahr": year, "stichtag": end.isoformat(),
             "konten": {nr: v for nr, v in sorted(items["konten"].items())},
             "umsatzsteuer": items["umsatzsteuer"], "vorsteuer": items["vorsteuer"],
             "gebucht_am": date.today().isoformat()}
    touched = _ensure_abgrenzung_accounts(book)
    if old:
        touched += book.remove_rows(lambda r: r.quelle == f"mwst:abgrenzung-{year}")
        book.reload()
    rows = abgrenzung_rows(book, year, saved)
    if rows:
        touched += post(book, rows)
    path = abgrenzung_path(book, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, saved)
    return saved, touched + [path]


# ---------- Umsatzabstimmung (year end) ----------

ABSTIMMUNG_ZIFFERN = {
    "effektiv": [("200", "Total der Entgelte"), ("220", "Steuerbefreite Leistungen"),
                 ("230", "Von der Steuer ausgenommene Leistungen"), ("299", "Steuerbares Gesamtentgelt"),
                 ("303", "Leistungen 8.1 %"), ("303_steuer", "Steuer 8.1 %"),
                 ("313", "Leistungen 2.6 %"), ("313_steuer", "Steuer 2.6 %"),
                 ("343", "Beherbergung 3.8 %"), ("343_steuer", "Steuer 3.8 %"),
                 ("382", "Bezugsteuer 8.1 % (Entgelt)"), ("383", "Bezugsteuer 2.6 % (Entgelt)"),
                 ("399", "Total geschuldete Steuer"), ("400", "Vorsteuer Material/DL"),
                 ("405", "Vorsteuer Investitionen/übriger Aufwand"), ("479", "Total Vorsteuer")],
    "saldo": [("200", "Total der Entgelte"), ("220", "Steuerbefreite Leistungen"),
              ("230", "Von der Steuer ausgenommene Leistungen"), ("299", "Steuerbares Gesamtentgelt"),
              ("322", "Leistungen zum Saldosteuersatz"), ("322_steuer", "Saldosteuer"),
              ("382", "Bezugsteuer 8.1 % (Entgelt)"), ("383", "Bezugsteuer 2.6 % (Entgelt)"),
              ("399", "Total geschuldete Steuer")],
}
REVENUE_KINDS = ("umsatz", "befreit", "ausgenommen")


def _ertrag(book: Book, year: int) -> dict:
    """Revenue accounts of the Erfolgsrechnung, split into what carries a turnover code and what not."""
    k = config(book)["konten"]
    skip = {k["umsatzsteuer"], k["vorsteuer"], k["vorsteuer_inv"], k["bezugsteuer"]}
    per: dict[str, dict] = {}
    for r in book.rows:
        if r.datum.year != year or r.quelle.startswith("mwst:"):
            continue
        coded = r.mwst in CODES and CODES[r.mwst].kind in REVENUE_KINDS
        for nr, sign in ((r.haben, 1), (r.soll, -1)):
            acct = book.accounts.get(nr)
            if not nr or nr in skip or acct is None or acct.klasse != "ertrag":
                continue
            e = per.setdefault(nr, {"konto": nr, "name": acct.name, "total": ZERO, "mit_code": ZERO, "ohne_code": ZERO})
            e["total"] += sign * r.betrag
            e["mit_code" if coded else "ohne_code"] += sign * r.betrag
    konten = [per[nr] for nr in sorted(per)]
    return {"konten": konten, "total": sum((e["total"] for e in konten), ZERO),
            "mit_code": sum((e["mit_code"] for e in konten), ZERO),
            "ohne_code": sum((e["ohne_code"] for e in konten), ZERO)}


def abstimmung(book: Book, year: int) -> dict:
    """Umsatz- und Steuerabstimmung for a business year: turnover and tax in the accounts against
    the booked Abrechnungen, the bridge for vereinnahmte Entgelte, and the MWST account balances."""
    from datetime import timedelta
    from .ledger import BalanceEngine
    cfg = config(book)
    if cfg["methode"] == "keine":
        raise BookError("Dieses Buch ist nicht MWST-pflichtig")
    k = cfg["konten"]
    start, end = date(year, 1, 1), date(year, 12, 31)
    cash = cfg["abrechnungsart"] == "vereinnahmt"
    lines = ABSTIMMUNG_ZIFFERN["saldo" if cfg["methode"] == "saldo" else "effektiv"]

    buch = _ziffern(cfg, _aggregate(book, book.rows, start, end))           # as booked (document dates)
    soll = _ziffern(cfg, _aggregate(book, report_rows(book), start, end))   # what the year's Abrechnungen must show
    if cash:
        anfang, ende = open_items(book, start - timedelta(days=1)), open_items(book, end)
        offen_a, offen_e = _ziffern(cfg, anfang["aggregat"]), _ziffern(cfg, ende["aggregat"])
    else:
        anfang = ende = None
        offen_a = offen_e = {}

    perioden, deklariert, unbooked_rows = [], defaultdict(lambda: ZERO), []
    rows_for_report = report_rows(book)
    for label in periods(book, year):
        saved = load(book, label)
        rep = report(book, label)
        perioden.append({"periode": label, "gebucht": saved is not None, "veraendert": rep["veraendert"],
                         "zahllast": Decimal(str(saved["ziffern"].get("500", 0))) - Decimal(str(saved["ziffern"].get("510", 0)))
                         if saved else rep["zahllast"]})
        if saved:
            for key, v in saved["ziffern"].items():
                deklariert[key] += Decimal(str(v))
        else:
            s_, e_, _ = resolve(label)
            unbooked_rows.append(_aggregate(book, rows_for_report, s_, e_))
    n = len(perioden)

    zeilen, ok = [], True
    for key, label in lines:
        z = {"ziffer": key, "label": label, "buchhaltung": buch.get(key, ZERO), "soll": soll.get(key, ZERO),
             "deklariert": deklariert.get(key, ZERO)}
        if cash:
            z["offen_anfang"], z["offen_ende"] = offen_a.get(key, ZERO), offen_e.get(key, ZERO)
        z["differenz"] = z["soll"] - z["deklariert"]
        # The ESTV computes tax per Abrechnung: up to a Rappen per period is rounding, not an error.
        z["rundung"] = bool(z["differenz"]) and ("steuer" in key or key in ("399", "400", "405", "479")) \
            and abs(z["differenz"]) <= Decimal("0.01") * n
        if z["differenz"] and not z["rundung"]:
            ok = False
        if any(z[f] for f in ("buchhaltung", "soll", "deklariert")) or key in ("200", "299", "399"):
            zeilen.append(z)

    ertrag = _ertrag(book, year)
    ertrag["umsatz_andere_konten"] = buch["200"] - ertrag["mit_code"] if cfg["methode"] != "saldo" else ZERO

    # MWST accounts per 31.12. (before this year's Abgrenzung) against what they should hold.
    engine = BalanceEngine(book)
    abgr_q = f"mwst:abgrenzung-{year}"
    abgr_rows = [r for r in book.rows if r.quelle == abgr_q and r.datum == end]

    def saldo(nr: str) -> Decimal:
        value = engine.balance_at(nr, end) if nr in book.accounts else ZERO
        for r in abgr_rows:
            value -= r.betrag if r.soll == nr else ZERO
            value += r.betrag if r.haben == nr else ZERO
        return value

    expected: dict[str, Decimal] = defaultdict(lambda: ZERO)
    why: dict[str, list[str]] = defaultdict(list)
    for agg, label in zip(unbooked_rows, [p["periode"] for p in perioden if not p["gebucht"]]):
        t = agg["tax"]
        parts = {k["umsatzsteuer"]: -sum((t[c] for c in CODES if CODES[c].kind == "umsatz"), ZERO),
                 k["vorsteuer"]: sum((t[c] for c in CODES if CODES[c].kind == "vorsteuer"), ZERO) + agg["bezug_vst"]["400"],
                 k["vorsteuer_inv"]: sum((t[c] for c in CODES if CODES[c].kind == "investition"), ZERO) + agg["bezug_vst"]["405"]}
        parts[k["bezugsteuer"]] = parts.get(k["bezugsteuer"], ZERO) - sum((t[c] for c in CODES if CODES[c].kind == "bezug"), ZERO)
        for nr, v in parts.items():
            if v:
                expected[nr] += v
                why[nr].append(f"Abrechnung {label} noch nicht gebucht")
    if ende:
        for nr, v in ende["konten"].items():
            expected[nr] += v
            why[nr].append("Steuer auf offenen Posten (vereinnahmt)")
    steuerkonten = []
    for role, label in (("umsatzsteuer", "Umsatzsteuer"), ("bezugsteuer", "Bezugsteuer"),
                        ("vorsteuer", "Vorsteuer Material/DL"), ("vorsteuer_inv", "Vorsteuer Investitionen")):
        nr = k[role]
        if any(s["konto"] == nr for s in steuerkonten) or nr not in book.accounts:
            continue
        have, want = saldo(nr), expected[nr]
        steuerkonten.append({"rolle": label, "konto": nr, "name": book.accounts[nr].name, "saldo": have,
                             "erwartet": want, "differenz": have - want, "erklaerung": "; ".join(why[nr])})
        if have != want:
            ok = False
    abrechnungskonto = {"konto": k["abrechnung"], "saldo": engine.balance_at(k["abrechnung"], end)
                        if k["abrechnung"] in book.accounts else ZERO}

    saved_abgr = load_abgrenzung(book, year)
    veraltet = bool(saved_abgr and ende and {nr: f"{Decimal(str(v)):.2f}" for nr, v in (saved_abgr.get("konten") or {}).items()}
                    != {nr: f"{v:.2f}" for nr, v in ende["konten"].items()})
    # ESTV: Jahresabstimmung, https://www.estv.admin.ch/de/mwst-jahresabstimmung
    frist = end + timedelta(days=240)
    hinweise = []
    open_periods = [p["periode"] for p in perioden if not p["gebucht"]]
    if open_periods:
        hinweise.append(f"Noch nicht gebuchte Abrechnungen: {', '.join(open_periods)} — die Abstimmung ist erst "
                        "nach allen Abrechnungen des Jahres vollständig.")
    for p in perioden:
        if p["veraendert"]:
            hinweise.append(f"Abrechnung {p['periode']}: seit dem Buchen geändert — Korrekturabrechnung einreichen.")
    if any(z["differenz"] and not z["rundung"] for z in zeilen) and not open_periods:
        hinweise.append(f"Differenzen zu den deklarierten Abrechnungen: Jahresabstimmung (Berichtigungsabrechnung) bei der ESTV bis "
                        f"{frist.strftime('%d.%m.%Y')} einreichen (Finalisierung, 240 Tage nach Geschäftsjahresende, Art. 72 MWSTG).")
    if ertrag["ohne_code"]:
        hinweise.append("Ertrag ohne MWST-Code: prüfen, ob er nicht steuerbar ist (Zinsen, Dividenden, Kursgewinne, "
                        "Subventionen/Spenden Ziff. 900/910) oder ob der Code fehlt.")
    if cash and ende and ende["konten"] and not saved_abgr:
        hinweise.append(f"Vereinnahmte Entgelte: Steuer auf offenen Debitoren ({ende['umsatzsteuer']:.2f}) und Kreditoren "
                        f"({ende['vorsteuer']:.2f}) ist noch nicht abgerechnet — per 31.12. abgrenzen "
                        f"(Konten {k['umsatzsteuer_offen']}/{k['vorsteuer_offen']}).")
    if veraltet:
        hinweise.append(f"Die gebuchte Abgrenzung {year} passt nicht mehr zu den offenen Posten — neu buchen.")
        ok = False
    return {"jahr": year, "von": start, "bis": end, "methode": cfg["methode"], "abrechnungsart": cfg["abrechnungsart"],
            "perioden": perioden, "zeilen": zeilen, "ertrag": ertrag, "steuerkonten": steuerkonten,
            "abrechnungskonto": abrechnungskonto,
            "offen": ({"anfang": {"umsatzsteuer": anfang["umsatzsteuer"], "vorsteuer": anfang["vorsteuer"]},
                       "ende": {"umsatzsteuer": ende["umsatzsteuer"], "vorsteuer": ende["vorsteuer"],
                                "konten": ende["konten"], "belege": ende["belege"]}} if cash else None),
            "abgrenzung": saved_abgr, "abgrenzung_veraltet": veraltet, "frist_korrektur": frist,
            "ok": ok and not open_periods, "hinweise": hinweise}


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
        + e("businessReferenceId", f"aeradex-{rep['periode']}{'-K' if korrektur else ''}")
        + "<eCH-0217:sendingApplication>"
        + f"<eCH-0058:manufacturer>aeradex</eCH-0058:manufacturer><eCH-0058:product>aeradex</eCH-0058:product>"
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

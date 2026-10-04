"""Foreign exchange: official daily rates (BAZG, the rates the ESTV uses for MWST).

Source: https://www.backend-rates.bazg.admin.ch/api/xmldaily?d=YYYYMMDD&locale=de
Rates are cached per day under .batzen/kurse/<JJJJ-MM-TT>.yaml so a book can be
re-evaluated offline and the rate used stays documented. A rate quoted per 100
units (e.g. "100 JPY") is normalised to 1 unit.

Foreign-currency accounts (kontenplan: `waehrung: EUR`) carry each booking's
amount in their currency and the rate used (journal columns FW and Kurs); the
books stay in CHF. At a Stichtag, `revaluation()` values every such account
at the BAZG rate of that day and books the difference to Kursgewinn/-verlust.
"""
from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .book import Book, BookError, Row
from .files import CENT, parse_date, read_yaml, write_yaml

URL = "https://www.backend-rates.bazg.admin.ch/api/xmldaily?d={d}&locale=de"


def parse(data: bytes) -> tuple[str, dict[str, Decimal]]:
    root = ET.fromstring(data)
    ns = {"r": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    p = "r:" if ns else ""
    datum = root.findtext(f"{p}datum", namespaces=ns) or ""
    rates = {}
    for dev in root.findall(f"{p}devise", ns):
        code = (dev.get("code") or "").upper()
        unit = (dev.findtext(f"{p}waehrung", namespaces=ns) or "1").split()[0]
        kurs = dev.findtext(f"{p}kurs", namespaces=ns)
        if code and kurs:
            rates[code] = Decimal(kurs) / Decimal(unit)
    return datum, rates


def rates(book: Book, day: date, fetch=None) -> dict[str, Decimal]:
    """Rates valid for `day` (CHF per 1 unit). Weekends/holidays fall back to the last published day."""
    cache = book.root / ".batzen" / "kurse"
    for back in range(0, 8):
        d = day - timedelta(days=back)
        path = cache / f"{d.isoformat()}.yaml"
        if path.exists():
            return {k: Decimal(str(v)) for k, v in read_yaml(path).get("kurse", {}).items()}
        if d > date.today():
            continue
        try:
            data = (fetch or _get)(URL.format(d=d.strftime("%Y%m%d")))
        except urllib.error.HTTPError:
            data = b""                       # no table for that day
        except OSError as exc:
            raise BookError(f"BAZG-Kurse nicht erreichbar ({exc}) — Kurs von Hand angeben") from None
        try:
            published, found = parse(data)
        except ET.ParseError:            # empty/HTML answer: no table for that day
            published, found = "", {}
        if found:
            # BAZG answers weekends with the last table (e.g. Friday's, valid Sat–Mon)
            write_yaml(path, {"datum": d.isoformat(), "publiziert": published, "quelle": "BAZG Tageskurse",
                              "kurse": found})
            return found
    if day > date.today():
        raise BookError(f"Der BAZG-Kurs für {day} ist noch nicht publiziert")
    raise BookError(f"Keine BAZG-Kurse für {day} gefunden")


def rate(book: Book, currency: str, day: date, fetch=None) -> Decimal:
    currency = currency.upper()
    if currency == "CHF":
        return Decimal(1)
    table = rates(book, day, fetch)
    if currency not in table:
        raise BookError(f"Kein BAZG-Kurs für {currency} am {day}")
    return table[currency]


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


# ---------- revaluation at a Stichtag ----------

def preview(book: Book, stichtag, fetch=None) -> list[dict]:
    """Every foreign-currency account at `stichtag`: balance in its currency, BAZG rate,
    CHF value at that rate, CHF value in the books, difference."""
    from .ledger import BalanceEngine, fw_balance
    when = parse_date(stichtag, "stichtag")
    eng = BalanceEngine(book)
    out = []
    for nr, a in sorted(book.accounts.items()):
        if not a.is_foreign:
            continue
        fw = fw_balance(book, nr, when)
        kurs = rate(book, a.waehrung, when, fetch)
        target = (fw * kurs).quantize(CENT)
        chf = eng.balance_at(nr, when) if when.year in book.years() else a.eroeffnung
        out.append({"konto": nr, "name": a.name, "waehrung": a.waehrung, "fw": fw, "kurs": kurs,
                    "chf_neu": target, "chf_buch": chf, "differenz": target - chf})
    return out


def preview_bills(book: Book, stichtag, fetch=None) -> list[dict]:
    """Open foreign-currency supplier bills at `stichtag`: open amount × BAZG rate against
    their CHF book value. Positive difference = the debt grew (Kursverlust)."""
    from . import kreditoren as kred
    when = parse_date(stichtag, "stichtag")
    paid = kred.payments(book)
    out = []
    for nr, meta in sorted(kred.bills(book).items()):
        if not kred.is_foreign(meta) or meta.get("status") == "storniert" or parse_date(meta["datum"]) > when:
            continue
        rows = [r for r in paid.get(nr, []) if r.datum <= when]
        open_fw = Decimal(str(meta["betrag"])) - sum((r.fw or Decimal(0) for r in rows), Decimal(0))
        if open_fw <= 0:
            continue
        cur = kred.currency(meta)
        kurs = rate(book, cur, when, fetch)
        value = kred.book_value(book, meta, when, paid_rows=rows)
        target = (open_fw * kurs).quantize(CENT)
        out.append({"nummer": nr, "name": meta.get("name"), "waehrung": cur, "fw": open_fw, "kurs": kurs,
                    "chf_neu": target, "chf_buch": value, "differenz": target - value})
    return out


def preview_invoices(book: Book, stichtag, fetch=None) -> list[dict]:
    """Open foreign-currency customer invoices at `stichtag`: open amount × BAZG rate against their
    CHF book value. Positive difference = the receivable grew (Kursgewinn)."""
    from . import invoices as inv
    when = parse_date(stichtag, "stichtag")
    paid = inv.settlements(book)
    out = []
    for nr, meta in sorted(inv.invoices(book).items()):
        if not inv.is_foreign(meta) or meta.get("status") == "storniert" or parse_date(meta["datum"]) > when:
            continue
        st = inv.invoice_state(book, meta, paid.get(nr, []), when)
        if st["offen"] <= 0:
            continue
        cur = inv.currency(meta)
        kurs = rate(book, cur, when, fetch)
        target = (st["offen"] * kurs).quantize(CENT)
        out.append({"nummer": nr, "name": st["name"], "waehrung": cur, "fw": st["offen"], "kurs": kurs,
                    "chf_neu": target, "chf_buch": st["offen_chf"], "differenz": target - st["offen_chf"]})
    return out


def invoice_revaluations(book: Book) -> dict[str, list[tuple[date, Decimal]]]:
    """Per customer invoice, the revaluations booked at each Stichtag (CHF, + = receivable grew)."""
    out: dict[str, list] = {}
    folder = book.root / "bewertung"
    for p in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        saved = read_yaml(p) or {}
        when = parse_date(saved.get("stichtag"))
        for line in saved.get("debitoren") or []:
            out.setdefault(str(line["nummer"]), []).append((when, Decimal(str(line["differenz"])).quantize(CENT)))
    return out


def bill_revaluations(book: Book) -> dict[str, list[tuple[date, Decimal]]]:
    """Per supplier bill, the revaluations booked at each Stichtag (CHF, + = debt grew)."""
    out: dict[str, list] = {}
    folder = book.root / "bewertung"
    for p in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        saved = read_yaml(p) or {}
        when = parse_date(saved.get("stichtag"))
        for line in saved.get("kreditoren") or []:
            out.setdefault(str(line["nummer"]), []).append((when, Decimal(str(line["differenz"])).quantize(CENT)))
    return out


def path(book: Book, when: date) -> Path:
    return book.root / "bewertung" / f"{when.isoformat()}.yaml"


def revaluation_rows(book: Book, saved: dict) -> list[Row]:
    when = parse_date(saved["stichtag"])
    gain, loss = book.settings.konto("kursgewinn"), book.settings.konto("kursverlust")
    rows = []
    for line in saved.get("konten") or []:
        diff = Decimal(str(line["differenz"])).quantize(CENT)
        if not diff:
            continue
        nr, cur, kurs = str(line["konto"]), str(line["waehrung"]), Decimal(str(line["kurs"]))
        text = f"Bewertung {cur} per {when:%d.%m.%Y}"
        if diff > 0:
            rows.append(Row(when, f"FX-{when.isoformat()}", text, nr, gain, diff, f"bewertung:{when.isoformat()}",
                            waehrung=cur, fw=Decimal("0.00"), kurs=kurs))
        else:
            rows.append(Row(when, f"FX-{when.isoformat()}", text, loss, nr, -diff, f"bewertung:{when.isoformat()}",
                            waehrung=cur, fw=Decimal("0.00"), kurs=kurs))
    debitoren = book.settings.konto("debitoren")
    for line in saved.get("debitoren") or []:
        diff = Decimal(str(line["differenz"])).quantize(CENT)
        if not diff:
            continue
        cur, kurs = str(line["waehrung"]), Decimal(str(line["kurs"]))
        text = f"Bewertung Rechnung {line['nummer']} {cur} per {when:%d.%m.%Y}"
        quelle = f"bewertung:{when.isoformat()}"
        if diff > 0:     # the receivable grew in CHF
            rows.append(Row(when, f"FX-{when.isoformat()}", text, debitoren, gain, diff, quelle,
                            waehrung=cur, fw=Decimal("0.00"), kurs=kurs))
        else:
            rows.append(Row(when, f"FX-{when.isoformat()}", text, loss, debitoren, -diff, quelle,
                            waehrung=cur, fw=Decimal("0.00"), kurs=kurs))
    kreditoren = book.settings.konto("kreditoren")
    for line in saved.get("kreditoren") or []:
        diff = Decimal(str(line["differenz"])).quantize(CENT)
        if not diff:
            continue
        cur, kurs = str(line["waehrung"]), Decimal(str(line["kurs"]))
        text = f"Bewertung Kreditor {line['nummer']} {cur} per {when:%d.%m.%Y}"
        quelle = f"bewertung:{when.isoformat()}"
        if diff > 0:     # the debt grew in CHF
            rows.append(Row(when, f"FX-{when.isoformat()}", text, loss, kreditoren, diff, quelle,
                            waehrung=cur, fw=Decimal("0.00"), kurs=kurs))
        else:
            rows.append(Row(when, f"FX-{when.isoformat()}", text, kreditoren, gain, -diff, quelle,
                            waehrung=cur, fw=Decimal("0.00"), kurs=kurs))
    return rows


def book_revaluation(book: Book, stichtag, fetch=None) -> tuple[dict, list[Path]]:
    """Value all foreign-currency accounts at the BAZG rate of `stichtag` and book the differences."""
    from .journal import ensure_open, post
    when = parse_date(stichtag, "stichtag")
    if path(book, when).exists():
        raise BookError(f"Fremdwährungen per {when} sind bereits bewertet")
    ensure_open(book, when)
    lines = preview(book, when, fetch)
    bills = preview_bills(book, when, fetch)
    receivables = preview_invoices(book, when, fetch)
    if not lines and not bills and not receivables:
        raise BookError("Weder Fremdwährungskonten (waehrung: EUR …) noch offene Fremdwährungs-Rechnungen")
    from . import invoices as inv, kreditoren as kred
    later = [nr for nr, rows in kred.payments(book).items() for r in rows if r.datum > when and r.fw]
    later += [nr for nr, rows in inv.settlements(book).items() for r in rows if r.datum > when and r.fw]
    if later:
        raise BookError(f"Nach dem {when} sind schon Fremdwährungs-Rechnungen bezahlt ({', '.join(sorted(set(later)))}) — "
                        "die Bewertung muss vor diesen Zahlungen gebucht werden")
    added = _ensure_accounts(book) if any(l["differenz"] for l in lines + bills + receivables) else []
    saved = {"stichtag": when.isoformat(), "quelle": "BAZG Tageskurse",
             "konten": [{"konto": l["konto"], "waehrung": l["waehrung"], "fw": l["fw"], "kurs": l["kurs"],
                         "chf_buch": l["chf_buch"], "chf_neu": l["chf_neu"], "differenz": l["differenz"]} for l in lines]}
    if bills:
        saved["kreditoren"] = [{"nummer": b["nummer"], "waehrung": b["waehrung"], "fw": b["fw"], "kurs": b["kurs"],
                                "chf_buch": b["chf_buch"], "chf_neu": b["chf_neu"], "differenz": b["differenz"]}
                               for b in bills]
    if receivables:
        saved["debitoren"] = [{"nummer": b["nummer"], "waehrung": b["waehrung"], "fw": b["fw"], "kurs": b["kurs"],
                               "chf_buch": b["chf_buch"], "chf_neu": b["chf_neu"], "differenz": b["differenz"]}
                              for b in receivables]
    rows = revaluation_rows(book, saved)
    touched = post(book, rows) if rows else []
    write_yaml(path(book, when), saved)
    return saved, touched + added + [path(book, when)]


def _ensure_accounts(book: Book) -> list[Path]:
    """Books from before multicurrency lack the Kursdifferenz accounts: add them (KMU numbers)."""
    from .book import Account
    from .statements import default_group
    missing = [(book.settings.konto(key), name, klasse)
               for key, name, klasse in (("kursverlust", "Kursverluste", "aufwand"), ("kursgewinn", "Kursgewinne", "ertrag"))
               if book.settings.konto(key) not in book.accounts]
    for nr, name, klasse in missing:
        book.accounts[nr] = Account(nr=nr, name=name, klasse=klasse, gruppe=default_group(nr, klasse))
    if missing:
        book.save_accounts()
        return [book.root / "kontenplan.yaml"]
    return []

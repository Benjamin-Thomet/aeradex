"""Kreditoren: suppliers, supplier bills, QR-bill scanning, payment runs (pain.001).

Files:
    lieferanten/L0001-<name>.md        supplier: address, IBAN, default account and MWST code
    kreditoren/<JJJJ>/E-<JJJJ>-NNNN.md  supplier bill (frozen once entered, like an invoice)
    zahlungen/<datum>-<id>.xml         payment file for e-banking (ISO 20022 pain.001.001.09, SPS)

A bill is booked when entered (Aufwand + Vorsteuer an Kreditoren), so the
books show what is owed. Its payment is a journal row `kzahlung:<nr>`
(Kreditoren an Bank) — booked when the bank has executed it.

A bill may be split over several accounts (`positionen`, each with its own
MWST code) and may be in a foreign currency (`waehrung`, `kurs`: the BAZG rate
of the bill date unless given). Foreign bills are booked in CHF at that rate;
the payment clears their book value and books the difference to the CHF
actually paid as Kursgewinn/-verlust. Open foreign bills are revalued with the
foreign-currency accounts at a Stichtag (see fx.py).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from . import qrbill_ch as qr
from .book import Book, BookError, Row
from .files import CENT, parse_amount, parse_date, read_frontmatter, slug, write_frontmatter

ZERO = Decimal("0")
KREDITOREN = "2000"


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def kreditoren_konto(book: Book) -> str:
    return str((book.settings.get("konten") or {}).get("kreditoren") or KREDITOREN)


# ---------- Swiss QR-bill: decode and parse ----------

def _decode_images(path: Path) -> list[str]:
    """All QR payloads in a PDF or image. Uses zxing-cpp + pypdfium2 when
    installed (pip install 'aeradex[scan]'), else the zbarimg/pdftoppm tools."""
    suffix = path.suffix.lower()
    try:
        import zxingcpp
        images = []
        if suffix == ".pdf":
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(path))
            images = [pdf[i].render(scale=3).to_pil() for i in range(min(len(pdf), 6))]
        else:
            from PIL import Image
            images = [Image.open(path)]
        return [r.text for img in images for r in zxingcpp.read_barcodes(img) if r.text]
    except ImportError:
        pass
    if not shutil.which("zbarimg"):
        raise BookError("Zum Lesen von QR-Rechnungen: pip install 'aeradex[scan]' (oder zbar installieren)")
    targets = [path]
    tmp = None
    if suffix == ".pdf":
        if not shutil.which("pdftoppm"):
            raise BookError("pdftoppm fehlt (poppler) — oder pip install 'aeradex[scan]'")
        tmp = Path(tempfile.mkdtemp(prefix="aeradex-qr-"))
        subprocess.run(["pdftoppm", "-r", "200", "-png", "-l", "6", str(path), str(tmp / "p")],
                       capture_output=True, check=False)
        targets = sorted(tmp.glob("p*.png"))
    out = []
    for t in targets:
        r = subprocess.run(["zbarimg", "--raw", "-q", "-Sbinary", str(t)], capture_output=True, check=False)
        if r.stdout:
            out.append(r.stdout.decode("utf-8", errors="replace"))
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def parse_spc(payload: str) -> dict:
    """Parse a Swiss QR-bill payload (Implementation Guidelines 2.x)."""
    lines = payload.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) < 31 or lines[0].strip() != "SPC":
        raise BookError("Kein Swiss-QR-Zahlteil (SPC) erkannt")
    g = lambda i: lines[i].strip() if i < len(lines) else ""  # noqa: E731

    def address(start: int) -> dict:
        kind, name, l1, l2, plz, ort, land = (g(start + i) for i in range(7))
        if not name:
            return {}
        if kind == "K":  # combined lines: "Strasse 1" / "3000 Bern"
            m = re.match(r"^(\d{4,5})\s+(.*)$", l2)
            street = re.match(r"^(.*?)\s+(\d+\w*)$", l1)
            return {"name": name, "strasse": street.group(1) if street else l1, "nr": street.group(2) if street else "",
                    "plz": m.group(1) if m else "", "ort": m.group(2) if m else l2, "land": land or "CH"}
        return {"name": name, "strasse": l1, "nr": l2, "plz": plz, "ort": ort, "land": land or "CH"}

    data = {
        "iban": qr.normalize_iban(g(3)),
        "kreditor": address(4),
        "betrag": g(18),
        "waehrung": g(19) or "CHF",
        "schuldner": address(20),
        "referenz_typ": g(27) or "NON",
        "referenz": g(28).replace(" ", ""),
        "mitteilung": g(29),
        "rechnungsinfo": g(31),
    }
    if data["betrag"]:
        data["betrag"] = str(money(data["betrag"]))
    problem = qr.iban_problem(data["iban"])
    if problem:
        raise BookError(f"QR-Rechnung: {problem}")
    check_reference(data["iban"], data["referenz_typ"], data["referenz"])
    return data


def check_reference(iban: str, ref_type: str, reference: str) -> None:
    """Refuse a reference the bank would reject — better now than at payment."""
    ref_type = (ref_type or "NON").upper()
    reference = (reference or "").replace(" ", "")
    if ref_type == "QRR":
        if not qr.is_qr_iban(iban):
            raise BookError("QRR-Referenz verlangt eine QR-IBAN")
        if not re.fullmatch(r"\d{27}", reference) or qr.mod10r_check_digit(reference[:-1]) != int(reference[-1]):
            raise BookError(f"QR-Referenz {reference} ist ungültig (Prüfziffer)")
    elif ref_type == "SCOR":
        if not reference.upper().startswith("RF") or qr._mod97(reference[4:].upper() + reference[:4].upper()) != 1:
            raise BookError(f"Creditor Reference {reference} ist ungültig (Prüfziffer)")
    elif qr.is_qr_iban(iban):
        raise BookError("Eine QR-IBAN verlangt eine QRR-Referenz")


def scan(book: Book, datei: str) -> dict:
    """Read the QR-bill from a PDF/photo in the book (usually inbox/) and match the supplier."""
    path = Path(datei) if Path(datei).is_absolute() else book.root / datei
    if not path.is_file():
        raise BookError(f"Datei {datei} nicht gefunden")
    payloads = [p for p in _decode_images(path) if p.lstrip().startswith("SPC")]
    if not payloads:
        raise BookError(f"In {path.name} wurde kein Swiss-QR-Zahlteil gefunden")
    data = parse_spc(payloads[0])
    match = next((s for s in suppliers(book).values() if qr.normalize_iban(s.get("iban")) == data["iban"]), None)
    data["lieferant"] = match["nummer"] if match else None
    data["datei"] = str(path.relative_to(book.root)) if path.resolve().is_relative_to(book.root) else str(path)
    return data


# ---------- suppliers ----------

def suppliers(book: Book) -> dict[str, dict]:
    out = {}
    folder = book.root / "lieferanten"
    for path in sorted(folder.glob("*.md")) if folder.exists() else []:
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        meta["_notizen"] = body.strip()
        out[str(meta.get("nummer"))] = meta
    return out


def supplier(book: Book, nr: str) -> dict:
    found = suppliers(book).get(str(nr))
    if found is None:
        raise BookError(f"Lieferant {nr} nicht gefunden")
    return found


def add_supplier(book: Book, name: str, strasse: str = "", nr: str = "", plz: str = "", ort: str = "",
                 land: str = "CH", iban: str = "", konto: str = "", mwst: str = "", email: str = "",
                 notizen: str = "") -> tuple[dict, Path]:
    if not name.strip():
        raise BookError("Name des Lieferanten fehlt")
    iban = qr.normalize_iban(iban)
    if iban and qr.iban_problem(iban) and not qr.iban_is_valid(iban):
        raise BookError(f"IBAN: {qr.iban_problem(iban)}")
    if konto:
        book.account(konto)
    if mwst:
        from .mwst import code
        code(mwst)
    existing = suppliers(book)
    if iban and any(qr.normalize_iban(s.get("iban")) == iban for s in existing.values()):
        raise BookError(f"Ein Lieferant mit IBAN {iban} existiert bereits")
    nums = [int(k[1:]) for k in existing if re.fullmatch(r"L\d+", k)]
    number = f"L{max(nums, default=0) + 1:04d}"
    meta = {"nummer": number, "name": name.strip(),
            "adresse": {"strasse": strasse, "nr": str(nr), "plz": str(plz), "ort": ort, "land": land or "CH"},
            "iban": iban, "konto": str(konto or ""), "mwst": (mwst or "").upper(), "email": email}
    path = book.root / "lieferanten" / f"{number}-{slug(name)}.md"
    write_frontmatter(path, meta, notizen)
    return meta, path


# ---------- supplier bills ----------

FROZEN = ("nummer", "lieferant", "name", "rechnungsnr", "datum", "faellig", "betrag", "waehrung", "iban",
          "referenz_typ", "referenz", "mitteilung", "konto", "mwst", "kreditorenkonto")


def fingerprint(meta: dict) -> str:
    frozen = {k: str(meta.get(k) if meta.get(k) is not None else "") for k in FROZEN}
    frozen["betrag"] = f"{money(meta.get('betrag')):.2f}"
    # Only present on newer bills, so the fingerprints of existing bills stay valid.
    if meta.get("kurs") not in (None, ""):
        frozen["kurs"] = str(Decimal(str(meta["kurs"])).normalize())
    if meta.get("positionen"):
        frozen["positionen"] = json.dumps(positions(meta), sort_keys=True, default=str)
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def bills(book: Book) -> dict[str, dict]:
    out = {}
    folder = book.root / "kreditoren"
    for path in sorted(folder.glob("*/E-*.md")) if folder.exists() else []:
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        meta["_text"] = body.strip()
        out[str(meta.get("nummer"))] = meta
    return out


def bill(book: Book, nr: str) -> dict:
    found = bills(book).get(nr)
    if found is None:
        raise BookError(f"Kreditor {nr} nicht gefunden")
    return found


def next_number(book: Book, year: int) -> str:
    prefix = f"E-{year}-"
    nums = [int(k[len(prefix):]) for k in bills(book) if k.startswith(prefix) and k[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:04d}"


def currency(meta: dict) -> str:
    return str(meta.get("waehrung") or "CHF").upper()


def is_foreign(meta: dict) -> bool:
    return currency(meta) != "CHF"


def positions(meta: dict) -> list[dict]:
    """The bill's account split; a bill without `positionen` is one position on `konto`."""
    if meta.get("positionen"):
        return [{"konto": str(p["konto"]), "betrag": f"{money(p['betrag']):.2f}", "mwst": str(p.get("mwst") or "").upper(),
                 "text": str(p.get("text") or "")} for p in meta["positionen"]]
    return [{"konto": str(meta.get("konto") or ""), "betrag": f"{money(meta.get('betrag')):.2f}",
             "mwst": str(meta.get("mwst") or "").upper(), "text": ""}]


def booking_rows(book: Book, meta: dict, rounding=ROUND_HALF_UP) -> list[Row]:
    """The journal rows an entered bill owns: Aufwand (+ Vorsteuer) an Kreditoren, per position,
    in CHF (a foreign bill at its rate, the foreign amount kept on every row)."""
    from .journal import convert
    from .mwst import split
    quelle = f"kreditor:{meta['nummer']}"
    base = f"Kreditor {meta['nummer']} – {meta.get('name')}" + (f" ({meta['rechnungsnr']})" if meta.get("rechnungsnr") else "")
    rows: list[Row] = []
    for p in positions(meta):
        row = Row(parse_date(meta["datum"]), meta["nummer"], base + (f" · {p['text']}" if p["text"] else ""),
                  p["konto"], str(meta.get("kreditorenkonto") or KREDITOREN), money(p["betrag"]), quelle)
        rows += split(book, row, p["mwst"])
    for r in rows:
        r.quelle = quelle
    if is_foreign(meta):
        rows = convert(book, rows, currency(meta), Decimal(str(meta["kurs"])), rounding=rounding)
    return rows


def booked_chf(book: Book, meta: dict) -> Decimal:
    """What the bill put on the Kreditoren account, in CHF."""
    konto = str(meta.get("kreditorenkonto") or KREDITOREN)
    return sum((r.betrag for r in booking_rows(book, meta) if r.haben == konto), ZERO)


def book_value(book: Book, meta: dict, until: date | None = None, paid_rows: list[Row] | None = None) -> Decimal:
    """The CHF value of what is still owed on a bill: as booked, plus Stichtag revaluations,
    minus what payments have cleared — up to `until`."""
    if meta.get("status") == "storniert":
        return ZERO
    from . import fx
    paid_rows = payments(book).get(meta["nummer"], []) if paid_rows is None else paid_rows
    value = booked_chf(book, meta) if is_foreign(meta) else money(meta.get("betrag"))
    value += sum((diff for d, diff in fx.bill_revaluations(book).get(meta["nummer"], []) if until is None or d <= until), ZERO)
    value -= sum((r.betrag for r in paid_rows if until is None or r.datum <= until), ZERO)
    return value


def add_bill(book: Book, lieferant: str, betrag, datum=None, faellig=None, konto: str = "", mwst: str | None = None,
             rechnungsnr: str = "", referenz_typ: str = "", referenz: str = "", mitteilung: str = "",
             iban: str = "", datei: str = "", text: str = "", waehrung: str = "", kurs=None,
             positionen: list[dict] | None = None) -> tuple[dict, list[Path]]:
    """Enter a supplier bill and book it. `betrag` is the gross amount in the bill's currency;
    `positionen` splits it over several accounts: [{konto, betrag, mwst?, text?}]."""
    from .journal import attach, ensure_open, post
    sup = supplier(book, lieferant)
    d = parse_date(datum, "datum") if datum else date.today()
    ensure_open(book, d)
    amount = parse_amount(betrag, "betrag")
    if amount <= 0 or amount != amount.quantize(CENT):
        raise BookError("Betrag muss positiv sein und höchstens zwei Nachkommastellen haben")
    cur = (waehrung or "CHF").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", cur):
        raise BookError(f"Währung '{cur}' ist kein ISO-Code")
    rate = None
    if cur != "CHF":
        from . import fx
        rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
        if rate <= 0:
            raise BookError("Kurs muss positiv sein")
    split_lines = []
    if positionen:
        from .mwst import code as mwst_code
        for i, p in enumerate(positionen, 1):
            pk = str(p.get("konto") or "").strip()
            if not pk:
                raise BookError(f"Position {i}: Konto fehlt")
            book.account(pk)
            pb = parse_amount(p.get("betrag"), f"Position {i} betrag")
            if pb <= 0 or pb != pb.quantize(CENT):
                raise BookError(f"Position {i}: Betrag muss positiv sein und höchstens zwei Nachkommastellen haben")
            pm = str(p.get("mwst") or "").strip().upper()
            if pm:
                mwst_code(pm)
            split_lines.append({"konto": pk, "betrag": f"{pb:.2f}", "mwst": pm, "text": str(p.get("text") or "").strip()})
        total = sum((Decimal(p["betrag"]) for p in split_lines), ZERO)
        if total != amount:
            raise BookError(f"Die Positionen ergeben {total:.2f}, die Rechnung lautet auf {amount:.2f} {cur}")
        konto = split_lines[0]["konto"]
    konto = str(konto or sup.get("konto") or "")
    if not konto:
        raise BookError("Aufwandkonto fehlt (beim Lieferanten hinterlegen oder angeben)")
    book.account(konto)
    iban = qr.normalize_iban(iban or sup.get("iban"))
    if not iban:
        raise BookError("IBAN fehlt (beim Lieferanten hinterlegen oder von der QR-Rechnung)")
    if not qr.iban_is_valid(iban):
        raise BookError(f"IBAN {iban}: Prüfsumme stimmt nicht")
    ref_type = (referenz_typ or ("QRR" if qr.is_qr_iban(iban) else "SCOR" if (referenz or "").upper().startswith("RF") else "NON")).upper()
    check_reference(iban, ref_type, referenz)
    frist = (parse_date(faellig, "faellig") if faellig else d + timedelta(days=30))
    nummer = next_number(book, d.year)
    meta = {"nummer": nummer, "lieferant": sup["nummer"], "name": sup["name"], "rechnungsnr": rechnungsnr,
            "datum": d.isoformat(), "faellig": frist.isoformat(), "betrag": amount, "waehrung": cur,
            "iban": iban, "referenz_typ": ref_type, "referenz": (referenz or "").replace(" ", ""),
            "mitteilung": mitteilung or (rechnungsnr and f"Rechnung {rechnungsnr}") or "",
            "konto": konto, "mwst": ((mwst if mwst is not None else sup.get("mwst")) or "").upper(),
            "kreditorenkonto": kreditoren_konto(book), "status": "offen"}
    if rate is not None:
        meta["kurs"] = str(rate)
    if split_lines:
        meta["positionen"] = split_lines
        meta["mwst"] = ""
    meta["fingerprint"] = fingerprint(meta)
    rows = booking_rows(book, meta)
    touched = post(book, rows)
    if datei:
        source = Path(datei) if Path(datei).is_absolute() else book.root / datei
        target = attach(book, rows[0], source)
        meta["datei"] = str(target.relative_to(book.root))
        touched += [target, source]
    path = book.root / "kreditoren" / str(d.year) / f"{nummer}.md"
    write_frontmatter(path, meta, text)
    return meta, touched + [path]


def bill_fingerprint_ok(meta: dict) -> bool:
    return meta.get("fingerprint") == fingerprint(meta)


def payments(book: Book) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    accounts = {str(m.get("kreditorenkonto") or KREDITOREN) for m in bills(book).values()} or {KREDITOREN}
    for r in book.rows:
        kind, _, nr = r.quelle.partition(":")
        if kind == "kzahlung" and nr and r.soll in accounts:
            out[nr].append(r)
    return out


def state(book: Book, meta: dict, paid_rows: list[Row] | None = None) -> dict:
    """Amounts in the bill's currency; `offen_chf` is what the open part is worth in the books."""
    paid_rows = payments(book).get(meta["nummer"], []) if paid_rows is None else paid_rows
    foreign = is_foreign(meta)
    paid = sum(((r.fw or ZERO) if foreign else r.betrag for r in paid_rows), ZERO)
    total = money(meta.get("betrag"))
    status = meta.get("status") or "offen"
    open_amount = ZERO if status == "storniert" else total - paid
    if status != "storniert":
        status = "bezahlt" if open_amount <= 0 else ("angewiesen" if meta.get("zahlungslauf") else "offen")
    return {"nummer": meta["nummer"], "lieferant": meta.get("lieferant"), "name": meta.get("name"),
            "rechnungsnr": meta.get("rechnungsnr") or "", "datum": str(meta.get("datum")),
            "faellig": str(meta.get("faellig")), "total": total, "bezahlt": paid, "offen": open_amount,
            "status": status, "zahlungslauf": meta.get("zahlungslauf") or "", "waehrung": currency(meta),
            "offen_chf": (book_value(book, meta, paid_rows=paid_rows) if foreign else open_amount)
            if status != "storniert" else ZERO}


def payment_account(book: Book, cur: str) -> tuple[str, str]:
    """(IBAN, ledger account) payments in `cur` are made from: a bank account of that currency
    under `bankkonten` in aeradex.yaml, else the CHF payment account (the bank converts)."""
    for iban, konto in (book.settings.get("bankkonten") or {}).items():
        acct = book.accounts.get(str(konto))
        if cur != "CHF" and acct is not None and acct.waehrung == cur and not qr.is_qr_iban(qr.normalize_iban(iban)):
            return qr.normalize_iban(iban), str(konto)
    return debtor_account(book), book.settings.konto("bank")


def pay(book: Book, nr: str, datum=None, betrag=None, konto: str | None = None, kurs=None,
        fw=None) -> tuple[Row, list[Path]]:
    """Book the payment of a bill: Kreditoren an Bank. `betrag` is in the paying account's
    currency (from a CHF account: the CHF actually debited). For a foreign bill the open book
    value is cleared and the difference booked as Kursgewinn/-verlust; `fw` settles only
    part of it (default: everything open)."""
    from .journal import next_beleg, post
    meta = bill(book, nr)
    st = state(book, meta)
    if st["status"] == "storniert":
        raise BookError(f"{nr} ist storniert")
    if is_foreign(meta):
        return _pay_foreign(book, meta, st, datum, betrag, konto, kurs, fw)
    if konto and book.account(konto).is_foreign:
        raise BookError(f"Konto {konto} führt {book.account(konto).waehrung}, {nr} lautet auf CHF")
    amount = money(betrag) if betrag not in (None, "") else st["offen"]
    if amount <= 0:
        raise BookError(f"{nr} ist bereits bezahlt")
    if amount > st["offen"]:
        raise BookError(f"Betrag {amount} übersteigt den offenen Betrag {st['offen']} von {nr}")
    d = parse_date(datum, "datum") if datum else date.today()
    row = Row(d, next_beleg(book, d.year), f"Zahlung Kreditor {nr} – {meta.get('name')}",
              str(meta.get("kreditorenkonto") or KREDITOREN), konto or book.settings.konto("bank"), amount,
              f"kzahlung:{nr}")
    return row, post(book, [row])


def _pay_foreign(book: Book, meta: dict, st: dict, datum, betrag, konto, kurs, fw) -> tuple[Row, list[Path]]:
    from . import fx
    from .journal import next_beleg, post
    nr, cur = meta["nummer"], currency(meta)
    d = parse_date(datum, "datum") if datum else date.today()
    konto = konto or payment_account(book, cur)[1]
    bank_acct = book.account(konto)
    open_fw = st["offen"]
    if bank_acct.is_foreign:
        if bank_acct.waehrung != cur:
            raise BookError(f"Konto {konto} führt {bank_acct.waehrung}, {nr} lautet auf {cur}")
        settle = money(fw or betrag) if (fw or betrag) not in (None, "") else open_fw
        rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
        bank_chf = (settle * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    else:
        settle = money(fw) if fw not in (None, "") else open_fw
        if betrag not in (None, ""):
            bank_chf = money(betrag)              # what the bank actually debited in CHF
            rate = None
        else:
            rate = parse_amount(kurs, "kurs") if kurs not in (None, "") else fx.rate(book, cur, d)
            bank_chf = (settle * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    if settle <= 0:
        raise BookError(f"{nr} ist bereits bezahlt")
    if settle > open_fw:
        raise BookError(f"{settle} {cur} übersteigt den offenen Betrag {open_fw} {cur} von {nr}")
    value = book_value(book, meta, d)
    clear = value if settle == open_fw else (value * settle / open_fw).quantize(CENT, rounding=ROUND_HALF_UP)
    beleg = next_beleg(book, d.year)
    quelle = f"kzahlung:{nr}"
    text = f"Zahlung Kreditor {nr} – {meta.get('name')} ({cur} {settle:.2f})"
    kreditoren = str(meta.get("kreditorenkonto") or KREDITOREN)
    rows = [Row(d, beleg, text, kreditoren, "", clear, quelle, waehrung=cur, fw=settle,
                kurs=(clear / settle).quantize(Decimal("1e-10")).normalize())]
    bank_row = Row(d, beleg, text, "", konto, bank_chf, quelle)
    if bank_acct.is_foreign:
        bank_row.waehrung, bank_row.fw, bank_row.kurs = cur, settle, rate
    rows.append(bank_row)
    diff = bank_chf - clear
    if diff > 0:
        rows.append(Row(d, beleg, f"Kursverlust {nr}", book.settings.konto("kursverlust"), "", diff, quelle))
    elif diff < 0:
        rows.append(Row(d, beleg, f"Kursgewinn {nr}", "", book.settings.konto("kursgewinn"), -diff, quelle))
    if diff:
        from .fx import _ensure_accounts
        _ensure_accounts(book)
    return rows[0], post(book, rows)


def amount_fits(book: Book, meta: dict, st: dict, amount: Decimal, konto: str) -> bool:
    """Could a bank debit of `amount` (in the account's currency) pay this bill?"""
    if not is_foreign(meta):
        return amount <= st["offen"]
    acct = book.accounts.get(konto)
    if acct is not None and acct.is_foreign:
        return acct.waehrung == currency(meta) and amount <= st["offen"]
    return True                                   # CHF debit for a foreign bill: the rate decides


def void(book: Book, nr: str, grund: str = "") -> tuple[dict, list[Path]]:
    from .journal import ensure_open
    meta = bill(book, nr)
    if meta.get("status") == "storniert":
        raise BookError(f"{nr} ist bereits storniert")
    if payments(book).get(nr):
        raise BookError(f"{nr} ist (teilweise) bezahlt — Zahlung zuerst stornieren")
    ensure_open(book, parse_date(meta["datum"]))
    touched = book.remove_rows(lambda r: r.quelle == f"kreditor:{nr}")
    path, text = meta.pop("_pfad"), meta.pop("_text", "")
    meta["status"] = "storniert"
    meta["storniert_am"] = date.today().isoformat()
    if grund:
        meta["storno_grund"] = grund
    write_frontmatter(path, meta, text)
    return meta, touched + [path]


# ---------- payment run: pain.001 ----------

def debtor_account(book: Book) -> str:
    """The account payments are made from: `zahlungs_iban` in aeradex.yaml, else the
    IBAN — a QR-IBAN only receives money, it cannot be debited."""
    iban = qr.normalize_iban(book.settings.get("zahlungs_iban") or book.settings.get("iban"))
    if not iban:
        raise BookError("Keine IBAN für Zahlungen (Einstellungen → IBAN bzw. zahlungs_iban)")
    if qr.is_qr_iban(iban):
        raise BookError("Die hinterlegte IBAN ist eine QR-IBAN; für Zahlungsaufträge die normale "
                        "Konto-IBAN als zahlungs_iban in den Einstellungen erfassen")
    return iban


def _x(text) -> str:
    return escape(str(text or ""))[:140]


def pain001(book: Book, nummern: list[str], ausfuehrung) -> tuple[bytes, dict]:
    """Build a pain.001.001.09 credit transfer file (Swiss Payment Standards) for
    the open amount of each bill. Returns the XML and a summary."""
    d = parse_date(ausfuehrung, "ausfuehrung")
    debtor_iban = debtor_account(book)
    s = book.settings
    paid = payments(book)
    items = []
    for nr in nummern:
        meta = bill(book, nr)
        st = state(book, meta, paid.get(nr, []))
        if st["status"] in ("storniert", "bezahlt"):
            raise BookError(f"{nr} ist {st['status']}")
        items.append((meta, st["offen"]))
    if not items:
        raise BookError("Keine Rechnungen gewählt")
    msg_id = f"AERADEX-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}".upper()
    total = sum((a for _, a in items), ZERO)
    a = s.adresse
    by_currency: dict[str, list] = defaultdict(list)
    for meta, amount in items:
        by_currency[currency(meta)].append((meta, amount))

    def postal(adr: dict) -> str:
        parts = []
        for tag, key in (("StrtNm", "strasse"), ("BldgNb", "nr"), ("PstCd", "plz"), ("TwnNm", "ort")):
            if adr.get(key):
                parts.append(f"<{tag}>{_x(adr[key])[:70 if tag in ('StrtNm', 'TwnNm') else 16]}</{tag}>")
        parts.append(f"<Ctry>{_x(adr.get('land') or 'CH')}</Ctry>")
        return "<PstlAdr>" + "".join(parts) + "</PstlAdr>"

    txs: dict[str, list[str]] = defaultdict(list)
    sups = suppliers(book)
    for i, (meta, amount) in enumerate(items, 1):
        sup = sups.get(meta.get("lieferant"), {})
        ref_type, ref = meta.get("referenz_typ") or "NON", meta.get("referenz") or ""
        if ref_type == "QRR":
            rmt = (f"<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry><Prtry>QRR</Prtry></CdOrPrtry></Tp>"
                   f"<Ref>{_x(ref)}</Ref></CdtrRefInf>{'<AddtlRmtInf>' + _x(meta.get('mitteilung')) + '</AddtlRmtInf>' if meta.get('mitteilung') else ''}</Strd></RmtInf>")
        elif ref_type == "SCOR":
            rmt = (f"<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry><Cd>SCOR</Cd></CdOrPrtry></Tp>"
                   f"<Ref>{_x(ref)}</Ref></CdtrRefInf></Strd></RmtInf>")
        else:
            rmt = f"<RmtInf><Ustrd>{_x(meta.get('mitteilung') or meta.get('rechnungsnr') or meta['nummer'])}</Ustrd></RmtInf>"
        txs[currency(meta)].append(
            f"<CdtTrfTxInf><PmtId><InstrId>{msg_id[-20:]}-{i}</InstrId><EndToEndId>{_x(meta['nummer'])}</EndToEndId></PmtId>"
            f"<Amt><InstdAmt Ccy=\"{currency(meta)}\">{amount:.2f}</InstdAmt></Amt>"
            f"<Cdtr><Nm>{_x(meta.get('name'))[:70]}</Nm>{postal(sup.get('adresse') or {})}</Cdtr>"
            f"<CdtrAcct><Id><IBAN>{meta['iban']}</IBAN></Id></CdtrAcct>{rmt}</CdtTrfTxInf>")
    # One payment block per currency, each from the account that pays in it (SPS: one currency per B-level).
    blocks = []
    for n, (cur, group) in enumerate(sorted(by_currency.items()), 1):
        iban = payment_account(book, cur)[0] if cur != "CHF" else debtor_iban
        sub = sum((amt for _, amt in group), ZERO)
        blocks.append(
            f"<PmtInf><PmtInfId>{msg_id[-27:]}-{n}</PmtInfId><PmtMtd>TRF</PmtMtd><BtchBookg>true</BtchBookg>"
            f"<NbOfTxs>{len(group)}</NbOfTxs><CtrlSum>{sub:.2f}</CtrlSum>"
            f"<ReqdExctnDt><Dt>{d.isoformat()}</Dt></ReqdExctnDt>"
            f"<Dbtr><Nm>{_x(s.firma)[:70]}</Nm>{postal(a)}</Dbtr>"
            f"<DbtrAcct><Id><IBAN>{iban}</IBAN></Id></DbtrAcct>"
            f"<DbtrAgt><FinInstnId><ClrSysMmbId><ClrSysId><Cd>CHBCC</Cd></ClrSysId><MmbId>{iban[4:9]}</MmbId></ClrSysMmbId></FinInstnId></DbtrAgt>"
            + "".join(txs[cur]) + "</PmtInf>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09">'
        "<CstmrCdtTrfInitn>"
        f"<GrpHdr><MsgId>{msg_id}</MsgId><CreDtTm>{datetime.now().replace(microsecond=0).isoformat()}</CreDtTm>"
        f"<NbOfTxs>{len(items)}</NbOfTxs><CtrlSum>{total:.2f}</CtrlSum><InitgPty><Nm>{_x(s.firma)[:70]}</Nm></InitgPty></GrpHdr>"
        + "".join(blocks) + "</CstmrCdtTrfInitn></Document>")
    return xml.encode("utf-8"), {"msg_id": msg_id, "anzahl": len(items), "total": total, "ausfuehrung": d,
                                 "nummern": [m["nummer"] for m, _ in items],
                                 "waehrungen": {c: sum((amt for _, amt in g), ZERO) for c, g in by_currency.items()}}


def create_run(book: Book, nummern: list[str], ausfuehrung) -> tuple[dict, list[Path]]:
    """Write the payment file and mark the bills as instructed."""
    xml, info = pain001(book, nummern, ausfuehrung)
    path = book.root / "zahlungen" / f"{info['ausfuehrung'].isoformat()}-{info['msg_id'][-6:].lower()}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xml)
    touched = [path]
    for nr in info["nummern"]:
        meta = bill(book, nr)
        p, text = meta.pop("_pfad"), meta.pop("_text", "")
        meta["zahlungslauf"] = path.name
        write_frontmatter(p, meta, text)
        touched.append(p)
    return {**info, "datei": str(path.relative_to(book.root))}, touched


def runs(book: Book) -> list[dict]:
    folder = book.root / "zahlungen"
    all_bills = bills(book)
    paid = payments(book)
    out = []
    for path in sorted(folder.glob("*.xml"), reverse=True) if folder.exists() else []:
        members = [m for m in all_bills.values() if m.get("zahlungslauf") == path.name]
        states = [state(book, m, paid.get(m["nummer"], [])) for m in members]
        out.append({"datei": path.name, "ausfuehrung": path.name[:10], "anzahl": len(members),
                    "total": sum((s["total"] for s in states), ZERO),
                    "offen": [s["nummer"] for s in states if s["status"] == "angewiesen"],
                    "bezahlt": all(s["status"] == "bezahlt" for s in states) if states else False})
    return out


def book_run(book: Book, datei: str, datum=None) -> tuple[list[Row], list[Path]]:
    """The bank executed the run: book the payment of every bill still open in it."""
    run = next((r for r in runs(book) if r["datei"] == Path(datei).name), None)
    if run is None:
        raise BookError(f"Zahlungslauf {datei} nicht gefunden")
    if not run["offen"]:
        raise BookError("Alle Rechnungen dieses Zahlungslaufs sind bereits gebucht")
    d = datum or run["ausfuehrung"]
    rows, touched = [], []
    for nr in run["offen"]:
        meta = bill(book, nr)
        row, t = pay(book, nr, d, konto=payment_account(book, currency(meta))[1] if is_foreign(meta) else None)
        rows.append(row)
        touched += t
    return rows, touched


def open_payables(book: Book) -> dict:
    paid = payments(book)
    rows = [state(book, m, paid.get(m["nummer"], [])) for m in bills(book).values()]
    rows = [r for r in rows if r["status"] in ("offen", "angewiesen")]
    rows.sort(key=lambda r: (r["faellig"], r["nummer"]))
    from .ledger import BalanceEngine
    konto = kreditoren_konto(book)
    eng = BalanceEngine(book)
    year = max(book.years())
    saldo = -eng.balance(konto, year)
    total = sum((r["offen_chf"] for r in rows), ZERO)
    return {"posten": rows, "total_offen": total, "kreditorenkonto": konto, "saldo_kreditoren": saldo,
            "differenz": saldo - total}


def open_payables(book: Book, stichtag: date | None = None) -> dict:
    """Kreditoren open on `stichtag` (default today): bills dated until then minus payments booked until then,
    aged by bill date like the Debitoren, reconciled with the Kreditoren account."""
    from .invoices import AGE_BUCKETS
    from .ledger import BalanceEngine
    stichtag = stichtag or date.today()
    paid = payments(book)
    rows, buckets = [], {b[0]: Decimal("0") for b in AGE_BUCKETS}
    for meta in bills(book).values():
        if parse_date(meta["datum"]) > stichtag or meta.get("status") == "storniert":
            continue
        st = state(book, meta, [r for r in paid.get(meta["nummer"], []) if r.datum <= stichtag])
        if st["offen"] <= 0:
            continue
        age = (stichtag - parse_date(meta["datum"])).days
        label = next(b[0] for b in AGE_BUCKETS if b[1] <= age <= b[2])
        buckets[label] += st["offen_chf"]
        rows.append({**st, "alter_tage": age, "kategorie": label})
    rows.sort(key=lambda r: (r["datum"], r["nummer"]))
    konto = kreditoren_konto(book)
    saldo = -BalanceEngine(book).balance_at(konto, stichtag)
    total = sum((r["offen_chf"] for r in rows), Decimal("0"))
    return {"stichtag": stichtag, "posten": rows, "kategorien": buckets, "total_offen": total,
            "kreditorenkonto": konto, "saldo_kreditoren": saldo, "differenz": saldo - total}

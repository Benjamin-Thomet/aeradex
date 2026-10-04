"""Belegeingang: documents from upload to draft — read, classify, assign accounts, never book.

A file (PDF, photo, scan) becomes a draft under ``eingang/ENT-NNNN.yaml`` of one of
three kinds (``art``), recognised from the document and changeable by a person:

    kreditor   a supplier bill still to be paid            → Kreditoren (pain.001, open items)
    quittung   a receipt for something already paid       → journal, matched with the bank
    debitor    an invoice we issued outside batzen        → Debitoren (open item, payment matching)

Two chains fill a draft, each step only filling what the previous ones left open:

    reading     QR-bill (exact) → plugin readers → PDF text layer → Tesseract OCR
    account     known supplier/customer → Jev (if enabled, above its threshold) → agent

A receipt is matched against the bank: an open bank movement with the same amount is
booked with it; a booking that already exists only gets the receipt attached — so
nothing is booked twice.

Every field records where it came from ("QR", "Text", "OCR", "Lieferant L0001",
"Jev 0.86", "Agent"), so a person sees at a glance what to double-check. A draft
is turned into a booked supplier bill only by a person (Kreditoren → Prüfen).
The text read from the file is cached under ``.batzen/erfassung/`` (not committed).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import qrbill_ch as qr
from .book import Book, BookError, Row
from .files import CENT, read_yaml, write_yaml

FIELDS = ("lieferant", "kunde", "name", "strasse", "nr", "plz", "ort", "land", "uid", "iban", "betrag", "waehrung",
          "referenz_typ", "referenz", "mitteilung", "rechnungsnr", "datum", "faellig", "mwst_satz", "mwst_betrag",
          "zahlungsart")
ARTEN = {"kreditor": "Lieferantenrechnung", "quittung": "Quittung (bezahlt)", "debitor": "eigene Rechnung (extern erstellt)"}
_RECEIPT = re.compile(r"\b(quittung|kassenbon|kassenzettel|kassenbeleg|bon-?nr|barzahlung|bar bezahlt|bezahlt|rückgeld|"
                      r"wechselgeld|kartenzahlung|twint|visa|mastercard|maestro|v pay|debit|postfinance card|kreditkarte|"
                      r"terminal|reçu|ticket de caisse|payé|scontrino|ricevuta|pagato|paid|receipt)\b", re.I)


# ---------- reading ----------

def read_qr(book: Book, path: Path) -> dict | None:
    from . import kreditoren as kred
    if path.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"):
        return None
    try:
        payloads = [p for p in kred._decode_images(path) if p.lstrip().startswith("SPC")]
    except BookError:
        return None
    if not payloads:
        return None
    try:
        data = kred.parse_spc(payloads[0])
    except BookError:
        return None
    out = {k: data.get(k) for k in ("iban", "betrag", "waehrung", "referenz_typ", "referenz", "mitteilung") if data.get(k)}
    out.update({k: v for k, v in (data.get("kreditor") or {}).items() if v})
    if data.get("rechnungsinfo"):
        # Swico /10/ is the invoice number in structured billing information
        m = re.search(r"/10/([^/]+)", data["rechnungsinfo"])
        if m:
            out["rechnungsnr"] = m.group(1)
    return out


def pdf_text(path: Path, max_pages: int = 4) -> str:
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        if shutil.which("pdftotext"):
            r = subprocess.run(["pdftotext", "-l", str(max_pages), "-layout", str(path), "-"], capture_output=True)
            return r.stdout.decode("utf-8", errors="replace")
        return ""
    pdf = pdfium.PdfDocument(str(path))
    return "\n".join(pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), max_pages)))


def ocr_languages() -> str:
    if not shutil.which("tesseract"):
        return ""
    r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    have = set(r.stdout.split())
    return "+".join(l for l in ("deu", "fra", "ita", "eng") if l in have)


def ocr_text(path: Path, max_pages: int = 3) -> str:
    """Tesseract on a photo/scan (or on the rendered pages of an image-only PDF)."""
    langs = ocr_languages()
    if not langs:
        return ""
    tmp = Path(tempfile.mkdtemp(prefix="batzen-ocr-"))
    try:
        images = []
        if path.suffix.lower() == ".pdf":
            try:
                import pypdfium2 as pdfium
            except ImportError:
                return ""
            pdf = pdfium.PdfDocument(str(path))
            for i in range(min(len(pdf), max_pages)):
                target = tmp / f"p{i}.png"
                pdf[i].render(scale=300 / 72).to_pil().save(target)
                images.append(target)
        else:
            images = [path]
        out = []
        for img in images:
            r = subprocess.run(["tesseract", str(img), "stdout", "-l", langs, "--psm", "4"], capture_output=True,
                               timeout=120)
            out.append(r.stdout.decode("utf-8", errors="replace"))
        return "\n".join(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- text → fields ----------

# 1234.50 · 1'234.50 · 1 234,50 · 1.234,50 (EU) · 1,234.50 (US)
_AMOUNT = r"(?<![\d.,'])(\d{1,3}(?:['’ .,]\d{3})+|\d+)[.,](\d{2})(?!\d)"
_MONTHS = {"januar": 1, "janvier": 1, "gennaio": 1, "februar": 2, "février": 2, "fevrier": 2, "febbraio": 2,
           "märz": 3, "maerz": 3, "mars": 3, "marzo": 3, "april": 4, "avril": 4, "aprile": 4, "mai": 5, "maggio": 5,
           "juni": 6, "juin": 6, "giugno": 6, "juli": 7, "juillet": 7, "luglio": 7, "august": 8, "août": 8,
           "aout": 8, "agosto": 8, "september": 9, "septembre": 9, "settembre": 9, "oktober": 10, "octobre": 10,
           "ottobre": 10, "november": 11, "novembre": 11, "dezember": 12, "décembre": 12, "decembre": 12,
           "dicembre": 12}
_DATE = (r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?!\d)|(\d{4})-(\d{2})-(\d{2})"
         r"|(\d{1,2})\.?\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})")
_TOTAL = re.compile(r"(total|gesamt|rechnungsbetrag|zu bezahlen|zahlbetrag|betrag fällig|endbetrag|montant|à payer|"
                    r"totale|importo|amount due)", re.I)
_VAT = re.compile(r"(mwst|mw\.?st|mehrwertsteuer|tva|iva|vat)", re.I)
_LEGAL = re.compile(r"\b(AG|GmbH|SA|Sàrl|Sarl|S\.A\.|Genossenschaft|KlG|& Co|Ltd|Inc)\b")


def amounts(line: str) -> list[Decimal]:
    out = []
    for whole, cents in re.findall(_AMOUNT, line):
        try:
            out.append(Decimal(re.sub(r"['’ .,]", "", whole) + "." + cents))
        except InvalidOperation:
            continue
    return out


def dates(text: str) -> list[date]:
    out = []
    for m in re.finditer(_DATE, text, re.I):
        g = m.groups()
        try:
            if g[0]:
                y = int(g[2]) + (2000 if len(g[2]) == 2 else 0)
                out.append(date(y, int(g[1]), int(g[0])))
            elif g[3]:
                out.append(date(int(g[3]), int(g[4]), int(g[5])))
            else:
                out.append(date(int(g[8]), _MONTHS[g[7].lower()], int(g[6])))
        except (ValueError, KeyError):
            continue
    return out


def _after(lines: list[str], pattern: str, finder):
    """First value `finder` returns on a line matching `pattern`, or on the line below it."""
    rx = re.compile(pattern, re.I)
    for i, line in enumerate(lines):
        m = rx.search(line)
        if m:
            found = finder(line[m.end():]) or (finder(lines[i + 1]) if i + 1 < len(lines) else None)
            if found:
                return found
    return None


def parse_text(text: str, own_name: str = "") -> dict:
    """The fields a Swiss supplier invoice usually states, from its text."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out: dict = {}
    first_date = lambda s: (dates(s) or [None])[0]  # noqa: E731
    d = _after(lines, r"(rechnungsdatum|datum der rechnung|invoice date|date de facture|data fattura|\bdatum\b|\bdate\b|\bdata\b)",
               first_date)
    all_dates = dates(text)
    d = d or (all_dates[0] if all_dates else None)
    if d:
        out["datum"] = d.isoformat()
    due = _after(lines, r"(zahlbar bis|fällig am|fälligkeit|faellig|zahlungsfrist bis|payable until|due date|échéance|"
                        r"scadenza)", first_date)
    if not due and d:
        m = re.search(r"(?:innert|within|dans les|entro)\s+(\d{1,3})\s+(?:tagen|days|jours|giorni)|"
                      r"(\d{1,3})\s+tage\s+netto|netto\s+(\d{1,3})\s+tage", text, re.I)
        if m:
            due = d + timedelta(days=int(next(g for g in m.groups() if g)))
    if due:
        out["faellig"] = due.isoformat()
    nr = _after(lines, r"(rechnung(?:s)?[- ]?(?:nr\.?|nummer|no\.?)|rg\.?-?nr\.?|invoice (?:no\.?|number|#)|"
                       r"facture (?:n[°o]\.?|numéro)|fattura (?:n\.?|numero))\s*[:#]?",
                lambda s: (re.match(r"\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/.]{2,24})", s, re.I) or [None, None])[1])
    if nr:
        out["rechnungsnr"] = nr.rstrip(".")
    total_lines = [l for l in lines if _TOTAL.search(l) and not re.search(r"(zwischen|sous-total|subtotal|netto|exkl)", l, re.I)]
    totals = [a for l in total_lines for a in amounts(l)]
    if totals:
        out["betrag"] = str(max(totals).quantize(CENT))
        best = next(l for l in total_lines if max(totals) in amounts(l))
        cur = re.search(r"\b(CHF|EUR|USD|GBP)\b|(€)|(\$)|(£)", best)
        if cur:
            out["waehrung"] = cur.group(1) or {"€": "EUR", "$": "USD", "£": "GBP"}[next(g for g in cur.groups()[1:] if g)]
    for line in lines:
        if _VAT.search(line):
            rate = re.search(r"(8[.,]1|2[.,]6|3[.,]8|7[.,]7|2[.,]5|3[.,]7)\s*%", line)
            if rate and "mwst_satz" not in out:
                out["mwst_satz"] = rate.group(1).replace(",", ".")
            values = [a for a in amounts(line) if a < Decimal(out.get("betrag") or "1e12")]
            if rate and values and "mwst_betrag" not in out:
                out["mwst_betrag"] = str(values[-1])
    uid = re.search(r"CHE[- ]?(\d{3})\.?(\d{3})\.?(\d{3})", text)
    if uid:
        out["uid"] = f"CHE-{uid.group(1)}.{uid.group(2)}.{uid.group(3)}"
    for m in re.finditer(r"\b([A-Z]{2}\d{2}(?:\s?[0-9A-Z]{4}){2,7}(?:\s?[0-9A-Z]{1,3})?)\b", text):
        iban = qr.normalize_iban(m.group(1))
        if qr.iban_is_valid(iban):        # the checksum rules out look-alikes (UID, order numbers …)
            out["iban"] = iban
            break
    own = own_name.lower().strip()
    for i, line in enumerate(lines[:15]):
        if own and own in line.lower():
            continue
        if _LEGAL.search(line) and not re.search(r"\d{4}\s", line) and len(line) < 70:
            out["name"] = line
            # the sender's address usually follows its name: "Strasse 5" / "3000 Bern"
            for nxt in lines[i + 1:i + 4]:
                street = re.match(r"^([A-Za-zÀ-ÿ][\w .\-'’]*?)\s+(\d+\s?[a-zA-Z]?)$", nxt)
                town = re.match(r"^(?:(?:CH|D|DE|A|AT|F|FR|I|IT|FL|LI)-)?(\d{4,5})\s+([A-Za-zÀ-ÿ][\w .\-'’]*)$", nxt)
                if street and "strasse" not in out:
                    out["strasse"], out["nr"] = street.group(1), street.group(2).replace(" ", "")
                elif town:
                    out["plz"], out["ort"] = town.group(1), town.group(2)
                    break
            break
    if "name" not in out:
        # no company with a legal form: take the first line that reads like a name (shop receipts)
        for line in lines[:4]:
            if (own and own in line.lower()) or len(line) > 50 or re.search(r"\d{3,}|[:@/]", line):
                continue
            if re.search(r"[A-Za-zÀ-ÿ]{3,}", line) and not _RECEIPT.search(line) and not re.search(
                    r"\b(rechnung|facture|fattura|invoice|datum|date)\b", line, re.I):
                out["name"] = line
                break
    if out.get("iban") and out["iban"][:2] not in ("CH", "LI") and "land" not in out:
        out["land"] = out["iban"][:2]
    low = text.lower()
    if re.search(r"\b(bar|barzahlung|bargeld|rückgeld|wechselgeld|cash|espèces|contanti)\b", low):
        out["zahlungsart"] = "bar"
    elif re.search(r"\btwint\b", low):
        out["zahlungsart"] = "twint"
    elif re.search(r"\b(visa|mastercard|amex|american express|kreditkarte)\b", low) and "debit" not in low:
        out["zahlungsart"] = "kreditkarte"
    elif re.search(r"\b(maestro|debit|v pay|postfinance card|ec-karte|karte|carte|card)\b", low):
        out["zahlungsart"] = "karte"
    return out


# ---------- the draft ----------

def folder(book: Book) -> Path:
    return book.root / "eingang"


def _folders(book: Book) -> list[Path]:
    return [folder(book), book.root / "kreditoren" / "entwuerfe"]      # the second: drafts from v0.6


def text_cache(book: Book, draft_id: str) -> Path:
    return book.root / ".batzen" / "erfassung" / f"{draft_id}.txt"


def drafts(book: Book, art: str = "") -> dict[str, dict]:
    out = {}
    for f in _folders(book):
        for path in sorted(f.glob("ENT-*.yaml")) if f.exists() else []:
            meta = read_yaml(path) or {}
            meta["_pfad"] = path
            meta.setdefault("art", "kreditor")
            if not art or meta["art"] == art:
                out[str(meta.get("id") or path.stem)] = meta
    return dict(sorted(out.items()))


def draft(book: Book, draft_id: str) -> dict:
    found = drafts(book).get(draft_id.upper())
    if found is None:
        raise BookError(f"Entwurf {draft_id} nicht gefunden")
    return found


def next_id(book: Book) -> str:
    nums = [int(k[4:]) for k in drafts(book) if re.fullmatch(r"ENT-\d+", k)]
    return f"ENT-{max(nums, default=0) + 1:04d}"


def value(meta: dict, key: str, default=""):
    return ((meta.get("felder") or {}).get(key) or {}).get("wert", default)


def _set(fields: dict, key: str, val, source: str, overwrite: bool = False) -> None:
    if val in (None, "") or (key in fields and not overwrite):
        return
    fields[key] = {"wert": str(val), "quelle": source}


def analyse(book: Book, path: Path) -> tuple[dict, str, list[str]]:
    """Read a file through the reader chain. Returns (fields, text, notes)."""
    from . import plugins
    fields: dict = {}
    notes: list[str] = []
    text = ""
    for reader in sorted(plugins.beleg_leser(book), key=lambda r: r.prioritaet):
        try:
            found = reader.read(book, path, text)
        except Exception as exc:  # one broken reader must not stop the others
            notes.append(f"{reader.label}: {type(exc).__name__}: {exc}")
            continue
        if not found:
            continue
        if found.get("_text") and not text:
            text = found["_text"]
        for key, val in found.items():
            if key in FIELDS:
                _set(fields, key, val, reader.label)
    if not text:
        notes.append("Kein Text erkannt (weder Textebene noch OCR)")
    return fields, text, notes


def classify(book: Book, fields: dict, text: str, hint: str = "") -> tuple[str, str]:
    """(art, why). Our own IBAN or UID on the document → an invoice we issued; receipt words and
    no IBAN → a receipt; else a supplier bill. A hint from the page (Debitoren) wins for debitor."""
    v = lambda k: value({"felder": fields}, k)  # noqa: E731
    own_ibans = {qr.normalize_iban(book.settings.get(k)) for k in ("iban", "zahlungs_iban")} - {""}
    own_uid = re.sub(r"\D", "", book.settings.get("uid") or "")
    if hint == "debitor":
        return "debitor", "auf der Debitoren-Seite hochgeladen"
    if qr.normalize_iban(v("iban")) in own_ibans:
        return "debitor", "die IBAN auf dem Beleg ist die eigene"
    if own_uid and re.sub(r"\D", "", v("uid")) == own_uid:
        return "debitor", "die UID auf dem Beleg ist die eigene"
    if not v("iban") and _RECEIPT.search(text or ""):
        return "quittung", "Quittung (bezahlt, keine IBAN)"
    return "kreditor", "Rechnung mit Zahlungsangaben" if v("iban") else "Rechnung"


def _same_name(a: str, b: str) -> bool:
    """Loose company-name match: legal forms and punctuation do not count."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", re.sub(r"\b(ag|gmbh|sa|sàrl|sarl|ltd|inc|schweiz|suisse)\b", "",  # noqa: E731
                                                      s.lower()))
    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or (len(min(x, y, key=len)) >= 4 and (x in y or y in x)))


def match_supplier(book: Book, fields: dict) -> tuple[dict | None, str]:
    """The known supplier of a bill, and a conflict note when the IBAN belongs to a
    supplier whose name does not match the name on the bill (classic invoice fraud)."""
    from . import kreditoren as kred
    sups = kred.suppliers(book)
    name = value({"felder": fields}, "name")
    iban = qr.normalize_iban(value({"felder": fields}, "iban"))
    if iban:
        for s in sups.values():
            if qr.normalize_iban(s.get("iban")) == iban:
                if name and not _same_name(name, str(s.get("name", ""))):
                    return None, (f"IBAN {iban} gehört dem Lieferanten {s['nummer']} {s.get('name')}, die Rechnung "
                                  f"nennt aber «{name}» — nicht zugeordnet, bitte prüfen")
                return s, ""
    if name:
        for s in sups.values():
            if _same_name(name, str(s.get("name", ""))):
                return s, ""              # a different IBAN than on file is flagged by warnings()
    return None, ""


def mwst_code(book: Book, konto: str, rate: str) -> str:
    """Vorsteuer code from the stated rate and the account: material/services (4xxx) → V, else I."""
    from . import mwst
    if not rate or mwst.config(book)["methode"] != "effektiv":
        return ""
    digits = rate.replace(".", "")
    prefix = "V" if konto.startswith("4") else "I"
    code = f"{prefix}{digits}"
    return code if code in mwst.CODES else ""


def kontieren(book: Book, meta: dict, text: str) -> None:
    """Account assignment by kind. Leaves `konto` empty when unsure (then the agent's turn)."""
    art = meta.get("art") or "kreditor"
    if art == "debitor":
        _kontieren_debitor(book, meta)
    else:
        if art == "kreditor":
            _match_kreditor(book, meta)
        _jev(book, meta, text)
        if art == "quittung":
            if not meta.get("zahlung"):
                meta["zahlung"] = match_payment(book, meta)
    konto = (meta.get("konto") or {}).get("wert")
    if konto and not (meta.get("mwst") or {}).get("wert") and art != "debitor":
        code = mwst_code(book, konto, value(meta, "mwst_satz"))
        if code:
            meta["mwst"] = {"wert": code, "quelle": f"Satz {value(meta, 'mwst_satz')} % + Konto"}
    if art == "kreditor" and not (meta.get("mwst") or {}).get("wert"):
        from . import mwst as m
        land = value(meta, "land") or (value(meta, "iban")[:2] if value(meta, "iban") else "")
        if land and land not in ("CH", "LI") and m.config(book)["methode"] != "keine":
            note = "Ausländischer Lieferant: ist es eine Dienstleistung, schuldet die Firma Bezugsteuer (Code B81)"
            if note not in (meta.get("hinweise") or []):
                meta.setdefault("hinweise", []).append(note)
    meta["status"] = status(meta)


def _kontieren_debitor(book: Book, meta: dict) -> None:
    from . import invoices, mwst as m
    fields = meta["felder"]
    name = value(meta, "name")
    for nr, c in invoices.customers(book).items():
        names = {str(c.get("firma") or ""), str(c.get("name") or ""), qr.invoice_name(c)}
        if name and any(_same_name(name, n) for n in names if n):
            _set(fields, "kunde", nr, f"Kunde {nr}", overwrite=True)
            break
    if not (meta.get("konto") or {}).get("wert"):
        meta["konto"] = {"wert": book.settings.konto("ertrag"), "quelle": "Standard-Ertragskonto"}
    rate = value(meta, "mwst_satz")
    if rate and m.config(book)["methode"] != "keine" and not (meta.get("mwst") or {}).get("wert"):
        code = f"U{rate.replace('.', '')}"
        if code in m.CODES:
            meta["mwst"] = {"wert": code, "quelle": f"Satz {rate} %"}


def _match_kreditor(book: Book, meta: dict) -> None:
    fields = meta["felder"]
    sup, conflict = match_supplier(book, fields)
    if conflict:
        meta.setdefault("hinweise", []).append(conflict)
        meta["konflikt"] = conflict
    if sup:
        _set(fields, "lieferant", sup["nummer"], f"Lieferant {sup['nummer']}", overwrite=True)
        _set(fields, "name", sup.get("name"), f"Lieferant {sup['nummer']}")      # the bill's own name wins
        if sup.get("konto"):
            meta["konto"] = {"wert": str(sup["konto"]), "quelle": f"Lieferant {sup['nummer']}"}
            if sup.get("mwst"):
                meta["mwst"] = {"wert": sup["mwst"], "quelle": f"Lieferant {sup['nummer']}"}


def _jev(book: Book, meta: dict, text: str) -> None:
    if not (meta.get("konto") or {}).get("wert"):
        from . import jev
        cfg = jev.config(book)
        if cfg["bereit"]:
            try:
                res = jev.suggest_bill(book, meta, text, cfg)
            except BookError as exc:
                meta.setdefault("hinweise", []).append(f"Jev: {exc}")
            else:
                if res["konfidenz"] >= cfg["schwelle"]:
                    meta["konto"] = {"wert": res["konto"], "quelle": f"Jev {res['konfidenz']:.2f}",
                                     "konfidenz": round(res["konfidenz"], 3)}
                else:
                    meta.setdefault("hinweise", []).append(
                        f"Jev unsicher ({res['konfidenz']:.2f}): {res['konto']}"
                        + (f" · {', '.join(f'{k} {p:.0%}' for k, p in res['alternativen'] if p >= 0.01)}"
                           if res["alternativen"] else ""))


def match_payment(book: Book, meta: dict) -> dict:
    """How a receipt was paid: an open bank movement with the same amount (±5 days) is booked
    with it; an existing booking without a receipt only gets the file; else cash/bank/card
    from what the receipt says."""
    from . import bank
    from .files import parse_amount
    try:
        amount = Decimal(value(meta, "betrag")).quantize(CENT)
        when = date.fromisoformat(value(meta, "datum")) if value(meta, "datum") else None
    except (InvalidOperation, ValueError):
        amount, when = None, None
    cur = (value(meta, "waehrung") or "CHF").upper()
    if amount and when:
        near = lambda d: abs((d - when).days) <= 5  # noqa: E731
        taken = {d.get("zahlung", {}).get("bank") for d in drafts(book).values()} - {None}
        # a movement is in its account's currency: a CHF receipt matches CHF accounts, a EUR receipt EUR accounts
        hits = [t for t in bank.transactions(book) if t["Status"] == "offen" and t["ID"] not in taken
                and (bank.account_currency(book, t["Konto"]) or "CHF") == cur
                and parse_amount(t["Betrag"]) == -amount and near(date.fromisoformat(t["Datum"]))]
        if hits:
            t = min(hits, key=lambda t: abs((date.fromisoformat(t["Datum"]) - when).days))
            return {"art": "bank", "bank": t["ID"], "konto": t["Konto"], "quelle": "Bankabgleich",
                    "text": f"Bankbewegung {t['Datum']} {t.get('Gegenpartei') or t.get('Text', '')}"[:120]}
    if amount and when and cur == "CHF":
        folder_ = book.root / "belege"
        with_file = {p.name.split(" ")[0] for p in folder_.rglob("*") if p.is_file()} if folder_.exists() else set()
        liquid = {nr for nr, a in book.accounts.items() if a.klasse == "aktiv" and nr.startswith("10")}
        rows = [r for r in book.rows if r.haben in liquid and r.betrag == amount and near(r.datum)
                and r.beleg not in with_file and not r.quelle.startswith(("kreditor", "kzahlung", "lohn"))]
        if rows:
            r = min(rows, key=lambda r: abs((r.datum - when).days))
            return {"art": "buchung", "beleg": r.beleg, "konto": r.haben, "quelle": "Journalabgleich",
                    "text": f"Beleg {r.beleg} vom {r.datum:%d.%m.%Y}: {r.text}"[:120]}
    how = value(meta, "zahlungsart")
    konto = book.settings.konto("bank")
    if how == "bar" and "1000" in book.accounts:
        konto = "1000"
    elif how == "kreditkarte":
        card = next((nr for nr, a in sorted(book.accounts.items()) if a.klasse == "passiv"
                     and "kreditkarte" in a.name.lower()), None)
        konto = card or konto
    return {"art": "konto", "konto": konto,
            "quelle": {"bar": "Barzahlung", "twint": "TWINT", "karte": "Karte", "kreditkarte": "Kreditkarte"}.get(how, "Standard")}


def status(meta: dict) -> str:
    has = lambda k: bool(value(meta, k))  # noqa: E731
    art = meta.get("art") or "kreditor"
    if not (meta.get("konto") or {}).get("wert") and meta.get("status") == "agent":
        return "agent"
    if art == "debitor":
        return "bereit" if has("betrag") else "unvollstaendig"
    if art == "quittung":
        if not (meta.get("konto") or {}).get("wert"):
            return "unsicher"
        return "bereit" if has("betrag") else "unvollstaendig"
    if meta.get("konflikt"):
        return "konflikt"             # stays until a person decides, even with an account
    if not (meta.get("konto") or {}).get("wert"):
        return "unsicher"
    if not has("betrag") or not (has("iban") or has("lieferant")):
        return "unvollstaendig"
    return "bereit"


def create(book: Book, datei: str, fields: dict, text: str, notes: list[str],
           art: str = "") -> tuple[dict, list[Path]]:
    path = Path(datei) if Path(datei).is_absolute() else book.root / datei
    if not path.is_file():
        raise BookError(f"Datei {datei} nicht gefunden")
    rel = str(path.resolve().relative_to(book.root.resolve())) if path.resolve().is_relative_to(book.root.resolve()) else str(path)
    if any(d.get("datei") == rel for d in drafts(book).values()):
        raise BookError(f"Für {rel} gibt es schon einen Entwurf")
    draft_id = next_id(book)
    if art and art not in ARTEN:
        raise BookError(f"Belegart '{art}': {', '.join(ARTEN)}")
    found, why = classify(book, fields, text, art)
    if art and art != found and art != "kreditor":
        found, why = art, "vorgegeben"
    if art == "kreditor" and found == "debitor":
        found = "kreditor"              # on the Kreditoren page our own IBAN can only be a typo of the reader
    meta = {"id": draft_id, "art": found, "datei": rel, "erstellt": date.today().isoformat(), "felder": fields,
            "hinweise": list(notes) + ([f"Als {ARTEN[found]} erkannt: {why}"] if found != (art or found) or not art else []),
            "status": "neu"}
    kontieren(book, meta, text)
    target = folder(book) / f"{draft_id}.yaml"
    write_yaml(target, meta)
    cache = text_cache(book, draft_id)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return meta, [target]


def update(book: Book, draft_id: str, quelle: str, konto: str = "", mwst: str | None = None,
           begruendung: str = "", positionen: list[dict] | None = None, art: str = "",
           zahlkonto: str = "", mitarbeiter: str = "", **fields) -> tuple[dict, list[Path]]:
    meta = draft(book, draft_id)
    path = meta.pop("_pfad")
    if art and art != meta.get("art"):
        if art not in ARTEN:
            raise BookError(f"Belegart '{art}': {', '.join(ARTEN)}")
        meta["art"] = art
        meta.pop("konflikt", None)
        meta.pop("zahlung", None)
        if art == "debitor" or (meta.get("konto") or {}).get("quelle", "").startswith("Standard"):
            meta.pop("konto", None)
        meta.setdefault("hinweise", []).append(f"{quelle}: als {ARTEN[art]} eingeordnet")
        kontieren(book, meta, "")
    if zahlkonto:
        acct = book.account(zahlkonto)
        if acct.klasse not in ("aktiv", "passiv"):
            raise BookError(f"Zahlkonto {zahlkonto} muss ein Bilanzkonto sein (Kasse, Bank, Kreditkarte, Privat …)")
        meta["zahlung"] = {"art": "konto", "konto": zahlkonto, "quelle": quelle}
    if mitarbeiter:
        from . import payroll
        emp = payroll.employee(book, mitarbeiter)
        meta["zahlung"] = {"art": "spesen", "mitarbeiter": emp["nummer"], "spesenart": "uebrige", "quelle": quelle,
                           "text": f"privat bezahlt von {payroll.display_name(emp)} — Spesen über den Lohn"}
    if positionen:
        from .mwst import code as mwst_code
        lines = []
        for i, p in enumerate(positionen, 1):
            acct = book.account(str(p.get("konto") or ""))
            if acct.klasse not in ("aufwand", "aktiv"):
                raise BookError(f"Position {i}: Konto {acct.nr} ist weder Aufwand noch Aktivkonto")
            amount = Decimal(str(p.get("betrag") or "0")).quantize(CENT)
            if amount <= 0:
                raise BookError(f"Position {i}: Betrag fehlt")
            code = str(p.get("mwst") or "").upper()
            if code:
                mwst_code(code)
            lines.append({"konto": acct.nr, "betrag": f"{amount:.2f}", "mwst": code, "text": str(p.get("text") or "")})
        meta["positionen"] = {"zeilen": lines, "quelle": quelle, **({"begruendung": begruendung} if begruendung else {})}
        konto = konto or lines[0]["konto"]
        total = sum((Decimal(l["betrag"]) for l in lines), Decimal(0))
        gross = value(meta, "betrag") or fields.get("betrag")
        if gross and total != Decimal(str(gross)):
            meta.setdefault("hinweise", []).append(f"{quelle}: Positionen ergeben {total:.2f}, Rechnung {gross}")
    if konto:
        acct = book.account(konto)
        if meta.get("art") == "debitor":
            if acct.klasse != "ertrag":
                raise BookError(f"Konto {konto} ist kein Ertragskonto")
        elif acct.klasse not in ("aufwand", "aktiv"):
            raise BookError(f"Konto {konto} ist weder Aufwand noch Aktivkonto (Investition)")
        meta["konto"] = {"wert": konto, "quelle": quelle, **({"begruendung": begruendung} if begruendung else {})}
    if mwst is not None:
        if mwst:
            from . import mwst as m
            m.code(mwst)
        meta["mwst"] = {"wert": (mwst or "").upper(), "quelle": quelle}
    if quelle.startswith("Agent") and fields.get("iban"):
        current = meta["felder"].get("iban") or {}
        if current.get("quelle") == "QR" and qr.normalize_iban(fields["iban"]) != qr.normalize_iban(current.get("wert")):
            # The QR-bill is the payment instruction; text on the bill must never redirect a payment.
            meta.setdefault("hinweise", []).append(
                f"{quelle} wollte die IBAN auf {fields['iban']} ändern — die IBAN aus dem QR-Zahlteil bleibt")
            fields = {k: v for k, v in fields.items() if k != "iban"}
    for key, val in fields.items():
        if key not in FIELDS:
            raise BookError(f"Unbekanntes Feld {key}")
        if val in (None, ""):
            continue
        current = meta["felder"].get(key) or {}
        same = (qr.normalize_iban(str(val)) == qr.normalize_iban(current.get("wert", "")) if key == "iban"
                else str(val).strip() == str(current.get("wert", "")).strip())
        if not same:                       # confirming a value keeps where it was read from
            _set(meta["felder"], key, val, quelle, overwrite=True)
    if begruendung and not konto:
        meta.setdefault("hinweise", []).append(f"{quelle}: {begruendung}")
    meta["status"] = status(meta)
    write_yaml(path, meta)
    return meta, [path]


def mark(book: Book, draft_id: str, status_: str, hinweis: str = "") -> tuple[dict, list[Path]]:
    meta = draft(book, draft_id)
    path = meta.pop("_pfad")
    meta["status"] = status_
    if hinweis:
        meta.setdefault("hinweise", []).append(hinweis)
    write_yaml(path, meta)
    return meta, [path]


def discard(book: Book, draft_id: str) -> list[Path]:
    meta = draft(book, draft_id)
    meta["_pfad"].unlink()
    text_cache(book, draft_id).unlink(missing_ok=True)
    return [meta["_pfad"]]


def warnings(book: Book, meta: dict) -> list[str]:
    """What a person must look at before booking a draft."""
    from . import kreditoren as kred
    out = [meta["konflikt"]] if meta.get("konflikt") else []
    if meta.get("art") in ("quittung", "debitor"):
        return out
    iban = qr.normalize_iban(value(meta, "iban"))
    nr = value(meta, "lieferant")
    sup = kred.suppliers(book).get(nr) if nr else None
    if sup and iban and qr.normalize_iban(sup.get("iban")) and qr.normalize_iban(sup.get("iban")) != iban:
        out.append(f"IBAN {iban} weicht von der hinterlegten IBAN des Lieferanten ab "
                   f"({qr.normalize_iban(sup.get('iban'))}) — vor dem Zahlen beim Lieferanten bestätigen lassen")
    src = ((meta.get("felder") or {}).get("iban") or {}).get("quelle", "")
    if iban and src not in ("QR",) and not sup:
        out.append(f"IBAN stammt aus «{src}», nicht aus einem QR-Zahlteil — bei neuen Lieferanten genau prüfen")
    return out


def form_values(meta: dict) -> dict:
    """What the Kreditoren form needs from a draft."""
    out = {k: value(meta, k) for k in FIELDS}
    out["konto"] = (meta.get("konto") or {}).get("wert", "")
    out["mwst"] = (meta.get("mwst") or {}).get("wert", "")
    out["positionen"] = (meta.get("positionen") or {}).get("zeilen") or []
    return out


def needs_agent(meta: dict) -> bool:
    return (meta.get("art") != "debitor" and not (meta.get("konto") or {}).get("wert")
            and meta.get("status") != "agent")


def agent_auto(book: Book) -> bool:
    """Drafts nobody could account (no known supplier, Jev unsure or off) go to the agent by themselves,
    unless the book says otherwise (batzen.yaml → kreditoren: {agent_automatisch: false})."""
    return bool((book.settings.get("kreditoren") or {}).get("agent_automatisch", True))


def agent_prompt(meta: dict, book: Book) -> str:
    known = {k: v["wert"] for k, v in (meta.get("felder") or {}).items()}
    if meta.get("art") == "quittung":
        pay = meta.get("zahlung") or {}
        return (f"Kontiere den Beleg-Entwurf {meta['id']} (Quittung für etwas bereits Bezahltes, Datei {meta['datei']}).\n"
                f"Bisher erkannt: {known or 'nichts'}. Bezahlt über: {pay.get('text') or pay.get('konto') or 'unbekannt'}.\n"
                "Vorgehen: bill_draft() zeigt Entwurf und Text; lies bei Bedarf die Datei. Wähle mit accounts() das "
                "Aufwandkonto und — wenn das Buch MWST abrechnet und die Quittung MWST ausweist — den Vorsteuer-Code; "
                "Quittungen mit mehreren Sätzen (z.B. Lebensmittel 2.6 % und anderes 8.1 %) mit `aufteilung`. Stimmt das "
                "Zahlkonto nicht (bar = Kasse, Firmenkarte, privat bezahlt = Konto gegenüber der Person), setze "
                "`zahlkonto`. Ist es gar keine Quittung, sondern eine offene Rechnung, setze `art` auf «kreditor». "
                "Alles mit complete_bill_draft, mit begruendung. Buche nichts selbst — ein Mensch prüft.")
    if meta.get("art") == "debitor":
        return (f"Prüfe den Beleg-Entwurf {meta['id']} (eigene Rechnung an einen Kunden, ausserhalb von batzen erstellt, "
                f"Datei {meta['datei']}). Bisher erkannt: {known or 'nichts'}. Ergänze mit complete_bill_draft fehlende "
                "Felder (betrag, datum, faellig, rechnungsnr, name des Kunden) und das Ertragskonto (konto). "
                "Buche nichts selbst.")
    return (f"Kontiere den Kreditoren-Entwurf {meta['id']} (Lieferantenrechnung, Datei {meta['datei']} im Buch).\n"
            f"Bisher erkannt: {known or 'nichts'}.\n"
            + (f"Hinweise: {'; '.join(meta.get('hinweise') or [])}.\n" if meta.get("hinweise") else "")
            + "Vorgehen: bill_draft() zeigt Entwurf und erkannten Text; lies bei Bedarf die Datei. Wähle mit accounts() "
            "das passende Aufwandkonto (bei Anschaffungen ein Aktivkonto) und, falls die Rechnung MWST ausweist und das "
            "Buch effektiv abrechnet, den Vorsteuer-Code. Gehören Positionen der Rechnung auf verschiedene Konten "
            "(z.B. Material und eine Anschaffung), teile sie mit `aufteilung` auf (Beträge brutto, Summe = Rechnung). "
            "Lautet die Rechnung nicht auf CHF, setze `waehrung` (EUR, USD …) — umgerechnet wird zum BAZG-Kurs. "
            "Trage alles mit complete_bill_draft ein: konto, begruendung (ein Satz, warum), und fehlende oder falsch "
            "erkannte Felder (betrag, datum, faellig, rechnungsnr, name, iban). Ein Lieferant im Ausland, der eine "
            "Dienstleistung erbringt: bei MWST-pflichtigen Büchern Code B81 (Bezugsteuer). Ist der Beleg eine bereits "
            "bezahlte Quittung, setze `art` auf «quittung». "
            "Erfasse die Rechnung NICHT selbst und buche nichts — ein Mensch prüft den Entwurf.")


def book_receipt(book: Book, draft_id: str, datum, text: str, betrag, konto: str = "", mwst: str = "",
                 positionen: list[dict] | None = None, waehrung: str = "", kurs=None,
                 zahlung: dict | None = None) -> tuple[list[Row], list[Path]]:
    """Book a receipt draft. `zahlung`: {"art": "bank", "bank": id} books it with that open bank
    movement; {"art": "buchung", "beleg": "26-014"} only files the receipt with that booking;
    {"art": "konto", "konto": "1000"} books it against that account."""
    from . import bank
    from .files import parse_amount, parse_date
    from .journal import attach, convert, next_beleg, post
    from .mwst import split
    meta = draft(book, draft_id)
    if meta.get("art") != "quittung":
        raise BookError(f"{draft_id} ist keine Quittung ({ARTEN.get(meta.get('art'), meta.get('art'))})")
    source = book.root / meta["datei"]
    if not source.is_file():
        raise BookError(f"Datei {meta['datei']} fehlt")
    pay = zahlung or meta.get("zahlung") or {}
    touched: list[Path] = []
    if pay.get("art") == "buchung":
        rows = [r for r in book.rows if r.beleg == pay.get("beleg")]
        if not rows:
            raise BookError(f"Beleg {pay.get('beleg')} nicht gefunden")
        touched += [attach(book, rows[0], source), source]
        return rows, touched + discard(book, draft_id)
    if pay.get("art") == "spesen":
        from . import spesen
        lines_ = positionen or None
        meta_, t = spesen.add(book, str(pay.get("mitarbeiter") or ""), datum, text, betrag, konto, mwst, lines_,
                              pay.get("spesenart") or "uebrige", meta["datei"], waehrung, kurs)
        book.reload()
        return [r for r in book.rows if r.quelle == f"spesen:{meta_['nummer']}"], t + discard(book, draft_id)
    d = parse_date(datum, "datum")
    amount = parse_amount(betrag, "betrag")
    lines = positionen or [{"konto": konto, "betrag": str(amount), "mwst": mwst, "text": ""}]
    total = sum((parse_amount(p["betrag"], "betrag") for p in lines), Decimal(0))
    if total != amount:
        raise BookError(f"Die Positionen ergeben {total:.2f}, die Quittung lautet auf {amount:.2f}")
    tx = None
    if pay.get("art") == "bank":
        tx = bank.find(book, pay["bank"])
        if tx["Status"] != "offen":
            raise BookError(f"Bankbewegung {tx['ID']} ist {tx['Status']}")
        haben = tx["Konto"]
    else:
        haben = str(pay.get("konto") or book.settings.konto("bank"))
    book.account(haben)
    beleg = next_beleg(book, d.year)
    rows: list[Row] = []
    for p in lines:
        pk = str(p.get("konto") or "")
        if not pk:
            raise BookError("Aufwandkonto fehlt")
        book.account(pk)
        row = Row(d, beleg, text + (f" · {p['text']}" if p.get("text") else ""), pk, haben,
                  parse_amount(p["betrag"], "betrag"))
        rows += split(book, row, str(p.get("mwst") or "").upper())
    cur = (waehrung or "CHF").upper()
    fw_account = bank.account_currency(book, haben)
    if fw_account and cur != fw_account:
        raise BookError(f"Konto {haben} führt {fw_account}, die Quittung lautet auf {cur}")
    if cur != "CHF":
        if tx is not None and kurs in (None, "") and not fw_account:
            # paid by card from a CHF account: the bank's CHF amount fixes the rate
            kurs = (abs(parse_amount(tx["Betrag"])) / amount).quantize(Decimal("1e-10")).normalize()
        rows = convert(book, rows, cur, kurs)
    if tx is not None:
        paid = sum(((r.fw or Decimal(0)) if fw_account else r.betrag for r in rows if r.haben == haben), Decimal(0))
        unit = fw_account or "CHF"
        if paid != abs(parse_amount(tx["Betrag"])):
            raise BookError(f"Quittung {unit} {paid:.2f} ≠ Bankbewegung {unit} {abs(parse_amount(tx['Betrag'])):.2f}")
    touched += post(book, rows)
    touched += [attach(book, rows[0], source), source]
    if tx is not None:
        touched += bank._set(book, tx["ID"], Status="gebucht", Beleg=beleg)
    return rows, touched + discard(book, draft_id)


def run_agent(root: Path, draft_id: str) -> str:
    """Hand a draft to the book's agent (the backend chosen in the settings) and wait for it."""
    import queue

    from . import api
    try:
        from .web import chat
    except ImportError as exc:
        raise BookError(f"Für den Agenten fehlen Pakete ({exc.name}): pip install 'batzen[ui]'") from None
    book = Book(root)
    kind = chat.backend(book.settings.get("agent_backend"))
    if not kind:
        raise BookError("Kein Agent verfügbar (Claude Code, Codex, opencode oder ANTHROPIC_API_KEY)")
    meta = draft(book, draft_id)
    api.bill_draft_mark(book, draft_id, "agent", f"An den Agenten ({chat.BACKENDS.get(kind, kind)}) übergeben")
    runner = chat.AGENTS[kind](root)
    events: queue.Queue = queue.Queue()
    said: list[str] = []
    try:
        runner.run(events, agent_prompt(meta, book))
    except Exception as exc:
        api.bill_draft_mark(Book(root), draft_id, "unsicher", f"Agent: {type(exc).__name__}: {str(exc)[:200]}")
        raise BookError(f"Agent abgebrochen: {exc}") from None
    while not events.empty():
        ev = events.get()
        if ev.get("type") == "text":
            said.append(ev.get("text", ""))
    after = draft(Book(root), draft_id)
    if after.get("status") == "agent":
        api.bill_draft_mark(Book(root), draft_id, "unsicher",
                            "Agent hat kein Konto gesetzt" + (f": {said[-1][:200]}" if said else ""))
    return said[-1] if said else ""

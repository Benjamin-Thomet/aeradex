"""Statements in any format: learned CSV/Excel formats and credit card statements (PDF).

camt.053 and the plugin formats are recognised by their own code. For everything
else the book's agent helps — but nothing it reads goes into the books unchecked:

* CSV/Excel: the agent describes the *format* once (which column is the date, the
  amount, the text …) in `bank/formate/<name>.yaml`. batzen then reads every file of
  that bank by itself, without an agent. A person confirms a new format once, after a
  preview and the running-balance check (opening + movements = balance, row by row).
* Credit card statements (PDF): the agent reads the transactions into
  `bank/karten/<hash>.yaml`, keyed by the PDF's SHA-256. They are imported only if
  old balance + transactions = new balance to the cent and every amount appears in
  the PDF's text (a scanned PDF without text needs `bestaetigt: true`).

Both are offered to the bank import as an ordinary `BankFormat`, so matching,
rules, suggestions and the balance reconciliation work as for camt.053. A credit
card is a liability account (e.g. 2040 Kreditkarte): purchases come in as money
out, the monthly debit from the bank as money in.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .book import Book, BookError
from .files import CENT, read_yaml, slug, write_yaml

ZERO = Decimal("0")
EXCEL = (".xlsx", ".xlsm")
TEXT = (".csv", ".txt", ".tsv")
FIELDS = ("datum", "betrag", "belastung", "gutschrift", "gegenpartei", "referenz", "saldo", "id", "waehrung")


# ---------- reading a table ----------

def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _delimiter(text: str) -> str:
    """The delimiter that splits most lines into the same number (> 1) of fields."""
    lines = [ln for ln in text.splitlines()[:60] if ln.strip()]
    best, score = ";", 0
    for cand in (";", ",", "\t", "|"):
        counts = [len(r) for r in csv.reader(lines, delimiter=cand) if len(r) > 1]
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        if counts.count(common) * common > score:
            best, score = cand, counts.count(common) * common
    return best


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    return str(value).strip()


def rows_of(filename: str, data: bytes, trennzeichen: str = "") -> list[list[str]]:
    """The file as rows of strings (trailing empty cells removed, empty rows dropped)."""
    if Path(filename).suffix.lower() in EXCEL:
        try:
            import openpyxl
        except ImportError:
            raise BookError("Für Excel-Auszüge fehlt openpyxl: pip install 'batzen[excel]' — "
                            "oder den Auszug als CSV exportieren") from None
        sheet = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        raw = [[_cell(c) for c in row] for row in sheet.iter_rows(values_only=True)]
    else:
        text = _decode(data)
        raw = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=trennzeichen or _delimiter(text))]
    out = []
    for r in raw:
        while r and not r[-1]:
            r = r[:-1]
        if r:
            out.append(r)
    return out


def _norm(cell: str) -> str:
    return re.sub(r"\s+", " ", str(cell)).strip().strip('"').lower()


def find_header(rows: list[list[str]], kopfzeile: list[str]) -> int | None:
    want = [_norm(c) for c in kopfzeile]
    for i, row in enumerate(rows[:80]):
        if [_norm(c) for c in row] == want:
            return i
    return None


def preview_text(filename: str, data: bytes, lines: int = 40) -> str:
    """The first lines as the agent should see them (Excel rows joined with ' | ')."""
    if Path(filename).suffix.lower() in EXCEL:
        return "\n".join(" | ".join(r) for r in rows_of(filename, data)[:lines])
    return "\n".join(_decode(data).splitlines()[:lines])


# ---------- numbers and dates ----------

def number(raw: str, dezimal: str = ".") -> Decimal | None:
    """'1'234.50', '-1.234,50', '(12.00)', '12.00-', 'CHF 5.00' → Decimal; empty → None."""
    t = re.sub(r"[\s'’  ]", "", str(raw or ""))
    t = re.sub(r"^[A-Za-z]{3}|[A-Za-z]{3}$", "", t)
    if not t:
        return None
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1]
    if t.endswith("-"):
        negative, t = True, t[:-1]
    if dezimal == ",":
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        value = Decimal(t)
    except InvalidOperation:
        raise BookError(f"Betrag «{raw}» unlesbar") from None
    return -value if negative else value


def to_date(raw: str, fmt: str) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for candidate in (text, re.split(r"[ T]", text)[0]):
        for f in (fmt, "%Y-%m-%d") if fmt else ("%Y-%m-%d",):
            try:
                return datetime.strptime(candidate, f).date()
            except ValueError:
                continue
    return None


# ---------- learned formats ----------

def folder(book: Book) -> Path:
    return book.root / "bank" / "formate"


def formats(book: Book) -> dict[str, dict]:
    out = {}
    for path in sorted(folder(book).glob("*.yaml")) if folder(book).exists() else []:
        spec = read_yaml(path) or {}
        out[path.stem] = {**spec, "_pfad": path}
    return out


def match(book: Book, filename: str, data: bytes, nur_bestaetigt: bool = True) -> tuple[str, dict] | None:
    """The stored format whose header row this file contains."""
    if Path(filename).suffix.lower() not in EXCEL + TEXT:
        return None
    for name, spec in formats(book).items():
        if nur_bestaetigt and not spec.get("bestaetigt"):
            continue
        try:
            rows = rows_of(filename, data, spec.get("trennzeichen") or "")
        except (BookError, csv.Error, ValueError):
            continue
        if find_header(rows, spec.get("kopfzeile") or []) is not None:
            return name, spec
    return None


def _columns(spec: dict, header: list[str]) -> dict[str, int]:
    index = {_norm(h): i for i, h in enumerate(header)}
    cols = {}
    for key in FIELDS:
        name = spec.get(key)
        if not name:
            continue
        if _norm(name) not in index:
            raise BookError(f"Format {spec.get('name')}: Spalte «{name}» ({key}) fehlt in der Kopfzeile")
        cols[key] = index[_norm(name)]
    texts = spec.get("text") or []
    for name in [texts] if isinstance(texts, str) else texts:
        if _norm(name) not in index:
            raise BookError(f"Format {spec.get('name')}: Spalte «{name}» (text) fehlt in der Kopfzeile")
    if "datum" not in cols:
        raise BookError(f"Format {spec.get('name')}: Spalte für das Datum fehlt")
    if "betrag" not in cols and not ({"belastung", "gutschrift"} & set(cols)):
        raise BookError(f"Format {spec.get('name')}: Spalte für den Betrag (oder Belastung/Gutschrift) fehlt")
    return cols


def _ibans(rows: list[list[str]]) -> set[str]:
    from . import qrbill_ch as qr
    found = set()
    for row in rows:
        for cell in row:
            for m in re.finditer(r"\b(?:CH|LI)\d{2}(?:\s?[0-9A-Z]){17}\b", cell.upper()):
                iban = qr.normalize_iban(m.group(0))
                if not qr.iban_problem(iban):
                    found.add(iban)
    return found


def read_with(book: Book, name: str, spec: dict, filename: str, data: bytes) -> tuple[list[dict], dict]:
    """Statements (shaped like bank.parse) and a check report: rows read, rows skipped,
    running-balance mismatches."""
    from . import bank
    rows = rows_of(filename, data, spec.get("trennzeichen") or "")
    head = find_header(rows, spec.get("kopfzeile") or [])
    if head is None:
        raise BookError(f"Format {spec.get('name') or name}: Kopfzeile nicht gefunden")
    cols = _columns(spec, rows[head])
    texts = spec.get("text") or []
    texts = [texts] if isinstance(texts, str) else texts
    text_cols = [{_norm(h): i for i, h in enumerate(rows[head])}[_norm(t)] for t in texts]
    dezimal = "." if Path(filename).suffix.lower() in EXCEL else (spec.get("dezimal") or ".")
    sign = -1 if int(spec.get("vorzeichen") or 1) < 0 else 1
    fmt = spec.get("datumsformat") or "%d.%m.%Y"

    def cell(row, key):
        i = cols.get(key)
        return row[i] if i is not None and i < len(row) else ""

    entries, skipped = [], []
    seen: dict[tuple, int] = {}
    for n, row in enumerate(rows[head + 1:], head + 2):
        if "betrag" in cols:
            amount = number(cell(row, "betrag"), dezimal)
        else:
            debit, credit = number(cell(row, "belastung"), dezimal), number(cell(row, "gutschrift"), dezimal)
            amount = None if debit is None and credit is None else (credit or ZERO) - abs(debit or ZERO)
        when = to_date(cell(row, "datum"), fmt)
        if when is None or not amount:
            if amount and when is None:
                skipped.append(f"Zeile {n}: Datum «{cell(row, 'datum')}» unlesbar")
            continue
        amount = (amount * sign).quantize(CENT)
        text = " · ".join(row[i] for i in text_cols if i < len(row) and row[i])[:200]
        party = cell(row, "gegenpartei")
        ref = cell(row, "id")
        if not ref:
            key = (when, amount, text, party)
            seen[key] = seen.get(key, 0) + 1
            ref = "h" + hashlib.sha1(f"{when}|{amount}|{text}|{party}|{seen[key]}".encode()).hexdigest()[:12]
        saldo = number(cell(row, "saldo"), dezimal) if "saldo" in cols else None
        entries.append({"datum": when, "betrag": amount, "gegenpartei": party, "referenz_typ": "",
                        "referenz": cell(row, "referenz"), "endtoend": "", "text": text, "bankref": ref,
                        "position": str(n), "_saldo": saldo * sign if saldo is not None else None,
                        "_waehrung": cell(row, "waehrung").upper()})
    if not entries:
        raise BookError(f"Format {spec.get('name') or name}: keine Bewegungen gelesen")
    if entries[0]["datum"] > entries[-1]["datum"]:
        entries.reverse()                       # newest first in the file
    mismatches = []
    last = None
    running = ZERO
    for e in entries:
        running += e["betrag"]
        if e["_saldo"] is None:
            continue
        if last is not None and last + running != e["_saldo"]:
            mismatches.append(f"{e['datum']} {e['betrag']}: Saldo {e['_saldo']} ≠ {last + running}")
        last, running = e["_saldo"], ZERO

    accounts = spec.get("konto") or ""
    by_currency: dict[str, list[dict]] = {}
    for e in entries:
        by_currency.setdefault(e["_waehrung"] or str(spec.get("waehrung_fest") or ""), []).append(e)
    ibans = sorted(_ibans(rows[:head])) if not spec.get("iban") else [str(spec["iban"])]
    statements = []
    for currency, items in by_currency.items():
        konto = str(accounts.get(currency, "") if isinstance(accounts, dict) else accounts)
        iban = ibans[0] if len(ibans) == 1 else ""
        if not konto:
            if not iban:
                raise BookError(f"Format {spec.get('name') or name}: Buchhaltungskonto unbekannt — in "
                                f"bank/formate/{name}.yaml `konto:` setzen (z.B. \"1020\")")
            konto = bank.ledger_account(book, iban)
        saldi = [e["_saldo"] for e in items if e["_saldo"] is not None]
        first_saldo = next((e for e in items if e["_saldo"] is not None), None)
        opening = None
        if first_saldo is not None:
            before = sum((e["betrag"] for e in items[:items.index(first_saldo) + 1]), ZERO)
            opening = first_saldo["_saldo"] - before
        closing = saldi[-1] if saldi and items[-1]["_saldo"] is not None else None
        von, bis = items[0]["datum"].isoformat(), items[-1]["datum"].isoformat()
        for e in items:
            e.pop("_saldo", None)
            e.pop("_waehrung", None)
        statements.append({"id": f"CSV-{name}-{konto}-{von}-{bis}", "konto": konto, "iban": iban,
                           "waehrung": currency, "von": von, "bis": bis, "eroeffnung": opening,
                           "schluss": closing, "schluss_datum": bis if closing is not None else None,
                           "buchungen": items})
    report = {"zeilen": len(entries), "uebersprungen": skipped, "saldo_fehler": mismatches,
              "saldo_geprueft": any(s["schluss"] is not None for s in statements)}
    return statements, report


def propose(book: Book, datei: str, name: str, spec: dict) -> tuple[dict, list[Path]]:
    """Store a format the agent (or a person) described — unconfirmed — after reading the file with it."""
    path = _source(book, datei)
    data = path.read_bytes()
    key = slug(name or "bank")[:40] or "bank"
    clean = {k: v for k, v in spec.items() if v not in (None, "", [], {})}
    clean = {"name": name, "bestaetigt": False, **clean}
    if not clean.get("kopfzeile"):
        raise BookError("kopfzeile fehlt: die Spaltennamen der Kopfzeile genau wie in der Datei")
    statements, report = read_with(book, key, clean, path.name, data)
    target = folder(book) / f"{key}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(target, clean)
    return {"format": key, **summary(statements, report)}, [target]


def confirm(book: Book, name: str) -> list[Path]:
    spec = formats(book).get(name)
    if spec is None:
        raise BookError(f"Format {name} nicht gefunden (bank/formate/)")
    path = spec.pop("_pfad")
    spec["bestaetigt"] = True
    write_yaml(path, spec)
    return [path]


def summary(statements: list[dict], report: dict, n: int = 8) -> dict:
    out = []
    for s in statements:
        moves = s["buchungen"]
        out.append({"konto": s["konto"], "iban": s.get("iban", ""), "waehrung": s.get("waehrung", ""),
                    "von": s["von"], "bis": s["bis"], "eroeffnung": s["eroeffnung"], "schluss": s["schluss"],
                    "bewegungen": len(moves),
                    "eingaenge": sum((e["betrag"] for e in moves if e["betrag"] > 0), ZERO),
                    "ausgaenge": sum((e["betrag"] for e in moves if e["betrag"] < 0), ZERO),
                    "beispiele": [{"datum": e["datum"].isoformat(), "betrag": e["betrag"], "text": e["text"],
                                   "gegenpartei": e["gegenpartei"]} for e in moves[:n]]})
    ok = not report.get("saldo_fehler") and not report.get("uebersprungen")
    return {"auszuege": out, "pruefung": {**report, "ok": ok}}


# ---------- credit card statements ----------

def cards_folder(book: Book) -> Path:
    return book.root / "bank" / "karten"


def card_path(book: Book, data: bytes) -> Path:
    return cards_folder(book) / f"{hashlib.sha256(data).hexdigest()[:16]}.yaml"


def pdf_text(path: Path) -> str:
    from . import erfassung
    text = erfassung.pdf_text(path, max_pages=30)
    if len(text.strip()) < 40:
        text = erfassung.ocr_text(path, max_pages=10)
    return text


def _amount_in(text: str, value: Decimal) -> bool:
    """Whether the amount is printed in the text (1'234.50, 1 234,50, 1.234,50 …, sign ignored)."""
    value = abs(value).quantize(CENT)
    whole, cents = f"{value:.2f}".split(".")
    plain = re.sub(r"(?<![\d.,])\d{1,3}(?:['’\u00a0\u202f ]\d{3})+(?=[.,]\d{2}(?!\d))",
                   lambda m: re.sub(r"\D", "", m.group(0)), text)
    grouped = f"{int(whole):,}".replace(",", ".")
    for w in {whole, grouped}:
        if re.search(rf"(?<![\d.,]){re.escape(w)}[.,]{cents}(?!\d)", plain):
            return True
    return False


def card_checks(card: dict, text: str | None) -> dict:
    """The balance must add up to the cent; every amount must be printed in the statement's text
    (unless a person checked a scan by hand and set `bestaetigt: true`)."""
    lines = card.get("buchungen") or []
    alt, neu = Decimal(str(card.get("saldo_alt") or 0)), Decimal(str(card.get("saldo_neu") or 0))
    total = sum((Decimal(str(b["betrag"])) for b in lines), ZERO)
    problems, unread = [], []
    if alt + total != neu:
        problems.append(f"Saldo geht nicht auf: alt {alt} + Buchungen {total} = {alt + total}, Abrechnung sagt {neu}")
    if text and text.strip():
        missing = [f"{b['datum']} {b['text']} {b['betrag']}" for b in lines
                   if not _amount_in(text, Decimal(str(b["betrag"])))]
        missing += [f"Saldo {v}" for v in (alt, neu) if v and not _amount_in(text, v)]
        if missing:
            unread.append("Nicht im Text der Abrechnung gefunden: " + "; ".join(missing[:8]))
    else:
        unread.append("Die PDF hat keinen Text (Scan): Beträge von Hand prüfen und `bestaetigt: true` setzen")
    if not card.get("bestaetigt"):
        problems += unread
    return {"ok": not problems, "probleme": problems, "summe": total}


def card_propose(book: Book, datei: str, konto: str, saldo_alt, saldo_neu, buchungen: list[dict],
                 herausgeber: str = "", karte: str = "", von: str = "", bis: str = "") -> tuple[dict, list[Path]]:
    path = _source(book, datei)
    acct = book.account(konto)
    if acct.klasse != "passiv":
        raise BookError(f"Konto {konto} ist kein Passivkonto — für die Kreditkarte ein Konto wie "
                        "«2040 Kreditkarte» (klasse passiv) anlegen")
    clean = []
    for i, b in enumerate(buchungen, 1):
        when = to_date(str(b.get("datum") or ""), "%d.%m.%Y")
        if when is None:
            raise BookError(f"Buchung {i}: Datum «{b.get('datum')}» unlesbar (JJJJ-MM-TT)")
        amount = number(str(b.get("betrag")))
        if amount is None:
            raise BookError(f"Buchung {i}: Betrag fehlt")
        item = {"datum": when.isoformat(), "text": str(b.get("text") or "").strip()[:200], "betrag": amount.quantize(CENT)}
        if b.get("original"):
            item["original"] = str(b["original"]).strip()
        clean.append(item)
    if not clean:
        raise BookError("Keine Buchungen")
    data = path.read_bytes()
    card = {"datei": path.name, "sha256": hashlib.sha256(data).hexdigest(), "konto": str(konto),
            "herausgeber": herausgeber, "karte": karte, "von": von or min(b["datum"] for b in clean),
            "bis": bis or max(b["datum"] for b in clean),
            "saldo_alt": number(str(saldo_alt or 0)).quantize(CENT), "saldo_neu": number(str(saldo_neu or 0)).quantize(CENT),
            "bestaetigt": False, "buchungen": clean}
    checks = card_checks(card, pdf_text(path) if path.suffix.lower() == ".pdf" else path.read_text(errors="replace"))
    target = card_path(book, data)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(target, card)
    return {"datei": book.rel(target), "pruefung": checks, "buchungen": len(clean)}, [target]


def card_statements(book: Book, card: dict) -> list[dict]:
    """A checked card statement as a bank statement on the card's liability account:
    charges are money out (negative), payments and credits money in."""
    items = []
    for n, b in enumerate(card["buchungen"], 1):
        amount = -Decimal(str(b["betrag"]))
        text = b["text"] + (f" ({b['original']})" if b.get("original") else "")
        items.append({"datum": to_date(str(b["datum"]), ""), "betrag": amount, "gegenpartei": "",
                      "referenz_typ": "", "referenz": "", "endtoend": "", "text": text[:200],
                      "bankref": f"{card['sha256'][:12]}-{n}", "position": str(n)})
    items.sort(key=lambda e: e["datum"])
    return [{"id": f"KARTE-{card['konto']}-{card['von']}-{card['bis']}", "konto": card["konto"], "iban": "",
             "von": str(card["von"]), "bis": str(card["bis"]),
             "eroeffnung": -Decimal(str(card["saldo_alt"])), "schluss": -Decimal(str(card["saldo_neu"])),
             "schluss_datum": str(card["bis"]), "buchungen": items}]


def load_card(book: Book, data: bytes) -> dict | None:
    path = card_path(book, data)
    return read_yaml(path) if path.exists() else None


# ---------- the BankFormat for the import ----------

def bank_format(book: Book | None, filename: str, data: bytes):
    """A learned format or a read card statement for this file, else None."""
    from .plugins import BankFormat
    if book is None:
        return None
    found = match(book, filename, data)
    if found:
        name, spec = found
        return BankFormat(name=f"csv:{name}", label=f"{spec.get('name') or name} (gelerntes Format)",
                          suffixes=(Path(filename).suffix.lower(),), detect=lambda f, d: True,
                          parse=lambda d, b: read_with(b, name, spec, filename, d)[0])
    card = load_card(book, data) if Path(filename).suffix.lower() == ".pdf" else None
    if card:
        def parse(d, b):
            checks = card_checks(card, _text_of(d))
            if not checks["ok"]:
                raise BookError("Kreditkartenabrechnung nicht geprüft: " + " · ".join(checks["probleme"])
                                + f" — {book.rel(card_path(b, d))} korrigieren")
            return card_statements(b, card)
        return BankFormat(name="karte", label=f"Kreditkarte {card.get('herausgeber') or ''}".strip(),
                          suffixes=(".pdf",), detect=lambda f, d: True, parse=parse)
    return None


def _text_of(data: bytes) -> str:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="batzen-karte-") as tmp:
        path = Path(tmp) / "abrechnung.pdf"
        path.write_bytes(data)
        return pdf_text(path)


def _source(book: Book, datei: str) -> Path:
    path = Path(datei) if Path(datei).is_absolute() else book.root / datei
    if not path.is_file():
        raise BookError(f"Datei {datei} nicht gefunden")
    return path


# ---------- the agent ----------

def format_prompt(datei: str, konto: str = "") -> str:
    return (f"Die Datei {datei} ist ein Kontoauszug (CSV oder Excel), dessen Format batzen noch nicht kennt.\n"
            f"1. bank_file_preview(\"{datei}\") zeigt die ersten Zeilen.\n"
            "2. Beschreibe das Format mit propose_bank_format: kopfzeile (die Spaltennamen genau wie in der Datei), "
            "die Spalten für datum (mit datumsformat im strftime-Stil, z.B. \"%d.%m.%Y\"), betrag ODER belastung und "
            "gutschrift, text (eine oder mehrere Spalten), wenn vorhanden gegenpartei, referenz, saldo, id und "
            "waehrung; dezimal \".\" oder \",\". Eingänge müssen positiv, Belastungen negativ werden — sonst "
            "vorzeichen -1 (häufig bei Kreditkarten-Exporten).\n"
            + (f"3. Das Buchhaltungskonto ist {konto}.\n" if konto else
               "3. konto: das Bank- oder Kreditkartenkonto aus accounts(); leer lassen, wenn die Datei eine IBAN "
               "enthält, die batzen zuordnen kann, oder wenn du unsicher bist.\n")
            + "4. Prüfe das Ergebnis: pruefung.ok muss true sein (Saldo Zeile für Zeile, keine übersprungenen Zeilen), "
            "die Beispiele müssen plausibel sein. Sonst korrigieren und erneut aufrufen.\n"
            "Buche nichts und importiere nichts — ein Mensch bestätigt das Format (Oberfläche: Bank → «Neu gelesen», "
            "oder `batzen bank format bestaetigen <format>`).")


def card_prompt(datei: str, konto: str) -> str:
    return (f"Die Datei {datei} ist eine Kreditkartenabrechnung. Konto in der Buchhaltung: {konto}.\n"
            f"1. card_statement_text(\"{datei}\") gibt den Text der Abrechnung.\n"
            "2. Übertrage mit propose_card_statement jede Transaktion (datum JJJJ-MM-TT, text = Händler/Beschreibung, "
            "betrag in CHF wie auf der Abrechnung: Belastungen positiv, Zahlungen und Gutschriften negativ; bei "
            "Fremdwährung original z.B. \"EUR 48.00\"), auch Gebühren und Zinsen als eigene Zeilen, dazu saldo_alt "
            "(Saldo der letzten Abrechnung) und saldo_neu (neuer Saldo / zu bezahlender Betrag), herausgeber, karte "
            "(nur die letzten 4 Ziffern), von und bis.\n"
            "3. pruefung.ok muss true sein: saldo_alt + Buchungen = saldo_neu, und jeder Betrag steht im Text. Sonst "
            "die fehlenden oder falschen Zeilen korrigieren und erneut aufrufen.\n"
            "Rechne keine Beträge um und erfinde keine Zeilen. Buche nichts — der Import gleicht danach ab.")


def run_agent(root: Path, prompt: str) -> str:
    """Run the book's agent (the backend chosen in the settings) on one task and wait for it."""
    import html
    import queue
    try:
        from .web import chat
    except ImportError as exc:
        raise BookError(f"Für den Agenten fehlen Pakete ({exc.name}): pip install 'batzen[ui]'") from None
    kind = chat.backend(Book(root).settings.get("agent_backend"))
    if not kind:
        raise BookError("Kein Agent verfügbar (Claude Code, Codex, opencode oder ANTHROPIC_API_KEY)")
    events: queue.Queue = queue.Queue()
    try:
        chat.AGENTS[kind](root).run(events, prompt)
    except Exception as exc:
        raise BookError(f"Agent abgebrochen: {exc}") from None
    said = []
    while not events.empty():
        ev = events.get()
        if ev.get("type") in ("text", "error"):
            markup = re.sub(r"<br\s*/?>|</(?:p|li|tr|h\d)>", "\n", ev.get("html") or "")
            said.append(ev.get("text") or html.unescape(re.sub(r"<[^>]+>", "", markup)))
    return said[-1].strip() if said else ""

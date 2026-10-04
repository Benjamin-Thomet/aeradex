"""Supplier bills from upload to draft: read, assign an account, never book.

A file (PDF, photo, scan) becomes a draft under ``kreditoren/entwuerfe/ENT-NNNN.yaml``.
Two chains fill it, each step only filling what the previous ones left open:

    reading     QR-bill (exact) → plugin readers → PDF text layer → Tesseract OCR
    account     known supplier → Jev (if enabled, above its threshold) → agent

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
from .book import Book, BookError
from .files import CENT, read_yaml, write_yaml

FIELDS = ("lieferant", "name", "strasse", "nr", "plz", "ort", "land", "uid", "iban", "betrag", "waehrung",
          "referenz_typ", "referenz", "mitteilung", "rechnungsnr", "datum", "faellig", "mwst_satz", "mwst_betrag")


# ---------- reading ----------

def read_qr(book: Book, path: Path) -> dict | None:
    from . import kreditoren as kred
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

_AMOUNT = r"(?<![\d.,'])(\d{1,3}(?:['’ ]\d{3})+|\d+)[.,](\d{2})(?!\d)"
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
            out.append(Decimal(re.sub(r"['’ ]", "", whole) + "." + cents))
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
    totals = [a for l in lines if _TOTAL.search(l) and not re.search(r"(zwischen|sous-total|subtotal|netto|exkl)", l, re.I)
              for a in amounts(l)]
    if totals:
        out["betrag"] = str(max(totals).quantize(CENT))
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
    for m in re.finditer(r"\b(CH|LI)\s?(\d{2})((?:\s?[0-9A-Z]{4}){4}\s?[0-9A-Z])\b", text):
        iban = qr.normalize_iban(m.group(0))
        if qr.iban_is_valid(iban):
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
                town = re.match(r"^(?:CH-)?(\d{4})\s+([A-Za-zÀ-ÿ][\w .\-'’]*)$", nxt)
                if street and "strasse" not in out:
                    out["strasse"], out["nr"] = street.group(1), street.group(2).replace(" ", "")
                elif town:
                    out["plz"], out["ort"] = town.group(1), town.group(2)
                    break
            break
    return out


# ---------- the draft ----------

def folder(book: Book) -> Path:
    return book.root / "kreditoren" / "entwuerfe"


def text_cache(book: Book, draft_id: str) -> Path:
    return book.root / ".batzen" / "erfassung" / f"{draft_id}.txt"


def drafts(book: Book) -> dict[str, dict]:
    f = folder(book)
    out = {}
    for path in sorted(f.glob("ENT-*.yaml")) if f.exists() else []:
        meta = read_yaml(path) or {}
        meta["_pfad"] = path
        out[str(meta.get("id") or path.stem)] = meta
    return out


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
    """Account assignment: known supplier → Jev. Leaves `konto` empty when unsure."""
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
    konto = (meta.get("konto") or {}).get("wert")
    if konto and not (meta.get("mwst") or {}).get("wert"):
        code = mwst_code(book, konto, value(meta, "mwst_satz"))
        if code:
            meta["mwst"] = {"wert": code, "quelle": f"Satz {value(meta, 'mwst_satz')} % + Konto"}
    meta["status"] = status(meta)


def status(meta: dict) -> str:
    has = lambda k: bool(value(meta, k))  # noqa: E731
    if not (meta.get("konto") or {}).get("wert") and meta.get("status") == "agent":
        return "agent"
    if meta.get("konflikt"):
        return "konflikt"             # stays until a person decides, even with an account
    if not (meta.get("konto") or {}).get("wert"):
        return "unsicher"
    if not has("betrag") or not (has("iban") or has("lieferant")):
        return "unvollstaendig"
    return "bereit"


def create(book: Book, datei: str, fields: dict, text: str, notes: list[str]) -> tuple[dict, list[Path]]:
    path = Path(datei) if Path(datei).is_absolute() else book.root / datei
    if not path.is_file():
        raise BookError(f"Datei {datei} nicht gefunden")
    rel = str(path.resolve().relative_to(book.root.resolve())) if path.resolve().is_relative_to(book.root.resolve()) else str(path)
    if any(d.get("datei") == rel for d in drafts(book).values()):
        raise BookError(f"Für {rel} gibt es schon einen Entwurf")
    draft_id = next_id(book)
    meta = {"id": draft_id, "datei": rel, "erstellt": date.today().isoformat(), "felder": fields,
            "hinweise": list(notes), "status": "neu"}
    kontieren(book, meta, text)
    target = folder(book) / f"{draft_id}.yaml"
    write_yaml(target, meta)
    cache = text_cache(book, draft_id)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return meta, [target]


def update(book: Book, draft_id: str, quelle: str, konto: str = "", mwst: str | None = None,
           begruendung: str = "", **fields) -> tuple[dict, list[Path]]:
    meta = draft(book, draft_id)
    path = meta.pop("_pfad")
    if konto:
        acct = book.account(konto)
        if acct.klasse not in ("aufwand", "aktiv"):
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
        if val not in (None, ""):
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
    return out


def needs_agent(meta: dict) -> bool:
    return not (meta.get("konto") or {}).get("wert") and meta.get("status") != "agent"


def agent_auto(book: Book) -> bool:
    """Drafts nobody could account (no known supplier, Jev unsure or off) go to the agent by themselves,
    unless the book says otherwise (batzen.yaml → kreditoren: {agent_automatisch: false})."""
    return bool((book.settings.get("kreditoren") or {}).get("agent_automatisch", True))


def agent_prompt(meta: dict, book: Book) -> str:
    known = {k: v["wert"] for k, v in (meta.get("felder") or {}).items()}
    return (f"Kontiere den Kreditoren-Entwurf {meta['id']} (Lieferantenrechnung, Datei {meta['datei']} im Buch).\n"
            f"Bisher erkannt: {known or 'nichts'}.\n"
            + (f"Hinweise: {'; '.join(meta.get('hinweise') or [])}.\n" if meta.get("hinweise") else "")
            + "Vorgehen: bill_draft() zeigt Entwurf und erkannten Text; lies bei Bedarf die Datei. Wähle mit accounts() "
            "das passende Aufwandkonto (bei Anschaffungen ein Aktivkonto) und, falls die Rechnung MWST ausweist und das "
            "Buch effektiv abrechnet, den Vorsteuer-Code. Trage alles mit complete_bill_draft ein: konto, begruendung "
            "(ein Satz, warum), und fehlende oder falsch erkannte Felder (betrag, datum, faellig, rechnungsnr, name, iban). "
            "Erfasse die Rechnung NICHT selbst und buche nichts — ein Mensch prüft den Entwurf.")


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

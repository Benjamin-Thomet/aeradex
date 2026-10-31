"""Paper reports filled in by hand on the building site, scanned and read into the book:

- Regie-Rapport (work done for a customer by the hour: what was done, hours per person and day) and
  Material-Rapport (material used: date, material, unit, quantity) → time and material entries for the customer
  and its object, collected in the customer's invoice draft (entwuerfe.py);
- Wochenrapport (one person's week: per day travel time and hours per customer/object) → that person's working time,
  checked against the Regie-Rapporte (woche below).

A scan is read by Claude (page images, a fixed JSON schema per form) — never booked directly: the reading waits in
`leistungen/rapporte/<nr>.yaml` (status «ausgelesen») next to the scan until a person checked it and takes it over.
Forms printed by aeradex (formulare.py) carry a QR code `aeradex:rapport:<art>:<nr>[:<kunde>[:<objekt>]]` (Wochenrapport:
`aeradex:rapport:woche:<JJJJ>-W<KW>:<person>`) that names the form, its number and the customer, so nothing has to be
guessed from handwriting.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aeradex import api, invoices
from aeradex.book import Book, BookError
from aeradex.files import parse_date, read_yaml, write_yaml

from . import daten

ARTEN = {"regie": "Regie-Rapport", "material": "Material-Rapport", "woche": "Wochenrapport"}
PREFIX = {"regie": "R", "material": "M", "woche": "W"}
QR_PREFIX = "aeradex:rapport:"
STATUS = ("ausgelesen", "uebernommen", "verworfen")
MODEL_ENV = "AERADEX_RAPPORT_MODEL"
ZERO = Decimal("0")


def folder(book: Book) -> Path:
    return book.root / "leistungen" / "rapporte"


def reports(book: Book) -> dict[str, dict]:
    out = {}
    for path in sorted(folder(book).glob("*.yaml")) if folder(book).exists() else []:
        if path.name == "nummern.yaml":
            continue
        out[path.stem] = {"nummer": path.stem, **(read_yaml(path) or {}), "_pfad": path}
    return out


def report(book: Book, nr: str) -> dict:
    found = reports(book).get(str(nr).strip())
    if found is None:
        raise BookError(f"Rapport {nr} nicht gefunden")
    return found


def to_check(book: Book) -> list[dict]:
    """Read but not taken over yet: waiting for a person."""
    return [r for r in reports(book).values() if r.get("status") == "ausgelesen"]


# ---------- numbers and QR ----------

def _numbers_path(book: Book) -> Path:
    return folder(book) / "nummern.yaml"


def reserve(book: Book, art: str, jahr: int, anzahl: int = 1) -> tuple[list[str], list[Path]]:
    """The next free numbers of a kind (R-2027-014 …) for printed forms; the high-water mark is kept so a number
    printed once is never handed out again."""
    if art not in ("regie", "material"):
        raise BookError("Nummern gibt es für Regie- und Material-Rapporte")
    path = _numbers_path(book)
    marks = (read_yaml(path) or {}) if path.exists() else {}
    key = f"{PREFIX[art]}-{jahr}"
    used = [int(m.group(1)) for k in reports(book) if (m := re.fullmatch(rf"{key}-(\d+)", k))]
    start = max([int(marks.get(key) or 0), *used]) + 1
    numbers = [f"{key}-{n:03d}" for n in range(start, start + max(1, int(anzahl)))]
    marks[key] = start + len(numbers) - 1
    folder(book).mkdir(parents=True, exist_ok=True)
    write_yaml(path, {k: marks[k] for k in sorted(marks)})
    return numbers, [path]


def qr_payload(art: str, nr: str, kunde: str = "", projekt: str = "") -> str:
    return QR_PREFIX + ":".join(x for x in (art, nr, kunde, projekt) if x)


def parse_qr(text: str) -> dict | None:
    """`aeradex:rapport:regie:R-2027-014:K0003:P0007` → {art, nummer, kunde, projekt};
    `aeradex:rapport:woche:2027-W05:M0002` → {art, jahr, kw, person}."""
    text = (text or "").strip()
    if not text.startswith(QR_PREFIX):
        return None
    parts = text[len(QR_PREFIX):].split(":")
    art = parts[0]
    ident = re.compile(r"[A-Z]+\d+")               # M0002, K0003, P0007: nothing else is ever taken from paper
    if art == "woche" and len(parts) >= 3 and (m := re.fullmatch(r"(\d{4})-W(\d{1,2})", parts[1])) \
            and ident.fullmatch(parts[2]) and 1 <= int(m.group(2)) <= 53:
        return {"art": art, "jahr": int(m.group(1)), "kw": int(m.group(2)), "person": parts[2]}
    if art in ("regie", "material") and len(parts) >= 2 and re.fullmatch(rf"{PREFIX[art]}-\d{{4}}-\d{{3,}}", parts[1]):
        kunde = parts[2] if len(parts) > 2 and ident.fullmatch(parts[2]) else ""
        projekt = parts[3] if len(parts) > 3 and ident.fullmatch(parts[3]) else ""
        return {"art": art, "nummer": parts[1], "kunde": kunde, "projekt": projekt}
    return None


_QR_SEEN: dict[tuple, dict | None] = {}


def qr_of(path: Path) -> dict | None:
    """The report QR on a scan (decoded once per file: every report kind asks when a file comes in)."""
    from aeradex import kreditoren
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _QR_SEEN:
        try:
            payloads = kreditoren._decode_images(path)
        except Exception:                       # no QR reader, a broken image: then the person says what it is
            payloads = []
        if len(_QR_SEEN) > 64:
            _QR_SEEN.clear()
        _QR_SEEN[key] = next((q for p in payloads if (q := parse_qr(p))), None)
    return _QR_SEEN[key]


# ---------- reading (Claude, page images, fixed schema) ----------

def _num_or_text(desc: str) -> dict:
    return {"type": "string", "description": desc}


_FLAG = {"type": "boolean", "description": "true, wenn die Handschrift unklar ist oder der Wert geraten wurde"}
_HEAD = {"type": "object", "additionalProperties": False, "required": ["nr", "auftraggeber", "adresse", "baustelle"],
         "properties": {"nr": {"type": "string"}, "auftraggeber": {"type": "string"},
                        "adresse": {"type": "string"}, "baustelle": {"type": "string"}}}
SCHEMAS = {
    "regie": {
        "type": "object", "additionalProperties": False,
        "required": ["kopf", "arbeiten", "stunden", "total_chf_auf_blatt", "unterschrift_firma",
                     "unterschrift_auftraggeber", "hinweise"],
        "properties": {
            "kopf": _HEAD,
            "arbeiten": {"type": "array", "items": {"type": "string"},
                         "description": "Jede Zeile unter «Ausgeführte Arbeiten» wörtlich"},
            "stunden": {"type": "array", "description": "Eine Zeile je Person und Datum mit Stunden",
                        "items": {"type": "object", "additionalProperties": False,
                                  "required": ["person", "datum", "stunden", "ansatz", "unsicher"],
                                  "properties": {"person": {"type": "string"},
                                                 "datum": _num_or_text("JJJJ-MM-TT"),
                                                 "stunden": _num_or_text("Dezimal mit Punkt, z.B. 7.5"),
                                                 "ansatz": _num_or_text("Ansatz CHF, falls eingetragen, sonst leer"),
                                                 "unsicher": _FLAG}}},
            "total_chf_auf_blatt": _num_or_text("Total CHF wie auf dem Blatt, sonst leer"),
            "unterschrift_firma": {"type": "boolean"}, "unterschrift_auftraggeber": {"type": "boolean"},
            "hinweise": {"type": "string", "description": "Was unleserlich oder auffällig ist, sonst leer"}}},
    "material": {
        "type": "object", "additionalProperties": False,
        "required": ["kopf", "zeilen", "total_chf_auf_blatt", "unterschrift_firma", "unterschrift_auftraggeber",
                     "hinweise"],
        "properties": {
            "kopf": _HEAD,
            "zeilen": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["datum", "material", "einheit", "menge", "preis", "unsicher"],
                "properties": {"datum": _num_or_text("JJJJ-MM-TT"), "material": {"type": "string"},
                               "einheit": {"type": "string"}, "menge": _num_or_text("Dezimal mit Punkt"),
                               "preis": _num_or_text("Preis je Einheit, falls eingetragen, sonst leer"),
                               "unsicher": _FLAG}}},
            "total_chf_auf_blatt": _num_or_text("Total CHF wie auf dem Blatt, sonst leer"),
            "unterschrift_firma": {"type": "boolean"}, "unterschrift_auftraggeber": {"type": "boolean"},
            "hinweise": {"type": "string"}}},
    "woche": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "kw", "jahr", "tage", "total_auf_blatt", "visum", "hinweise"],
        "properties": {
            "name": {"type": "string"}, "kw": _num_or_text("Kalenderwoche"), "jahr": _num_or_text("Jahr"),
            "tage": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["wochentag", "datum", "reisezeit", "zeilen"],
                "properties": {"wochentag": {"type": "string"}, "datum": _num_or_text("JJJJ-MM-TT"),
                               "reisezeit": _num_or_text("Stunden Reisezeit dezimal, sonst leer"),
                               "zeilen": {"type": "array", "items": {
                                   "type": "object", "additionalProperties": False,
                                   "required": ["kunde_objekt", "stunden", "bemerkung", "unsicher"],
                                   "properties": {"kunde_objekt": {"type": "string"},
                                                  "stunden": _num_or_text("Dezimal mit Punkt"),
                                                  "bemerkung": {"type": "string"}, "unsicher": _FLAG}}}}}},
            "total_auf_blatt": _num_or_text("Total Stunden wie auf dem Blatt, sonst leer"),
            "visum": {"type": "boolean"}, "hinweise": {"type": "string"}}},
}

_FORMS = {
    "regie": "einen Regie-Rapport eines Malergeschäfts: Kopf (Nr., Auftraggeber, Adresse, Baustelle), «Ausgeführte "
             "Arbeiten» (Zeilen), darunter eine Tabelle mit einer Zeile je Person (Name vorgedruckt oder von Hand) und "
             "Spalten je Datum (Datum steht in der Kopfzeile der Spalte) mit Stunden, dann Total, Ansatz, Betrag",
    "material": "einen Material-Rapport eines Malergeschäfts: Kopf (Nr., Auftraggeber, Adresse, Baustelle) und eine "
                "Tabelle Datum, Material, Einheit, Menge, Preis, Betrag",
    "woche": "einen Wochenrapport einer Person: Name, Woche (Kalenderwoche), je Wochentag eine Zeile «Reisezeit» "
             "mit Stunden und darunter Zeilen Kunde/Objekt mit Stunden und Bemerkung (z.B. Ferien, krank, Schule)",
}


def _prompt(art: str, hints: dict) -> str:
    lines = [f"Das Bild zeigt {_FORMS[art]}. Lies das von Hand Geschriebene genau ab und gib es im verlangten JSON "
             "zurück. Erfinde nichts: leere Felder bleiben leer, unleserliche Werte bestmöglich lesen und "
             "unsicher=true setzen. Zahlen mit Punkt als Dezimaltrennzeichen (7½ → 7.5, 7:30 → 7.5). "
             f"Daten als JJJJ-MM-TT; fehlt das Jahr, gilt {hints.get('jahr')}."]
    if hints.get("personen"):
        lines.append("Mitarbeitende (Namen so schreiben, wenn erkennbar): " + ", ".join(hints["personen"]))
    if hints.get("kunden") and art != "woche":
        lines.append("Bekannte Kunden/Objekte: " + "; ".join(hints["kunden"][:60]))
    if hints.get("objekte") and art == "woche":
        lines.append("Bekannte Kunden/Objekte: " + "; ".join(hints["objekte"][:80]))
    if hints.get("material") and art == "material":
        lines.append("Material im Sortiment: " + "; ".join(hints["material"][:80]))
    if hints.get("woche"):
        lines.append(f"Die Woche ist {hints['woche']}.")
    return "\n".join(lines)


def page_images(path: Path, max_pages: int = 2, long_edge: int = 1600) -> list[tuple[str, bytes]]:
    """(media type, bytes) per page: a PDF rendered to PNG, a photo as it is."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import io
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        out = []
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            w, h = page.get_size()
            img = page.render(scale=long_edge / max(w, h)).to_pil()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            out.append(("image/png", buf.getvalue()))
        return out
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
             ".gif": "image/gif"}.get(suffix)
    if not media:
        raise BookError(f"{path.name}: als PDF oder Foto (PNG/JPEG) hochladen")
    import io
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(path))   # a phone photo: upright and no larger than needed
    if max(img.size) > long_edge:
        img.thumbnail((long_edge, long_edge))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=88)
    return [("image/jpeg", buf.getvalue())]


def ask_claude(images: list[tuple[str, bytes]], art: str, hints: dict) -> dict:
    """One request: the page images and the form's JSON schema (structured output) → the reading."""
    try:
        import anthropic
    except ImportError:
        raise BookError("Zum Auslesen der Rapporte fehlt das Paket anthropic (pip install 'aeradex[ui]')") from None
    from aeradex.web.chat import _api_credentials
    if not _api_credentials():
        raise BookError("Zum Auslesen braucht es Zugang zur Claude API (ANTHROPIC_API_KEY oder `ant auth login`)")
    content = [{"type": "image", "source": {"type": "base64", "media_type": media,
                                             "data": base64.standard_b64encode(data).decode("ascii")}}
               for media, data in images]
    content.append({"type": "text", "text": _prompt(art, hints)})
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=os.environ.get(MODEL_ENV) or os.environ.get("AERADEX_MODEL") or "claude-opus-5-5",
            max_tokens=16000, output_config={"format": {"type": "json_schema", "schema": SCHEMAS[art]}},
            messages=[{"role": "user", "content": content}])
    except anthropic.APIConnectionError:
        raise BookError("Claude ist nicht erreichbar — später nochmals auslesen") from None
    except anthropic.APIStatusError as exc:
        raise BookError(f"Claude: {exc.message}") from None
    if response.stop_reason == "refusal":
        raise BookError("Claude hat das Auslesen abgelehnt")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise BookError("Claude hat kein lesbares Ergebnis geliefert") from None


READER = ask_claude          # tests replace it; the network is never touched there


def hints(book: Book, art: str, qr: dict | None = None) -> dict:
    people = [p["name"] for p in daten.people(book).values() if p.get("aktiv", True)]
    custs = invoices.customers(book)
    projs = [p for p in daten.projects(book).values() if p.get("status", "offen") == "offen"]
    out = {"jahr": book_year(book), "personen": people,
           "kunden": [invoices.qr.invoice_name(c) for c in custs.values()],
           "objekte": [f"{p['name']} ({invoices.qr.invoice_name(custs.get(p.get('kunde'), {}))})" for p in projs],
           "material": [p["text"] for p in daten.materials(book).values()]}
    if qr and qr.get("art") == "woche":
        out["woche"] = f"KW {qr['kw']}/{qr['jahr']}"
        out["jahr"] = qr["jahr"]
    return out


# ---------- matching ----------

def _norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _score(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b.split() or b in a.split():           # «Lea» for «Lea Muster»
        return 0.85
    return difflib.SequenceMatcher(None, a, b).ratio()


def _best(text: str, options: dict[str, list[str]], cut: float = 0.72) -> str:
    """The key whose labels resemble `text` most, when clearly enough; else ""."""
    scored = sorted(((max(_score(text, l) for l in labels), k) for k, labels in options.items() if labels),
                    reverse=True)
    if not scored or scored[0][0] < cut:
        return ""
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.03 and scored[0][0] < 1:
        return ""                                   # two equally likely: let the person choose
    return scored[0][1]


def match_person(book: Book, name: str) -> str:
    people = daten.people(book)
    return _best(name, {nr: [p["name"], p["name"].split()[0] if p["name"] else ""] for nr, p in people.items()})


def match_object(book: Book, text: str) -> tuple[str, str]:
    """(kunde, projekt) for «Muster AG, Seeweg 2» or «MFH Seeweg»: an object first, else a customer."""
    custs = invoices.customers(book)
    projs = daten.projects(book)
    options = {f"P:{nr}": [p["name"], daten.address_text(p), f"{p['name']} {daten.address_text(p)}"]
               for nr, p in projs.items() if p.get("status", "offen") == "offen"}
    options.update({f"K:{nr}": [invoices.qr.invoice_name(c), c.get("name") or "", c.get("firma") or ""]
                    for nr, c in custs.items()})
    best = _best(text, options, 0.6)
    if best.startswith("P:"):
        return projs[best[2:]]["kunde"], best[2:]
    if best.startswith("K:"):
        return best[2:], ""
    return "", ""


def match_product(book: Book, text: str) -> str:
    return _best(text, {nr: [p["text"]] for nr, p in daten.materials(book).items()}, 0.75)


def amount(value) -> str:
    """«7.5», «7,5», «7½», «7:30» → '7.5'; '' when there is no number."""
    text = str(value or "").strip().replace(",", ".").replace("'", "")
    text = re.sub(r"(\d*)\s*½", lambda m: f"{m.group(1) or '0'}.5", text)   # «7½», «7 ½», «½»
    if m := re.fullmatch(r"(\d+):(\d{2})", text):
        return format((Decimal(m.group(1)) + (Decimal(m.group(2)) / 60).quantize(Decimal("0.01"))).normalize(), "f")
    m = re.search(r"-?\d+(\.\d+)?", text)
    if not m:
        return ""
    try:
        return format(Decimal(m.group(0)).normalize(), "f")
    except InvalidOperation:
        return ""


def book_year(book: Book) -> int:
    """The year a date without one belongs to: this year, or the book's last year when this one is not in the book
    yet (a book that starts next year)."""
    years = book.years()
    return date.today().year if not years or date.today().year in years else max(years)


def _day(value, year: int, years: tuple = ()) -> str:
    """A date as written on the sheet; a day more than a month ahead is last year's (December scanned in January) —
    unless the book itself is already in that year."""
    out = _parse_day(value, year)
    if out and date.fromisoformat(out) > date.today() + timedelta(days=31) \
            and date.fromisoformat(out).year not in years:
        d = date.fromisoformat(out)
        try:
            return d.replace(year=d.year - 1).isoformat()
        except ValueError:
            return out
    return out


def _parse_day(value, year: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return parse_date(text).isoformat()
    except Exception:
        m = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.?\s*(\d{2}|\d{4})?", text)
        if m:
            y = int(m.group(3)) if m.group(3) else year
            try:
                return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1))).isoformat()
            except ValueError:
                return ""
    return ""


def normalise(book: Book, art: str, raw: dict, qr: dict | None = None) -> dict:
    """The reading in the book's terms: persons, customer/object and products matched; numbers as text."""
    year = int((qr or {}).get("jahr") or book_year(book))
    years = tuple(book.years())
    if art in ("regie", "material"):
        kopf = raw.get("kopf") or {}
        kunde, projekt = (qr or {}).get("kunde") or "", (qr or {}).get("projekt") or ""
        if not kunde:
            kunde, projekt = match_object(book, " ".join(x for x in (kopf.get("baustelle"), kopf.get("adresse")) if x))
            if not kunde:
                kunde, projekt = match_object(book, kopf.get("auftraggeber") or "")
        out = {"kopf": kopf, "kunde": kunde, "projekt": projekt,
               "unterschrieben_firma": bool(raw.get("unterschrift_firma")),
               "unterschrieben_kunde": bool(raw.get("unterschrift_auftraggeber")),
               "summe_blatt": amount(raw.get("total_chf_auf_blatt")), "hinweise": raw.get("hinweise") or ""}
        if art == "regie":
            out["arbeiten"] = [a.strip() for a in raw.get("arbeiten") or [] if str(a).strip()]
            out["stunden"] = [{"person": match_person(book, s.get("person")), "gelesen": s.get("person") or "",
                               "datum": _day(s.get("datum"), year, years), "stunden": amount(s.get("stunden")),
                               "ansatz": amount(s.get("ansatz")), "unsicher": bool(s.get("unsicher"))}
                              for s in raw.get("stunden") or [] if amount(s.get("stunden"))]
        else:
            out["zeilen"] = [{"produkt": match_product(book, z.get("material")), "text": (z.get("material") or "").strip(),
                              "einheit": (z.get("einheit") or "").strip(), "datum": _day(z.get("datum"), year, years),
                              "menge": amount(z.get("menge")), "preis": amount(z.get("preis")),
                              "unsicher": bool(z.get("unsicher"))}
                             for z in raw.get("zeilen") or [] if (z.get("material") or "").strip()]
        return out
    person = (qr or {}).get("person") or match_person(book, raw.get("name") or "")
    kw = int((qr or {}).get("kw") or amount(raw.get("kw")).split(".")[0] or 0)
    year = int((qr or {}).get("jahr") or amount(raw.get("jahr")).split(".")[0] or year)
    try:
        monday = date.fromisocalendar(year, kw, 1) if 1 <= kw <= 53 else None
    except ValueError:                              # week 53 in a year with 52
        monday = None
    names = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
    tage = []
    for t in raw.get("tage") or []:
        d = _day(t.get("datum"), year, years)
        wd = _norm(t.get("wochentag"))
        if not d and monday and wd in names:
            d = (monday + timedelta(days=names.index(wd))).isoformat()
        zeilen = []
        for z in t.get("zeilen") or []:
            if not (str(z.get("kunde_objekt") or "").strip() or amount(z.get("stunden"))):
                continue
            kunde, projekt = match_object(book, z.get("kunde_objekt") or "")
            zeilen.append({"text": (z.get("kunde_objekt") or "").strip(), "kunde": kunde, "projekt": projekt,
                           "stunden": amount(z.get("stunden")), "bemerkung": (z.get("bemerkung") or "").strip(),
                           "unsicher": bool(z.get("unsicher"))})
        if d and (zeilen or amount(t.get("reisezeit"))):
            tage.append({"datum": d, "reisezeit": amount(t.get("reisezeit")), "zeilen": zeilen})
    return {"person": person, "gelesen": raw.get("name") or "", "jahr": year, "kw": kw, "tage": tage,
            "summe_blatt": amount(raw.get("total_auf_blatt")), "visum": bool(raw.get("visum")),
            "hinweise": raw.get("hinweise") or ""}


# ---------- taking a scan in ----------

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def take_in(book: Book, path: Path, art: str = "") -> dict:
    """A scanned report from the inbox: QR, reading (slow, outside the write lock), then stored for checking.
    A reading that fails (no API access …) still stores the scan: the person fills in the table by hand."""
    qr = qr_of(path)
    art = art or (qr or {}).get("art") or ""
    if art not in ARTEN:
        raise BookError("Welcher Rapport? Regie, Material oder Woche wählen")
    if qr and qr.get("art") != art:
        qr = None                                   # a QR of another form: rather trust the person's choice
    sha = _sha(path)
    if dup := next((r for r in reports(book).values() if r.get("sha") == sha), None):
        raise BookError(f"Dieser Scan ist schon eingelesen: {dup['nummer']}")
    error = ""
    try:
        raw = READER(page_images(path), art, hints(book, art, qr))
    except BookError as exc:
        raw, error = {}, str(exc)
    except Exception as exc:                        # a broken file, an unexpected answer: keep the scan anyway
        raw, error = {}, f"Nicht lesbar ({type(exc).__name__})"
    data = normalise(book, art, raw, qr) if raw else normalise(book, art, {}, qr)
    res = api.write(book, f"{ARTEN[art]} eingelesen", store, str(book.rel(path)), art, data, qr or {}, sha, error)
    nr = res["ergebnis"]["nummer"]
    return {"url": f"/p/leistungen/rapporte?nr={nr}",
            "meldung": f"{ARTEN[art]} {nr} eingelesen — bitte prüfen" + (f" ({error})" if error else "")}


def store(book: Book, datei: str, art: str, data: dict, qr: dict, sha: str, fehler: str = "") -> tuple[dict, list[Path]]:
    source = book.root / datei
    touched: list[Path] = []
    if art == "woche":                  # one per person and week; a second sheet of the same week gets -2
        base = (f"W-{data['jahr']}-KW{data['kw']:02d}-{data['person']}" if data.get("person") and data.get("kw")
                else f"W-{date.today().isoformat()}")
        nr, n = base, 2
        while (folder(book) / f"{nr}.yaml").exists():
            nr, n = f"{base}-{n}", n + 1
    else:
        nr = qr.get("nummer") or ""
        if not nr:
            numbers, touched = reserve(book, art, book_year(book))
            nr = numbers[0]
        if (folder(book) / f"{nr}.yaml").exists():
            raise BookError(f"Rapport {nr} gibt es schon ({report(book, nr).get('status')}) — "
                            "zweiter Scan desselben Blatts?")
    folder(book).mkdir(parents=True, exist_ok=True)
    target = folder(book) / f"{nr}{source.suffix.lower()}"
    target.write_bytes(source.read_bytes())
    source.unlink()
    meta = {"art": art, "status": "ausgelesen", "eingelesen": date.today().isoformat(), "scan": book.rel(target),
            "sha": sha, **({"fehler": fehler} if fehler else {}), **data}
    write_yaml(folder(book) / f"{nr}.yaml", meta)
    return {"nummer": nr, **meta}, touched + [source, target, folder(book) / f"{nr}.yaml"]


# ---------- checked by a person, taken over ----------

def save(book: Book, nr: str, fields: dict) -> tuple[dict, list[Path]]:
    """The person's corrections of the reading (the table on the check page)."""
    meta = report(book, nr)
    if meta.get("status") != "ausgelesen":
        raise BookError(f"{nr} ist {meta.get('status')}")
    allowed = {"regie": ("kunde", "projekt", "arbeiten", "stunden", "unterschrieben_kunde"),
               "material": ("kunde", "projekt", "zeilen", "unterschrieben_kunde"),
               "woche": ("person", "tage")}[meta["art"]]
    for key in allowed:
        if key in fields:
            meta[key] = fields[key]
    if meta.get("kunde"):
        meta["kunde"] = invoices.customer(book, meta["kunde"])["nummer"]
    if meta.get("projekt"):
        p = daten.project(book, meta["projekt"])
        meta["kunde"] = meta.get("kunde") or p["kunde"]
        if p["kunde"] != meta["kunde"]:
            raise BookError(f"{meta['projekt']} gehört nicht zu {meta['kunde']}")
    path = meta.pop("_pfad")
    meta.pop("nummer", None)
    write_yaml(path, meta)
    return {"nummer": nr, **meta}, [path]


def problems(book: Book, meta: dict) -> list[str]:
    """What still stops the take-over (shown on the check page)."""
    out = []
    art = meta["art"]
    if art in ("regie", "material") and not meta.get("kunde"):
        out.append("Kunde bzw. Objekt wählen")
    if art == "regie":
        rows = meta.get("stunden") or []
        if not rows:
            out.append("Keine Stunden erfasst")
        for i, r in enumerate(rows, 1):
            if not r.get("person"):
                out.append(f"Zeile {i}: Person wählen («{r.get('gelesen')}»)")
            if not r.get("datum"):
                out.append(f"Zeile {i}: Datum fehlt")
            if not _positive(r.get("stunden")):
                out.append(f"Zeile {i}: Stunden fehlen")
    if art == "material":
        rows = meta.get("zeilen") or []
        if not rows:
            out.append("Kein Material erfasst")
        for i, r in enumerate(rows, 1):
            if not r.get("datum"):
                out.append(f"Zeile {i}: Datum fehlt")
            if not _positive(r.get("menge")):
                out.append(f"Zeile {i}: Menge fehlt")
            if not r.get("produkt") and not r.get("einheit"):
                out.append(f"Zeile {i}: Einheit fehlt (neues Material)")
    if art == "woche":
        from .woche_rapport import problems as week_problems
        out += week_problems(book, meta)
    return out


def _positive(value) -> bool:
    text = amount(value)
    return bool(text) and Decimal(text) > 0


def regie_service(book: Book) -> str:
    """The Leistungsart of hours from a Regie-Rapport: «Regiearbeiten» (or the only Leistungsart) when there is one."""
    services = daten.services(book)
    named = [nr for nr, p in services.items() if _norm(p["text"]).startswith("regie")]
    if named:
        return named[0]
    return next(iter(services)) if len(services) == 1 else ""


def rate_or_zero(book: Book, wer: str, kunde: str, projekt: str, leistung: str) -> str:
    """The hourly rate as for any entry; '0' when none is set yet — the invoice draft then asks for it."""
    from .saetze import hourly_rate
    try:
        return str(hourly_rate(book, wer, kunde, projekt, leistung))
    except BookError:
        return "0"


def take_over(book: Book, nr: str) -> tuple[dict, list[Path]]:
    """Regie/Material → entries for the customer (object) and its invoice draft; Woche → working time (woche_rapport)."""
    from . import entwuerfe
    meta = report(book, nr)
    if meta.get("status") != "ausgelesen":
        raise BookError(f"{nr} ist {meta.get('status')}")
    if found := problems(book, meta):
        raise BookError(f"{nr}: " + "; ".join(found))
    touched: list[Path] = []
    ids: list[str] = []
    extra = {}
    if meta["art"] == "regie":
        leistung = regie_service(book)
        text = "; ".join(meta.get("arbeiten") or []) or ARTEN["regie"]
        per_day: dict[tuple, Decimal] = {}
        for r in meta["stunden"]:
            rate = amount(r.get("ansatz")) or rate_or_zero(book, r["person"], meta["kunde"], meta.get("projekt") or "",
                                                           leistung)
            e, paths = daten.add_time(book, r["datum"], r["person"], amount(r["stunden"]), text, meta["kunde"],
                                      meta.get("projekt") or "", True, "", rate, leistung)
            ids.append(e["id"])
            touched += paths
            key = (e["wer"], r["datum"])
            per_day[key] = per_day.get(key, ZERO) + Decimal(amount(r["stunden"]))
        from .woche_rapport import replace_week_entries
        for (person, day), hours in per_day.items():
            touched += replace_week_entries(book, person, parse_date(day), meta["kunde"], meta.get("projekt") or "",
                                            hours)
    elif meta["art"] == "material":
        new: dict[str, str] = {}
        for r in meta["zeilen"]:
            prod = r.get("produkt") or ""
            if not prod:                               # new material: into the range with its unit, price to come
                key = _norm(r["text"])
                prod = new.get(key) or next((nr for nr, p in daten.materials(book).items() if _norm(p["text"]) == key), "")
                if not prod:
                    p, paths = daten.add_product(book, r["text"], amount(r.get("preis")) or "0", r.get("einheit") or "Stk")
                    prod = p["nummer"]
                    touched += paths
                new[key] = prod
            e, paths = daten.add_material(book, r["datum"], meta["kunde"], prod, amount(r["menge"]),
                                          meta.get("projekt") or "", amount(r.get("preis")) or None)
            ids.append(e["id"])
            touched += paths
    else:
        from .woche_rapport import take_over as week
        extra, paths = week(book, meta)
        ids = extra.get("eintraege", [])
        touched += paths
    if meta["art"] in ("regie", "material"):
        draft, paths = entwuerfe.add(book, meta["kunde"], ids, meta.get("projekt") or "", [nr])
        extra["entwurf"] = draft["nummer"]
        touched += paths
    path = meta.pop("_pfad")
    meta.pop("nummer", None)
    meta.update({"status": "uebernommen", "uebernommen": date.today().isoformat(), "eintraege": ids,
                 **{k: v for k, v in extra.items() if k in ("entwurf", "abgleich", "abwesenheiten", "ohne_regie")}})
    write_yaml(path, meta)
    return {"nummer": nr, **extra, "eintraege": ids}, touched + [path]


def discard(book: Book, nr: str) -> tuple[dict, list[Path]]:
    meta = report(book, nr)
    if meta.get("status") != "ausgelesen":
        raise BookError(f"{nr} ist {meta.get('status')}")
    path = meta.pop("_pfad")
    meta.pop("nummer", None)
    meta["status"] = "verworfen"
    write_yaml(path, meta)
    return {"nummer": nr}, [path]

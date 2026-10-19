"""Read a receipt or a credit card statement into lines for the journal grid.

    .aeradex/lokal/auslesen/<ref>/<datei>         the uploaded file (not in git until it is booked)
    .aeradex/lokal/auslesen/<ref>/ergebnis.yaml   what was read: art, datum, zeilen, quelle, hinweise
    .aeradex/lokal/auslesen/<ref>/text.txt        the text the readers found (PDF layer or OCR)

A receipt goes through the Belegeingang's readers (QR, PDF text, OCR) and Jev picks account, total,
date and VAT code — one line. A credit card statement, or a receipt Jev cannot account, goes to the
book's agent, which transcribes every line with `propose_journal_lines` (each amount must be in the
text). Nothing is booked here: the lines land in the grid with the counter account open, a person
chooses how it was paid and books. All lines of one statement become one Beleg with the file.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from .book import Book, BookError
from .files import parse_amount, parse_date, read_yaml, write_yaml

CENT = Decimal("0.01")
ARTEN = {"beleg": "Beleg", "karte": "Kreditkartenabrechnung"}
_CARD = re.compile(r"(?i)kreditkarte|karten-?abrechnung|monatsabrechnung|rechnungsübersicht|"
                   r"visa|mastercard|american express|amex|cembra|viseca|swisscard|cornèrcard|cornercard")
_NAME = re.compile(r"[^\w.\- ]+")


def folder(book: Book, ref: str = "") -> Path:
    if ref and not re.fullmatch(r"[0-9a-f]{16}", ref):
        raise BookError(f"Unbekannter Beleg '{ref}'")
    base = book.root / ".aeradex" / "lokal" / "auslesen"
    return base / ref if ref else base


def file_of(book: Book, ref: str) -> Path:
    files = [p for p in folder(book, ref).glob("*") if p.is_file() and p.name not in ("ergebnis.yaml", "text.txt")]
    if not files:
        raise BookError(f"Die Datei zu {ref} ist nicht mehr da — bitte neu auslesen")
    return files[0]


def text_of(book: Book, ref: str) -> str:
    p = folder(book, ref) / "text.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def result(book: Book, ref: str) -> dict:
    p = folder(book, ref) / "ergebnis.yaml"
    return (read_yaml(p) or {}) if p.exists() else {}


def store(book: Book, filename: str, data: bytes) -> tuple[str, Path]:
    """Keep the upload under its content hash: the same file twice is the same reading."""
    if not data:
        raise BookError("Leere Datei")
    ref = hashlib.sha256(data).hexdigest()[:16]
    name = _NAME.sub("_", Path(filename or "beleg").name).strip() or "beleg"
    target = folder(book, ref) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return ref, target


def is_card_statement(text: str) -> bool:
    """Card issuer words and many dated amount lines: a statement with several purchases."""
    if not _CARD.search(text or ""):
        return False
    dated = re.findall(r"(?m)^.*\b\d{1,2}\.\d{1,2}\.(?:\d{2,4})?\b.*\d[.,]\d{2}\s*$", text)
    return len(dated) >= 2


def read(book: Book, filename: str, data: bytes) -> dict:
    """Read one file. Returns the result, or {"agent": True} when the agent has to read it (`read_with_agent`)."""
    from . import erfassung
    ref, path = store(book, filename, data)
    done = result(book, ref)
    if done.get("zeilen"):
        return {**done, "ref": ref, "datei": path.name, "wieder": True}
    fields, text, notes = erfassung.analyse(book, path)
    (folder(book, ref) / "text.txt").write_text(text or "", encoding="utf-8")
    if is_card_statement(text):
        return {"ref": ref, "datei": path.name, "agent": True, "art": "karte", "hinweise": notes}
    meta = {"art": "quittung", "felder": fields, "hinweise": list(notes)}
    erfassung._match_kreditor(book, meta)          # a known supplier brings its account
    erfassung._jev(book, meta, text)
    konto = (meta.get("konto") or {}).get("wert")
    if konto and not (meta.get("mwst") or {}).get("wert"):
        code = erfassung.mwst_code(book, konto, erfassung.value(meta, "mwst_satz"))
        if code:
            meta["mwst"] = {"wert": code}
    betrag, datum = erfassung.value(meta, "betrag"), erfassung.value(meta, "datum")
    if not (konto and betrag and datum):
        return {"ref": ref, "datei": path.name, "agent": True, "art": "beleg", "hinweise": meta["hinweise"]}
    name = erfassung.value(meta, "name") or Path(path.name).stem
    nr = erfassung.value(meta, "rechnungsnr")
    line = {"datum": datum, "text": f"{name} {nr}".strip(), "konto": konto, "betrag": betrag,
            "mwst": (meta.get("mwst") or {}).get("wert") or ""}
    out = {"art": "beleg", "datum": datum, "zeilen": [line], "quelle": (meta["konto"].get("quelle") or "Leser"),
           "hinweise": meta["hinweise"] + [a["text"] for a in meta.get("abweichungen") or []]}
    write_yaml(folder(book, ref) / "ergebnis.yaml", out)
    return {**out, "ref": ref, "datei": path.name}


def agent_prompt(book: Book, ref: str, art: str) -> str:
    from . import mwst
    text = text_of(book, ref)
    method = mwst.config(book)["methode"]
    vat = ("MWST: Bei einer Quittung mit ausgewiesener MWST den Vorsteuer-Code (V81 Material/Waren, I81 übriger "
           "Aufwand/Investitionen, V26 2.6 %) mitgeben; bei einer Kreditkartenabrechnung keine Codes — sie ist kein "
           "MWST-Beleg (Vorsteuer nur mit der Quittung)." if method == "effektiv" else
           "MWST: keine Codes (das Buch rechnet nicht effektiv ab).")
    kind = ("eine Kreditkartenabrechnung: jede Belastung eine Zeile (datum = Kaufdatum, text = Händler/Beschreibung, "
            "betrag in CHF wie belastet), Gebühren und Zinsen auch; eine Gutschrift/Rückerstattung mit negativem "
            "Betrag. Die Zahlung der letzten Abrechnung und Saldovorträge sind KEINE Zeilen. datum (Parameter) = "
            "Datum der Abrechnung." if art == "karte" else
            "ein Beleg (Quittung, Rechnung): meist eine Zeile mit dem Total; nur aufteilen, wenn Positionen auf "
            "verschiedene Konten gehören (z.B. Material und Verpflegung).")
    return (f"Lies den Beleg {ref} für das Journal aus. Es ist {kind}\n"
            "Konto je Zeile: das passende Aufwandkonto aus accounts() (bei Anschaffungen mehrjähriger Geräte ein "
            "Anlagekonto 15xx); frühere Buchungen mit journal() zeigen, was üblich ist. Das Gegenkonto (womit bezahlt) "
            f"wählt der Mensch. {vat}\n"
            f"Dann propose_journal_lines(ref=\"{ref}\", art=\"{art}\", datum=…, zeilen=[{{datum, text, konto, betrag, "
            "mwst}]). Jeder Betrag muss so im Text stehen; ist pruefung.ok false, korrigieren und erneut aufrufen. "
            "Rechne nichts um, erfinde keine Zeilen, buche nichts.\n\nText des Belegs:\n" + text[:15000])


def read_with_agent(root: Path, ref: str, art: str) -> dict:
    """Let the book's agent transcribe the lines (waits for it)."""
    from .bankformat import run_agent
    book = Book(root)
    file_of(book, ref)
    said = run_agent(root, agent_prompt(book, ref, art))
    out = result(Book(root), ref)
    if not out.get("zeilen"):
        raise BookError("Der Agent hat keine Zeilen geliefert" + (f": {said[:300]}" if said else ""))
    return {**out, "ref": ref, "datei": file_of(Book(root), ref).name}


def propose(book: Book, ref: str, zeilen: list[dict], art: str = "beleg", datum: str = "",
            hinweis: str = "") -> dict:
    """The agent's transcription: checked (accounts, codes, every amount in the text) and kept for the grid."""
    from . import mwst
    from .bankformat import _amount_in
    file_of(book, ref)
    if art not in ARTEN:
        raise BookError(f"art: {', '.join(ARTEN)}")
    if not zeilen:
        raise BookError("Keine Zeilen")
    text = text_of(book, ref)
    lines, problems = [], []
    for i, z in enumerate(zeilen, 1):
        try:
            d = parse_date(z.get("datum") or datum, f"Zeile {i}: datum")
            amount = parse_amount(z.get("betrag"), f"Zeile {i}: betrag")
            konto = str(z.get("konto") or "").strip()
            book.account(konto)
            code = str(z.get("mwst") or "").upper()
            if code and code not in mwst.CODES:
                raise BookError(f"MWST-Code {code} unbekannt")
            if not str(z.get("text") or "").strip():
                raise BookError("text fehlt")
        except (BookError, ValueError) as exc:
            problems.append(f"Zeile {i}: {exc}")
            continue
        if text and not _amount_in(text, amount):
            problems.append(f"Zeile {i}: {amount:.2f} steht nicht im Text")
        lines.append({"datum": d.isoformat(), "text": str(z["text"]).strip(), "konto": konto,
                      "betrag": f"{amount:.2f}", "mwst": code})
    if problems:
        return {"ok": False, "pruefung": {"ok": False, "fehler": problems}}
    d_all = parse_date(datum, "datum").isoformat() if datum else max(l["datum"] for l in lines)
    out = {"art": art, "datum": d_all, "zeilen": lines, "quelle": "Agent",
           "hinweise": [hinweis] if hinweis else []}
    write_yaml(folder(book, ref) / "ergebnis.yaml", out)
    return {"ok": True, "pruefung": {"ok": True}, "zeilen": len(lines)}


def grid_lines(book: Book, res: dict) -> list[dict]:
    """The result as grid rows: a statement is one Beleg on its date (the purchase date goes into the text);
    a hint where the same amount is already booked around that date."""
    card = res.get("art") == "karte"
    out = []
    for z in res.get("zeilen") or []:
        d = date.fromisoformat(z["datum"])
        text = f"{d:%d.%m.} {z['text']}" if card and z["datum"] != res["datum"] else z["text"]
        out.append({"datum": date.fromisoformat(res["datum"] if card else z["datum"]).strftime("%d.%m.%Y"),
                    "text": text, "soll": z["konto"], "haben": "", "betrag": z["betrag"], "mwst": z.get("mwst") or "",
                    "hinweis": duplicate(book, d, Decimal(z["betrag"]))})
    return out


def duplicate(book: Book, d: date, amount: Decimal) -> str:
    """An existing booking with this amount within three days: maybe booked already."""
    amount = abs(amount)
    for r in book.rows:
        if r.betrag == amount and abs((r.datum - d).days) <= 3:
            return f"Gleicher Betrag schon gebucht: Beleg {r.beleg} vom {r.datum:%d.%m.%Y} ({r.text[:60]})"
    return ""


def cleanup(book: Book, refs: set[str]) -> None:
    import shutil
    for ref in refs:
        shutil.rmtree(folder(book, ref), ignore_errors=True)


def stale(book: Book, days: int = 30) -> None:
    """Readings nobody booked: gone after a month."""
    import shutil
    import time
    base = folder(book)
    if not base.exists():
        return
    limit = time.time() - days * 86400
    for d in base.iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)


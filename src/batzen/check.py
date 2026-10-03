"""`batzen check`: everything that must hold for a book to be valid.

Run by every write, by the git pre-commit hook and by agents after any manual
file edit. Errors (fehler) block a commit; warnings (warnung) and hints
(hinweis) do not.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import invoices as inv
from . import payroll
from .book import Book, BookError, Row
from .files import FormatError, parse_date, read_yaml, write_yaml
from .qrbill_ch import iban_problem, normalize_iban

ZERO = Decimal("0")


@dataclass
class Issue:
    level: str        # fehler | warnung | hinweis
    where: str
    message: str

    def as_dict(self) -> dict:
        return {"stufe": self.level, "ort": self.where, "meldung": self.message}

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


def _key(r: Row) -> tuple:
    return (r.datum, r.beleg, r.soll, r.haben, r.betrag, r.quelle)


def run(book: Book) -> list[Issue]:
    issues: list[Issue] = []

    def add(level, where, msg):
        issues.append(Issue(level, where, msg))

    # ---- settings & chart ----
    try:
        s = book.settings
    except FormatError as exc:
        return [Issue("fehler", "batzen.yaml", str(exc))]
    if not s.firma:
        add("fehler", "batzen.yaml", "firma fehlt")
    if s.get("iban") and iban_problem(normalize_iban(s.get("iban"))):
        add("warnung", "batzen.yaml", f"IBAN: {iban_problem(normalize_iban(s.get('iban')))}")
    try:
        accounts = book.accounts
    except FormatError as exc:
        return issues + [Issue("fehler", "kontenplan.yaml", str(exc))]
    for role in ("debitoren", "ertrag", "gewinnvortrag", "jahresergebnis", "bank"):
        if s.konto(role) not in accounts:
            add("warnung", "batzen.yaml", f"Systemkonto {role} = {s.konto(role)} fehlt im Kontenplan")
    opening = sum((a.eroeffnung for a in accounts.values() if a.is_balance_sheet), ZERO)
    if opening:
        add("fehler", "kontenplan.yaml",
            f"Eröffnungsbilanz geht nicht auf: Aktiven + Passiven = {opening} (muss 0 sein)")
    if any(a.eroeffnung for a in accounts.values() if a.is_pl):
        add("fehler", "kontenplan.yaml", "Erfolgskonten dürfen keinen Eröffnungssaldo haben")

    # ---- journal ----
    try:
        rows = book.rows
    except FormatError as exc:
        return issues + [Issue("fehler", "journal", str(exc))]
    by_beleg: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_beleg[r.beleg].append(r)
        expected = f"journal/{r.datum.year}/{r.datum.year}-{r.datum.month:02d}.md"
        if r.file != expected:
            add("fehler", r.where, f"Datum {r.datum} gehört in {expected}")
        if r.datum.year < s.erstes_jahr:
            add("fehler", r.where, f"Datum vor dem ersten Geschäftsjahr {s.erstes_jahr}")
        if not r.beleg:
            add("fehler", r.where, "Belegnummer fehlt")
        if r.betrag <= 0:
            add("fehler", r.where, f"Betrag {r.betrag} muss positiv sein")
        elif r.betrag != r.betrag.quantize(Decimal("0.01")):
            add("fehler", r.where, f"Betrag {r.betrag} hat mehr als zwei Nachkommastellen")
        if not (r.soll or r.haben):
            add("fehler", r.where, "weder Soll- noch Habenkonto")
        for nr in (r.soll, r.haben):
            if nr and nr not in accounts:
                add("fehler", r.where, f"Konto {nr} existiert nicht")
    for beleg, group in by_beleg.items():
        if len({r.datum for r in group}) > 1:
            add("fehler", group[0].where, f"Beleg {beleg} kommt an mehreren Daten vor")
        elif len({r.file for r in group}) > 1 or \
                [r.line for r in group] != list(range(group[0].line, group[0].line + len(group))):
            add("fehler", group[0].where, f"Beleg {beleg} steht an mehreren Stellen — doppelt erfasst?")
        if any(n > 1 for n in Counter((_key(r), r.text) for r in group).values()):
            add("fehler", group[0].where, f"Beleg {beleg} enthält identische Zeilen — doppelt erfasst? "
                "(Absichtlich gleiche Teilbeträge im Text unterscheiden)")
        soll = sum((r.betrag for r in group if r.soll), ZERO)
        haben = sum((r.betrag for r in group if r.haben), ZERO)
        if soll != haben:
            add("fehler", group[0].where, f"Beleg {beleg} nicht ausgeglichen: Soll {soll} ≠ Haben {haben}")

    # ---- posting lock ----
    issues += check_lock(book)

    # ---- invoices ----
    owned = defaultdict(list)
    for r in rows:
        if r.quelle:
            owned[r.quelle].append(r)
    try:
        all_inv = inv.invoices(book)
    except FormatError as exc:
        all_inv = {}
        add("fehler", "rechnungen", str(exc))
    for nr, meta in all_inv.items():
        where = book.rel(meta["_pfad"])
        if not inv.invoice_fingerprint_ok(meta):
            add("fehler", where, "Rechnung wurde nach der Ausstellung verändert (Fingerprint). "
                "Änderungen rückgängig machen; korrigieren nur per Storno oder Gutschrift.")
        expected = inv.booking_rows(meta) if meta.get("status") != "storniert" else []
        actual = owned.get(f"rechnung:{nr}", [])
        if Counter(map(_key, expected)) != Counter(map(_key, actual)):
            add("fehler", where, "Journalbuchung passt nicht zur Rechnung "
                f"(erwartet {len(expected)} Zeile(n), gefunden {len(actual)})")
        settled = owned.get(f"zahlung:{nr}", []) + owned.get(f"gutschrift:{nr}", [])
        total = sum((r.betrag for r in settled), ZERO)
        if meta.get("status") == "storniert" and settled:
            add("fehler", where, "stornierte Rechnung hat Zahlungen/Gutschriften")
        elif total > Decimal(str(meta.get("total") or 0)):
            add("fehler", where, f"Zahlungen {total} übersteigen das Total {Decimal(str(meta.get('total'))):.2f}")
        for r in settled:
            if r.haben != str(meta.get("debitorenkonto")):
                add("fehler", r.where, f"Zahlung zu {nr} muss im Haben auf {meta.get('debitorenkonto')} gehen")
    for quelle, group in owned.items():
        kind, _, ref = quelle.partition(":")
        if kind in ("rechnung", "zahlung", "gutschrift") and ref not in all_inv:
            add("fehler", group[0].where, f"Quelle {quelle}: Rechnung {ref} existiert nicht")

    # ---- payroll ----
    cfg = payroll.config(book)
    try:
        slips = payroll.payslips(book)
    except FormatError as exc:
        slips = []
        add("fehler", "lohn", str(exc))
    known = set()
    for meta in slips:
        where = book.rel(meta["_pfad"])
        quelle = f"lohn:{meta['jahr']}-{int(meta['monat']):02d}:{meta['mitarbeiter']}"
        known.add(quelle)
        actual = owned.get(quelle, [])
        if meta.get("status") == "abgeschlossen":
            if not payroll.payslip_fingerprint_ok(meta):
                add("fehler", where, "abgeschlossene Lohnabrechnung wurde verändert (Fingerprint) — "
                    "mit `batzen payslip reopen` öffnen statt von Hand ändern")
            expected = payroll.booking_rows(meta, cfg) if cfg.get("buchen", True) else []
            if Counter(map(_key, expected)) != Counter(map(_key, actual)):
                add("fehler", where, "Lohnbuchung im Journal passt nicht zur Abrechnung")
        else:
            if actual:
                add("fehler", where, "Entwurf hat Buchungen im Journal")
            add("hinweis", where, "Lohnabrechnung ist noch ein Entwurf")
    for quelle, group in owned.items():
        if quelle.startswith("lohn:") and quelle not in known:
            add("fehler", group[0].where, f"Quelle {quelle}: Lohnabrechnung existiert nicht")
        if quelle.partition(":")[0] not in ("rechnung", "zahlung", "gutschrift", "lohn", "abschluss"):
            add("warnung", group[0].where, f"unbekannte Quelle '{quelle}'")

    # ---- receipts (GeBüV: every booking needs a Beleg) ----
    manual = {r.beleg: r for r in rows if not r.quelle}
    receipts_dir = book.root / "belege"
    names = {p.name for p in receipts_dir.rglob("*") if p.is_file()} if receipts_dir.exists() else set()
    missing = [b for b in manual if not any(n == b or n.startswith(b + " ") for n in names)]
    if missing:
        add("hinweis", "belege", f"{len(missing)} Buchung(en) ohne Beleg-Datei, z.B. {', '.join(sorted(missing)[:5])}")

    inbox = book.root / "inbox"
    pending = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")] if inbox.exists() else []
    if pending:
        add("hinweis", "inbox", f"{len(pending)} unverarbeitete Datei(en) in inbox/")
    return issues


# ---------- posting lock ----------

def locks_path(book: Book) -> Path:
    return book.root / ".batzen" / "locks.yaml"


def _month_hashes(book: Book, until: date) -> dict[str, str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for r in book.rows:
        if r.datum <= until:
            groups[f"{r.datum.year}-{r.datum.month:02d}"].append(
                "|".join([r.datum.isoformat(), r.beleg, r.text, r.soll, r.haben, f"{r.betrag:.2f}", r.quelle]))
    return {m: hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16] for m, lines in sorted(groups.items())}


def check_lock(book: Book) -> list[Issue]:
    lock = book.settings.sperre_bis
    if not lock:
        return []
    path = locks_path(book)
    if not path.exists():
        return [Issue("fehler", "batzen.yaml", f"sperre_bis {lock} gesetzt, aber .batzen/locks.yaml fehlt — "
                      "Sperre nur mit `batzen lock` setzen")]
    data = read_yaml(path)
    if str(data.get("bis")) != lock.isoformat():
        return [Issue("fehler", ".batzen/locks.yaml", f"Sperrdatum {data.get('bis')} ≠ sperre_bis {lock}")]
    stored = data.get("monate") or {}
    current = _month_hashes(book, lock)
    out = []
    for month in sorted(set(stored) | set(current)):
        if stored.get(month) != current.get(month):
            out.append(Issue("fehler", f"journal {month}",
                             "gesperrte Periode wurde verändert — Buchungen bis "
                             f"{lock} dürfen nicht mehr geändert werden (Korrektur als neue Buchung)"))
    return out


def lock(book: Book, until) -> tuple[dict, list[Path]]:
    until = parse_date(until, "bis")
    current = book.settings.sperre_bis
    if current and until < current:
        raise BookError(f"Bereits bis {current} gesperrt. Zurücknehmen nur mit `batzen unlock`.")
    errors = [i for i in run(book) if i.level == "fehler"]
    if errors:
        raise BookError("Buch hat Fehler, Sperre verweigert:\n" + "\n".join(map(str, errors)))
    drafts = [p for p in payroll.payslips(book)
              if p.get("status") != "abgeschlossen" and date(int(p["jahr"]), int(p["monat"]), 1) <= until]
    if drafts:
        raise BookError(f"{len(drafts)} Lohnabrechnung(en) im Sperrzeitraum noch im Entwurf")
    history = read_yaml(locks_path(book)).get("verlauf", []) if locks_path(book).exists() else []
    history.append({"aktion": "sperre", "bis": until.isoformat(), "am": date.today().isoformat()})
    data = {"bis": until.isoformat(), "monate": _month_hashes(book, until), "verlauf": history}
    write_yaml(locks_path(book), data)
    book.settings.data["sperre_bis"] = until.isoformat()
    book.save_settings()
    return data, [locks_path(book), book.root / "batzen.yaml"]


def unlock(book: Book, until, grund: str) -> tuple[dict, list[Path]]:
    if not grund.strip():
        raise BookError("Entsperren braucht einen Grund (wird im Verlauf und in git festgehalten)")
    current = book.settings.sperre_bis
    if not current:
        raise BookError("Die Bücher sind nicht gesperrt")
    new = parse_date(until, "bis") if until else None
    if new and new >= current:
        raise BookError(f"Neues Sperrdatum muss vor {current} liegen")
    data = read_yaml(locks_path(book)) if locks_path(book).exists() else {}
    history = data.get("verlauf", [])
    history.append({"aktion": "entsperrt", "von": current.isoformat(),
                    "bis": new.isoformat() if new else None, "grund": grund,
                    "am": date.today().isoformat()})
    if new:
        data = {"bis": new.isoformat(), "monate": _month_hashes(book, new), "verlauf": history}
        book.settings.data["sperre_bis"] = new.isoformat()
    else:
        data = {"bis": None, "monate": {}, "verlauf": history}
        book.settings.data["sperre_bis"] = None
    write_yaml(locks_path(book), data)
    book.save_settings()
    return data, [locks_path(book), book.root / "batzen.yaml"]

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
    return (r.datum, r.beleg, r.soll, r.haben, r.betrag, r.quelle, r.mwst, r.waehrung, r.fw, r.kurs)


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
    from . import plugins
    for problem in plugins.problems(book):
        add("fehler", "batzen.yaml", problem)
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

    # ---- foreign currencies ----
    for a in accounts.values():
        if a.is_foreign and not a.is_balance_sheet:
            add("fehler", "kontenplan.yaml", f"Konto {a.nr}: Fremdwährung nur für Bilanzkonten")
        if a.eroeffnung_fw and not a.is_foreign:
            add("fehler", "kontenplan.yaml", f"Konto {a.nr}: eroeffnung_fw ohne waehrung")
    for r in rows:
        foreign = [accounts[a] for a in (r.soll, r.haben) if a in accounts and accounts[a].is_foreign]
        if len({a.waehrung for a in foreign}) > 1:
            add("fehler", r.where, "Zeile verbindet zwei verschiedene Fremdwährungen — über CHF aufteilen")
        elif foreign and r.waehrung != foreign[0].waehrung:
            add("fehler", r.where, f"Konto {foreign[0].nr} führt {foreign[0].waehrung}: FW-Betrag und Kurs fehlen")
        if r.waehrung:
            if r.fw is None or r.fw < 0:
                add("fehler", r.where, "FW-Betrag fehlt oder ist negativ")
            elif r.kurs is None or r.kurs <= 0:
                add("fehler", r.where, "Kurs fehlt")
            elif r.fw and abs(r.betrag - (r.fw * r.kurs).quantize(Decimal("0.01"))) > Decimal("0.01"):
                add("fehler", r.where, f"Betrag {r.betrag} passt nicht zu {r.waehrung} {r.fw} × {r.kurs}")
    issues += check_revaluation(book, rows)

    # ---- MWST ----
    issues += check_mwst(book, rows, by_beleg)

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
        settled_all = owned.get(f"zahlung:{nr}", []) + owned.get(f"gutschrift:{nr}", [])
        deb = str(meta.get("debitorenkonto"))
        settled = [r for r in settled_all if r.haben == deb]
        for beleg in {r.beleg for r in settled_all} - {r.beleg for r in settled}:
            add("fehler", where, f"Zahlung/Gutschrift {beleg} zu {nr} geht nicht im Haben auf {deb}")
        total = sum((r.betrag for r in settled), ZERO)
        if meta.get("status") == "storniert" and settled:
            add("fehler", where, "stornierte Rechnung hat Zahlungen/Gutschriften")
        elif total > Decimal(str(meta.get("total") or 0)):
            add("fehler", where, f"Zahlungen {total} übersteigen das Total {Decimal(str(meta.get('total'))):.2f}")
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
    plugin_sources = plugins.sources(book)
    for quelle, group in owned.items():
        if quelle.startswith("lohn:") and quelle not in known:
            add("fehler", group[0].where, f"Quelle {quelle}: Lohnabrechnung existiert nicht")
        if quelle.partition(":")[0] not in ("rechnung", "zahlung", "gutschrift", "lohn", "abschluss", "mwst",
                                             "kreditor", "kzahlung", "bewertung", *plugin_sources):
            add("warnung", group[0].where, f"unbekannte Quelle '{quelle}' (fehlt ein Plugin?)")

    # ---- documents of plugins own their rows like invoices do ----
    for prefix, src in plugin_sources.items():
        try:
            expected = src.rows(book) or {}
        except (BookError, FormatError) as exc:
            add("fehler", prefix, f"{src.label}: {exc}")
            continue
        mine = {q: g for q, g in owned.items() if q.partition(":")[0] == prefix}
        for quelle in sorted(set(expected) | set(mine)):
            want, have = expected.get(quelle, []), mine.get(quelle, [])
            if Counter(map(_key, want)) != Counter(map(_key, have)):
                where = have[0].where if have else prefix
                add("fehler", where, f"{src.label} {quelle.partition(':')[2]}: Journalbuchung passt nicht zum Dokument "
                    f"(erwartet {len(want)} Zeile(n), gefunden {len(have)})")
    for f in plugins.check_findings(book, rows):
        add(f.level if f.level in ("fehler", "warnung", "hinweis") else "fehler", f.where, f.message)

    # ---- kreditoren ----
    from . import kreditoren as kred
    try:
        all_bills = kred.bills(book)
    except FormatError as exc:
        all_bills = {}
        add("fehler", "kreditoren", str(exc))
    for nr, meta in all_bills.items():
        where = book.rel(meta["_pfad"])
        if not kred.bill_fingerprint_ok(meta):
            add("fehler", where, "Kreditor wurde nach der Erfassung verändert (Fingerprint) — stornieren und neu erfassen")
        expected = kred.booking_rows(book, meta) if meta.get("status") != "storniert" else []
        if Counter(map(_key, expected)) != Counter(map(_key, owned.get(f"kreditor:{nr}", []))):
            add("fehler", where, "Journalbuchung passt nicht zum Kreditor")
        konto_k = str(meta.get("kreditorenkonto") or kred.KREDITOREN)
        paid_rows = [r for r in owned.get(f"kzahlung:{nr}", []) if r.soll == konto_k]
        paid = sum(((r.fw or ZERO) if kred.is_foreign(meta) else r.betrag for r in paid_rows), ZERO)
        if meta.get("status") == "storniert" and paid_rows:
            add("fehler", where, "stornierter Kreditor hat Zahlungen")
        elif paid > Decimal(str(meta.get("betrag") or 0)):
            add("fehler", where, f"Zahlungen {paid} übersteigen den Rechnungsbetrag")
    for quelle, group in owned.items():
        kind, _, ref = quelle.partition(":")
        if kind in ("kreditor", "kzahlung") and ref not in all_bills:
            add("fehler", group[0].where, f"Quelle {quelle}: Kreditor {ref} existiert nicht")

    # ---- bank statements ----
    from . import bank
    try:
        bank_rows = bank.transactions(book)
    except FormatError as exc:
        bank_rows = []
        add("fehler", "bank", str(exc))
    belege_set = set(by_beleg)
    statement_belege = set()
    for t in bank_rows:
        if t.get("Status") in ("gebucht", "abgeglichen"):
            if t.get("Beleg") not in belege_set:
                add("fehler", f"bank/{t['_jahr']}.md", f"Bankbewegung {t['ID']}: Beleg {t.get('Beleg')} existiert nicht")
            statement_belege.add(t.get("Beleg"))
    open_count = sum(1 for t in bank_rows if t.get("Status") == "offen")
    if open_count:
        add("hinweis", "bank", f"{open_count} offene Bankbewegung(en) zu verbuchen")
    for rec in bank.reconciliation(book):
        if rec["differenz"]:
            add("warnung", rec["auszug"], f"Kontoauszug {rec['id']}: Schlusssaldo Bank {rec['bank']:.2f}, Buchhaltung "
                f"{rec['buch']:.2f} per {rec['datum']} (+ offen {rec['offen']:.2f}) — Differenz {rec['differenz']:.2f}")

    # ---- receipts (GeBüV: every booking needs a Beleg) ----
    manual = {r.beleg: r for r in rows if (not r.quelle or r.quelle.startswith("kreditor:"))
              and r.beleg not in statement_belege}
    receipts_dir = book.root / "belege"
    names = {p.name for p in receipts_dir.rglob("*") if p.is_file()} if receipts_dir.exists() else set()
    missing = [b for b in manual if not any(n == b or n.startswith(b + " ") for n in names)]
    if missing:
        add("hinweis", "belege", f"{len(missing)} Buchung(en) ohne Beleg-Datei, z.B. {', '.join(sorted(missing)[:5])}")

    from . import mahnungen
    try:
        late = mahnungen.overdue(book)
    except (FormatError, BookError):
        late = []
    if late:
        due = [r["nummer"] for r in late if r["bereit"]]
        add("hinweis", "debitoren", f"{len(late)} Rechnung(en) überfällig"
            + (f", {len(due)} bereit für die nächste Mahnung ({', '.join(due[:4])}{' …' if len(due) > 4 else ''})"
               if due else ""))

    from . import erfassung
    try:
        bill_drafts = erfassung.drafts(book)
    except (FormatError, OSError) as exc:
        bill_drafts = {}
        add("fehler", "kreditoren/entwuerfe", str(exc))
    for d in bill_drafts.values():
        if not (book.root / str(d.get("datei", ""))).is_file():
            add("warnung", book.rel(d["_pfad"]), f"Entwurf {d.get('id')}: Datei {d.get('datei')} fehlt")
    if bill_drafts:
        add("hinweis", "eingang", f"{len(bill_drafts)} Beleg-Entwurf/-Entwürfe im Eingang zu prüfen")
    drafted = {str(d.get("datei", "")) for d in bill_drafts.values()}

    inbox = book.root / "inbox"
    pending = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
               and f"inbox/{p.name}" not in drafted] if inbox.exists() else []
    if pending:
        add("hinweis", "inbox", f"{len(pending)} unverarbeitete Datei(en) in inbox/")
    return issues


def check_revaluation(book: Book, rows: list[Row]) -> list[Issue]:
    from . import fx
    out = []
    owned = defaultdict(list)
    for r in rows:
        if r.quelle.startswith("bewertung:"):
            owned[r.quelle].append(r)
    known = set()
    folder = book.root / "bewertung"
    for path in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        saved = read_yaml(path)
        quelle = f"bewertung:{saved.get('stichtag')}"
        known.add(quelle)
        expected = fx.revaluation_rows(book, saved)
        if Counter(map(_key, expected)) != Counter(map(_key, owned.get(quelle, []))):
            out.append(Issue("fehler", book.rel(path), "Buchung der Fremdwährungsbewertung passt nicht zur gespeicherten Bewertung"))
    for quelle, group in owned.items():
        if quelle not in known:
            out.append(Issue("fehler", group[0].where, f"Quelle {quelle}: Bewertung existiert nicht"))
    foreign = [a for a in book.accounts.values() if a.is_foreign]
    if foreign and rows:
        last_year = max(r.datum.year for r in rows)
        for year in range(book.settings.erstes_jahr, last_year):
            if f"bewertung:{year}-12-31" not in known and any(r.datum.year == year for r in rows):
                out.append(Issue("hinweis", "bewertung", f"Fremdwährungskonten per 31.12.{year} nicht bewertet "
                                 f"(batzen fx bewerten {year}-12-31)"))
    return out


def check_mwst(book: Book, rows: list[Row], by_beleg: dict) -> list[Issue]:
    from . import mwst
    out = []
    cfg = mwst.config(book)
    coded = [r for r in rows if r.mwst]
    if not coded and cfg["methode"] == "keine":
        return out
    tax_accounts = {cfg["konten"]["umsatzsteuer"], cfg["konten"]["vorsteuer"], cfg["konten"]["vorsteuer_inv"],
                    cfg["konten"]["bezugsteuer"]}
    for r in coded:
        if r.mwst not in mwst.CODES:
            out.append(Issue("fehler", r.where, f"Unbekannter MWST-Code {r.mwst}"))
        elif cfg["methode"] == "keine":
            out.append(Issue("warnung", r.where, f"MWST-Code {r.mwst}, aber das Buch ist nicht MWST-pflichtig"))
        elif cfg["methode"] == "saldo" and mwst.CODES[r.mwst].kind in ("vorsteuer", "investition"):
            out.append(Issue("fehler", r.where, "Vorsteuer-Code bei Saldosteuersatzmethode"))
    if cfg["methode"] == "effektiv":
        # Within a Beleg, the tax per code must match the rate on the net amount (± 1 Rappen).
        for beleg, group in by_beleg.items():
            per_code: dict[str, list] = {}
            for r in group:
                if r.mwst in mwst.CODES and mwst.CODES[r.mwst].rate:
                    per_code.setdefault(r.mwst, []).append(r)
            for c, crows in per_code.items():
                if mwst.CODES[c].kind == "bezug":     # owed tax vs the expense; the Vorsteuer row mirrors it
                    tax = sum((x.betrag for x in crows if x.haben in tax_accounts and not x.soll), ZERO)
                    net = sum((x.betrag for x in crows if x.soll and x.haben), ZERO)
                else:
                    tax = sum((x.betrag for x in crows if (x.soll or x.haben) in tax_accounts), ZERO)
                    net = sum((x.betrag for x in crows if (x.soll or x.haben) not in tax_accounts), ZERO)
                expected = mwst.tax_from_net(net, mwst.CODES[c].rate)
                if net and abs(tax - expected) > Decimal("0.02"):
                    out.append(Issue("warnung", crows[0].where,
                                     f"Beleg {beleg}: MWST {c} {tax} passt nicht zu {mwst.CODES[c].rate} % auf {net} (erwartet {expected})"))
        for r in rows:
            if not r.mwst and not r.quelle and (r.soll in tax_accounts or r.haben in tax_accounts):
                out.append(Issue("warnung", r.where, "Buchung auf einem MWST-Konto ohne MWST-Code — "
                                 "erscheint nicht in der MWST-Abrechnung"))
    # Booked Abrechnungen own their rows; a changed period afterwards needs a correction.
    folder = book.root / "mwst"
    owned = defaultdict(list)
    for r in rows:
        if r.quelle.startswith("mwst:"):
            owned[r.quelle].append(r)
    known = set()
    for path in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        saved = read_yaml(path)
        label = str(saved.get("periode") or path.stem)
        known.add(f"mwst:{label}")
        expected = mwst.booking_rows(book, label, saved)
        if Counter(map(_key, expected)) != Counter(map(_key, owned.get(f"mwst:{label}", []))):
            out.append(Issue("fehler", book.rel(path), "Buchung der MWST-Abrechnung passt nicht zur gespeicherten Abrechnung"))
        current = mwst.report(book, label)
        if current["veraendert"]:
            out.append(Issue("warnung", book.rel(path), f"Seit der MWST-Abrechnung {label} wurden Buchungen mit "
                             "MWST-Code geändert — Korrekturabrechnung bei der ESTV nötig"))
    for quelle, group in owned.items():
        if quelle not in known:
            out.append(Issue("fehler", group[0].where, f"Quelle {quelle}: MWST-Abrechnung existiert nicht"))
    return out


# ---------- posting lock ----------

def locks_path(book: Book) -> Path:
    return book.root / ".batzen" / "locks.yaml"


def _month_hashes(book: Book, until: date) -> dict[str, str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for r in book.rows:
        if r.datum <= until:
            groups[f"{r.datum.year}-{r.datum.month:02d}"].append(
                "|".join([r.datum.isoformat(), r.beleg, r.text, r.soll, r.haben, f"{r.betrag:.2f}", r.quelle]
                         + ([r.mwst] if r.mwst else [])
                         + ([r.waehrung, f"{r.fw:.2f}", str(r.kurs)] if r.waehrung else [])))
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

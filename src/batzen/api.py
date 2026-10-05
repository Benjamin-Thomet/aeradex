"""The operations of batzen, shared by the CLI and the MCP server.

Every function takes a Book (or a path), returns plain JSON-able data, and —
for writes — commits its changes to git with a descriptive message. Writes
refuse to start on a book that already has errors, so a bad state is never
compounded; what they write is validated before it touches a file.
"""
from __future__ import annotations

import re
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import check as checks
from . import gitlog, invoices, journal, lohnausweis, payroll, pdf, statements
from .book import Book, BookError, Row
from .files import parse_date, write_yaml
from .ledger import BalanceEngine, account_ledger, resolve_period, trial_balance

DATA = Path(__file__).parent / "data"


# ---------- helpers ----------

def jsonable(value):
    if isinstance(value, float):
        value = Decimal(repr(value))
    if isinstance(value, Decimal):
        if not value:
            value = abs(value)     # never print -0.00
        return f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Row):
        return {"datum": value.datum.isoformat(), "beleg": value.beleg, "text": value.text,
                "soll": value.soll, "haben": value.haben, "betrag": f"{value.betrag:.2f}",
                "quelle": value.quelle, **({"mwst": value.mwst} if value.mwst else {}),
                **({"waehrung": value.waehrung, "fw": f"{value.fw:.2f}", "kurs": str(value.kurs)} if value.waehrung else {})}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def _guard(book: Book) -> None:
    errors = [i for i in checks.run(book) if i.level == "fehler"]
    if errors:
        raise BookError("Das Buch hat Fehler — zuerst beheben (batzen check):\n" +
                        "\n".join(f"  {e}" for e in errors[:10]))


def _done(book: Book, message: str, paths: list[Path], **data) -> dict:
    book.reload()
    errors = [i for i in checks.run(book) if i.level == "fehler"]
    if errors:
        raise BookError("Interner Fehler: Schreibvorgang hat das Buch ungültig gemacht. "
                        "Bitte `git diff` prüfen.\n" + "\n".join(map(str, errors)))
    commit = gitlog.commit(book.root, f"batzen: {message}", paths)
    return jsonable({"ok": True, "meldung": message, "commit": commit,
                     "dateien": sorted({book.rel(p) for p in paths
                                         if Path(p).exists() and Path(p).resolve().is_relative_to(book.root)}),
                     **data})


# ---------- book ----------

def init_book(path: Path, firma: str, jahr: int | None = None, kontenplan: str | None = None,
              rechtsform: str = "GmbH", git: bool = True, **settings) -> dict:
    """A new book. Without `kontenplan` the template follows the legal form:
    AG/GmbH → kmu, Einzelfirma → einzelfirma, Verein → verein."""
    from .book import KONTENPLAN_FUER, Settings
    root = Path(path).resolve()
    if (root / "batzen.yaml").exists():
        raise BookError(f"{root} enthält bereits ein Buch")
    from . import plugins
    kontenplan = kontenplan or KONTENPLAN_FUER[Settings(firma, {"rechtsform": rechtsform}).rechtsform_art]
    templates = plugins.kontenplaene()
    if kontenplan not in templates:
        raise BookError(f"Kontenplan-Vorlage '{kontenplan}' unbekannt (vorhanden: {', '.join(sorted(templates))})")
    template = templates[kontenplan]
    root.mkdir(parents=True, exist_ok=True)
    from .files import read_yaml as _read
    system_overrides = {k: str(v) for k, v in ((_read(template) or {}).get("systemkonten") or {}).items()}
    data = {
        "firma": firma, "rechtsform": rechtsform, "uid": settings.get("uid", ""),
        "adresse": {"strasse": settings.get("strasse", ""), "nr": settings.get("nr", ""),
                    "plz": settings.get("plz", ""), "ort": settings.get("ort", ""), "land": "CH"},
        "telefon": settings.get("telefon", ""), "email": settings.get("email", ""),
        "iban": settings.get("iban", ""), "qr_referenz_praefix": "",
        "waehrung": "CHF", "sprache": "de", "zahlungsfrist_tage": 30,
        "erstes_jahr": int(jahr or date.today().year), "sperre_bis": None,
        # vorschlag: Agenten (MCP) dürfen freie Buchungen nur vorschlagen; direkt: auch buchen.
        "agent_modus": "vorschlag",
        "konten": {"bank": "1020", "debitoren": "1100", "ertrag": "3400", "gutschrift": "3800",
                   "gewinnvortrag": "2970", "jahresergebnis": "2979", "dividende": "2261",
                   "reserve": "2950", **system_overrides},
    }
    write_yaml(root / "batzen.yaml", data)
    shutil.copy(template, root / "kontenplan.yaml")
    for folder in ("journal", "belege", "inbox", "kunden", "rechnungen", "personal", "lohn", "abschluss"):
        (root / folder).mkdir(exist_ok=True)
        if not any((root / folder).iterdir()):
            (root / folder / ".gitkeep").write_text("")
    book = Book(root)
    payroll.write_default_config(book)
    (root / ".gitignore").write_text("berichte/\n.batzen/write.lock\n.DS_Store\n__pycache__/\n")
    agents = (DATA / "BUCH_AGENTS.md").read_text(encoding="utf-8")
    (root / "AGENTS.md").write_text(agents.replace("{firma}", firma), encoding="utf-8")
    claude = root / "CLAUDE.md"
    if not claude.exists():
        claude.write_text("@AGENTS.md\n", encoding="utf-8")
    commit = None
    if git:
        gitlog.init_repo(root)
        commit = gitlog.commit(root, f"batzen: Buch {firma} eröffnet ({data['erstes_jahr']})")
    return jsonable({"ok": True, "meldung": f"Buch {firma} in {root} eröffnet", "buch": root,
                     "firma": firma, "commit": commit})


def status(book: Book) -> dict:
    issues = checks.run(book)
    states = [invoices.invoice_state(book, m) for m in invoices.invoices(book).values()]
    open_inv = [s for s in states if s["status"] in ("offen", "teilbezahlt")]
    inbox = book.root / "inbox"
    year = max(book.years())
    eng = BalanceEngine(book)
    liquid = sum((eng.balance(nr, year) for nr, a in book.accounts.items() if a.gruppe == "fluessige"),
                 Decimal("0"))
    return jsonable({
        "firma": book.settings.firma, "buch": book.root, "jahre": book.years(),
        "mwst_methode": (book.settings.get("mwst") or {}).get("methode") or "keine",
        "sperre_bis": book.settings.sperre_bis, "buchungen": len(book.rows),
        "fluessige_mittel": liquid, "jahresergebnis_laufend": -eng.result(year),
        "offene_rechnungen": len(open_inv), "offen_total": sum((s["offen"] for s in open_inv), Decimal("0")),
        "vorschlaege": len(journal.list_proposals(book)),
        "inbox": sorted(p.name for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")) if inbox.exists() else [],
        "fehler": sum(1 for i in issues if i.level == "fehler"),
        "warnungen": sum(1 for i in issues if i.level == "warnung"),
        "hinweise": [i.as_dict() for i in issues if i.level == "hinweis"],
    })


def run_check(book: Book) -> dict:
    issues = checks.run(book)
    return {"ok": not any(i.level == "fehler" for i in issues), "probleme": [i.as_dict() for i in issues]}


def accounts(book: Book, suche: str = "") -> list[dict]:
    q = suche.lower()
    return [{"konto": a.nr, "name": a.name, "klasse": a.klasse, "gruppe": a.gruppe,
             **({"waehrung": a.waehrung} if a.is_foreign else {})}
            for a in book.accounts.values()
            if a.aktiv_ and (not q or q in a.nr or q in a.name.lower())]


def add_account(book: Book, nr: str, name: str, klasse: str = "", gruppe: str = "", waehrung: str = "") -> dict:
    _guard(book)
    from .book import _klasse_for, Account, KLASSEN
    if nr in book.accounts:
        raise BookError(f"Konto {nr} existiert bereits")
    klasse = klasse or _klasse_for(nr)
    if klasse not in KLASSEN:
        raise BookError(f"klasse muss eine von {', '.join(KLASSEN)} sein")
    waehrung = _currency(waehrung)
    if waehrung != "CHF" and klasse not in ("aktiv", "passiv"):
        raise BookError("Fremdwährung nur für Bilanzkonten (Bank, Debitoren, Kreditoren …)")
    book.accounts[nr] = Account(nr=nr, name=name, klasse=klasse,
                                gruppe=gruppe or statements.default_group(nr, klasse), waehrung=waehrung)
    book.save_accounts()
    return _done(book, f"Konto {nr} {name} angelegt", [book.root / "kontenplan.yaml"])


def _currency(code: str | None) -> str:
    code = (code or "CHF").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise BookError(f"Währung '{code}' ist kein ISO-Code (EUR, USD …)")
    return code


def fx_rate(book: Book, waehrung: str, datum=None) -> dict:
    """BAZG daily rate (CHF per 1 unit) for a day; weekends use the last published day."""
    from . import fx
    d = parse_date(datum, "datum") if datum else date.today()
    return jsonable({"waehrung": _currency(waehrung), "datum": d, "kurs": fx.rate(book, _currency(waehrung), d),
                     "quelle": "BAZG Tageskurse"})


def fx_preview(book: Book, stichtag) -> dict:
    """Foreign-currency accounts valued at the BAZG rate of `stichtag` (nothing is booked)."""
    from . import fx
    d = parse_date(stichtag, "stichtag")
    return jsonable({"stichtag": d, "gebucht": fx.path(book, d).exists(), "konten": fx.preview(book, d),
                     "kreditoren": fx.preview_bills(book, d), "debitoren": fx.preview_invoices(book, d)})


def fx_revalue(book: Book, stichtag) -> dict:
    """Book the revaluation of all foreign-currency accounts at the BAZG rate of `stichtag`."""
    from . import fx
    _guard(book)
    saved, touched = fx.book_revaluation(book, stichtag)
    # net effect on the result: accounts gain when they grow, supplier debts lose when they grow
    total = (sum((Decimal(str(k["differenz"])) for k in saved["konten"]), Decimal(0))
             - sum((Decimal(str(k["differenz"])) for k in saved.get("kreditoren") or []), Decimal(0))
             + sum((Decimal(str(k["differenz"])) for k in saved.get("debitoren") or []), Decimal(0)))
    return _done(book, f"Fremdwährungen per {saved['stichtag']} bewertet (Kurserfolg {total:.2f})", touched,
                 bewertung=saved)


def balances(book: Book, jahr: int | None = None, periode: str = "jahr") -> dict:
    year = jahr or max(book.years())
    start, end = resolve_period(year, periode)
    return jsonable({"jahr": year, "von": start, "bis": end,
                     "konten": trial_balance(book, year, start, end)})


def ledger(book: Book, konto: str, jahr: int | None = None) -> dict:
    return jsonable(account_ledger(book, konto, jahr or max(book.years())))


def journal_rows(book: Book, jahr: int | None = None, monat: int | None = None, beleg: str = "",
                 konto: str = "", suche: str = "", limit: int = 200) -> list[dict]:
    rows = book.rows
    if jahr:
        rows = [r for r in rows if r.datum.year == jahr]
    if monat:
        rows = [r for r in rows if r.datum.month == monat]
    if beleg:
        rows = [r for r in rows if r.beleg == beleg]
    if konto:
        rows = [r for r in rows if konto in (r.soll, r.haben)]
    if suche:
        rows = [r for r in rows if suche.lower() in r.text.lower()]
    return jsonable(rows[-limit:])


def post_entry(book: Book, datum, soll: str, haben: str, betrag, text: str, beleg: str = "",
               datei: str | None = None, mwst: str = "", waehrung: str = "", kurs=None) -> dict:
    _guard(book)
    row, touched = journal.book_entry(book, datum, soll, haben, betrag, text, beleg,
                                      attachment=Path(datei) if datei else None, mwst=mwst or "",
                                      waehrung=waehrung or "", kurs=kurs)
    fw = f" = {row.waehrung} {row.fw:.2f} zu {row.kurs}" if row.waehrung else ""
    return _done(book, f"Beleg {row.beleg} gebucht: {row.text} ({row.soll} an {row.haben} {row.betrag:.2f}{fw})",
                 touched, buchung=row)


def post_split(book: Book, datum, text: str, zeilen: list[dict], beleg: str = "",
               datei: str | None = None, waehrung: str = "", kurs=None) -> dict:
    """A split (Sammel-)Buchung: several lines under one Beleg, each with soll
    and/or haben; the Beleg as a whole must balance."""
    _guard(book)
    d = parse_date(datum, "datum")
    ref = beleg or journal.next_beleg(book, d.year)
    rows = [Row(d, ref, str(z.get("text") or text), str(z.get("soll") or ""), str(z.get("haben") or ""),
                Decimal(str(z["betrag"])), mwst=str(z.get("mwst") or "").upper()) for z in zeilen]
    rows = journal.convert(book, rows, waehrung, kurs)
    touched = journal.post(book, rows, Path(datei) if datei else None)
    return _done(book, f"Beleg {ref} gebucht (Sammelbuchung, {len(rows)} Zeilen): {text}", touched, buchungen=rows)


def reverse_entry(book: Book, beleg: str, datum=None, text: str = "") -> dict:
    _guard(book)
    rows, touched = journal.reverse(book, beleg, datum, text)
    return _done(book, f"Beleg {beleg} storniert mit {rows[0].beleg}", touched, buchungen=rows)


def propose(book: Book, datum, soll, haben, betrag, text, begruendung: str = "", datei: str = "",
            mwst: str = "", bank: str = "", waehrung: str = "", kurs=None) -> dict:
    _guard(book)
    cells, path = journal.propose(book, datum, soll, haben, betrag, text, begruendung, datei=datei or "",
                                  mwst=mwst or "", bank=bank or "", waehrung=waehrung or "", kurs=kurs)
    fw = f"{cells['FW']} " if cells.get("FW") else ""
    return _done(book, f"Vorschlag {cells['ID']}: {cells['Text']} ({cells['Soll']} an {cells['Haben']} {fw}{cells['Betrag']})",
                 [path], vorschlag=cells)


def proposals(book: Book) -> list[dict]:
    return journal.list_proposals(book)


def approve(book: Book, ids: list[str]) -> dict:
    _guard(book)
    rows, touched = journal.approve(book, ids)
    return _done(book, f"Vorschläge freigegeben: {', '.join(ids)} → Beleg {', '.join(sorted({r.beleg for r in rows}))}",
                 touched, buchungen=rows)


def reject(book: Book, ids: list[str]) -> dict:
    found, touched = journal.reject(book, ids)
    return _done(book, f"Vorschläge verworfen: {', '.join(r['ID'] for r in found)}", touched)


# ---------- customers & invoices ----------

def customer_list(book: Book) -> list[dict]:
    return jsonable([{"nummer": k, "name": invoices.qr.invoice_name(c), "ort": (c.get("adresse") or {}).get("ort")}
                     for k, c in invoices.customers(book).items()])


def customer_add(book: Book, **fields) -> dict:
    _guard(book)
    meta, path = invoices.add_customer(book, **fields)
    return _done(book, f"Kunde {meta['nummer']} {meta['firma'] or meta['name']} angelegt", [path], kunde=meta)


def invoice_create(book: Book, kunde: str, positionen: list[dict], datum=None, text: str = "",
                   zahlungsfrist: int | None = None, waehrung: str = "", kurs=None) -> dict:
    _guard(book)
    meta, touched = invoices.issue_invoice(book, kunde, positionen, datum, text, zahlungsfrist, waehrung, kurs)
    meta["_text"] = text
    pdf_path = meta_pdf_path(book, meta)
    pdf_path.write_bytes(pdf.invoice_pdf(book, meta, invoices.customer(book, kunde)))
    return _done(book, f"Rechnung {meta['nummer']} an {meta['an']['name']} über {meta['total']:.2f} ausgestellt",
                 touched + [pdf_path], rechnung=meta, pdf=book.rel(pdf_path))


def meta_pdf_path(book: Book, meta: dict) -> Path:
    return book.root / "rechnungen" / str(parse_date(meta["datum"]).year) / f"{meta['nummer']}.pdf"


def invoice_list(book: Book, status: str = "") -> list[dict]:
    paid = invoices.settlements(book)
    out = [invoices.invoice_state(book, m, paid.get(k, [])) for k, m in invoices.invoices(book).items()]
    return jsonable([s for s in out if not status or s["status"] == status])


def invoice_show(book: Book, nr: str) -> dict:
    meta = invoices.invoice(book, nr)
    return jsonable({**meta, "zustand": invoices.invoice_state(book, meta)})


def invoice_void(book: Book, nr: str, grund: str = "") -> dict:
    _guard(book)
    meta, touched = invoices.void_invoice(book, nr, grund)
    return _done(book, f"Rechnung {nr} storniert" + (f": {grund}" if grund else ""), touched)


def invoice_pay(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None, kurs=None, fw=None) -> dict:
    """Book a payment. `betrag` is in the receiving account's currency; for a foreign invoice paid into
    a CHF account the CHF credited (default: open amount × BAZG rate); `fw` settles only part of it."""
    _guard(book)
    row, touched = invoices.pay_invoice(book, nr, betrag, datum, konto, kurs, fw)
    fwtext = f" ({row.waehrung} {row.fw:.2f})" if row.waehrung else ""
    return _done(book, f"Zahlung {row.betrag:.2f}{fwtext} auf Rechnung {nr} verbucht (Beleg {row.beleg})", touched,
                 buchung=row)


def invoice_credit(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None, grund: str = "") -> dict:
    _guard(book)
    row, touched = invoices.credit_invoice(book, nr, betrag, datum, konto, grund)
    return _done(book, f"Gutschrift {row.betrag:.2f} auf Rechnung {nr} (Beleg {row.beleg})", touched, buchung=row)


def invoice_match(book: Book, betrag, text: str) -> dict:
    found, how = invoices.find_match(book, betrag, text)
    return jsonable({"treffer": found, "grund": how})


def receivables(book: Book, stichtag=None, pdf_out: str | None = None) -> dict:
    ar = invoices.aged_receivables(book, parse_date(stichtag) if stichtag else None)
    out = jsonable(ar)
    if pdf_out is not None:
        out["pdf"] = _write_report(book, pdf_out or None, pdf.receivables_pdf(book, ar), "Offene Debitoren.pdf")
    return out


# ---------- payroll ----------

def employee_list(book: Book) -> list[dict]:
    return jsonable([{"nummer": k, "name": payroll.display_name(e), "lohnart": e.get("lohnart"),
                      "aktiv": e.get("aktiv", True)} for k, e in payroll.employees(book).items()])


def employee_add(book: Book, vorname: str, nachname: str, **fields) -> dict:
    _guard(book)
    meta, path = payroll.add_employee(book, vorname, nachname, **fields)
    return _done(book, f"Mitarbeiter {meta['nummer']} {vorname} {nachname} erfasst", [path], mitarbeiter=meta)


def _ym(monat: str) -> tuple[int, int]:
    y, m = str(monat).split("-")
    return int(y), int(m)


def payroll_run(book: Book, monat: str, mitarbeiter: str | None = None, eingaben: dict | None = None) -> dict:
    _guard(book)
    y, m = _ym(monat)
    results = payroll.run(book, y, m, mitarbeiter, eingaben)
    if not results:
        return {"ok": True, "meldung": "Nichts zu rechnen (alle abgeschlossen oder keine aktiven Mitarbeiter)"}
    return _done(book, f"Lohnlauf {m:02d}/{y}: {len(results)} Abrechnung(en) berechnet",
                 [p for _, p in results], abrechnungen=[
                     {"mitarbeiter": meta["mitarbeiter"], "name": meta.get("name"), "status": meta["status"],
                      "brutto": meta["werte"]["bruttolohn"], "netto": meta["werte"]["nettolohn"]}
                     for meta, _ in results])


def payslip_show(book: Book, monat: str, mitarbeiter: str) -> dict:
    y, m = _ym(monat)
    return jsonable(payroll.load_payslip(book, y, m, mitarbeiter))


def payslip_close(book: Book, monat: str, mitarbeiter: str) -> dict:
    _guard(book)
    y, m = _ym(monat)
    meta, touched = payroll.close(book, y, m, mitarbeiter)
    path = payroll.payslip_path(book, y, m, mitarbeiter).with_suffix(".pdf")
    path.write_bytes(pdf.payslip_pdf(book, meta, payroll.employee(book, mitarbeiter)))
    return _done(book, f"Lohnabrechnung {mitarbeiter} {m:02d}/{y} abgeschlossen und verbucht "
                 f"(netto {meta['werte']['nettolohn']:.2f})", touched + [path], abrechnung=meta,
                 pdf=book.rel(path))


def payslip_reopen(book: Book, monat: str, mitarbeiter: str) -> dict:
    _guard(book)
    y, m = _ym(monat)
    meta, touched = payroll.reopen(book, y, m, mitarbeiter)
    pdf_path = payroll.payslip_path(book, y, m, mitarbeiter).with_suffix(".pdf")
    if pdf_path.exists():
        pdf_path.unlink()
        touched.append(pdf_path)
    return _done(book, f"Lohnabrechnung {mitarbeiter} {m:02d}/{y} wieder geöffnet, Buchung entfernt", touched)


def lohnkonto(book: Book, jahr: int, mitarbeiter: str) -> dict:
    return jsonable(payroll.lohnkonto(book, jahr, mitarbeiter))


def lohnausweis_create(book: Book, jahr: int, mitarbeiter: str) -> dict:
    _guard(book)
    totals = lohnausweis.annual_totals(book, jahr, mitarbeiter)
    if not totals["monate"]:
        raise BookError(f"Keine abgeschlossenen Lohnabrechnungen {jahr} für {mitarbeiter}")
    path = book.root / "lohnausweise" / str(jahr) / f"{mitarbeiter}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(lohnausweis.build_pdf(book, jahr, mitarbeiter))
    warn = f" ({totals['entwuerfe']} Entwurf/Entwürfe nicht berücksichtigt)" if totals["entwuerfe"] else ""
    return _done(book, f"Lohnausweis {jahr} für {mitarbeiter} erstellt{warn}", [path],
                 ziffern=totals, pdf=book.rel(path))


# ---------- reports & closing ----------

def _write_report(book: Book, out: str | None, data: bytes, default: str = "bericht.pdf") -> str:
    path = Path(out) if out else book.root / "berichte" / default
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def statement(book: Book, jahr: int | None = None, pdf_out: str | None = None, als_pdf: bool = False) -> dict:
    year = jahr or max(book.years())
    st = statements.year_end_statement(book, year)
    out = jsonable(st)
    if pdf_out or als_pdf:
        blocks = statements.parse_anhang(statements.anhang(book, year))
        out["pdf"] = _write_report(book, pdf_out, pdf.statement_pdf(book, st, blocks), f"Jahresrechnung {year}.pdf")
    return out


def ledger_pdf(book: Book, konto: str, jahr: int | None = None, pdf_out: str | None = None) -> dict:
    year = jahr or max(book.years())
    led = account_ledger(book, konto, year)
    return {"pdf": _write_report(book, pdf_out, pdf.ledger_pdf(book, led), f"Konto {konto} {year}.pdf")}


def journal_pdf(book: Book, jahr: int | None = None, pdf_out: str | None = None) -> dict:
    year = jahr or max(book.years())
    rows = [r for r in book.rows if r.datum.year == year]
    return {"pdf": _write_report(book, pdf_out, pdf.journal_pdf(book, rows, f"Journal {year}"), f"Journal {year}.pdf")}


# ---------- Abschlussunterlagen (dossier) and the .batzen exchange file ----------

def dossier_overview(book: Book, jahr: int | None = None) -> dict:
    from . import dossier
    year = jahr or max(book.years())
    k = dossier.Kontext(book, year)
    return jsonable({"jahr": year, "entwurf": k.entwurf, "teile": dossier.overview(book, year),
                     "luecken": dossier.belegluecken(book, year)})


def dossier(book: Book, jahr: int | None = None, teile: list[str] | None = None,
            formate: list[str] | None = None, out: str | None = None) -> dict:
    """All Abschlussunterlagen of a year as one ZIP (default: berichte/)."""
    from . import dossier as dos
    year = jahr or max(book.years())
    name, data, manifest = dos.build_zip(book, year, teile or None, formate or None)
    path = _write_report(book, out, data, name)
    gaps = [g for g in manifest["luecken"] if g["art"] != "reserviert"]
    return jsonable({"ok": True, "datei": path, "jahr": year, "entwurf": manifest["entwurf"],
                     "dateien": list(manifest["dateien"]), "luecken": manifest["luecken"],
                     "meldung": f"Abschlussunterlagen {year}: {len(manifest['dateien'])} Dateien"
                                + (" (ENTWURF, Jahr nicht gesperrt)" if manifest["entwurf"] else "")
                                + (f", {len(gaps)} Lücke(n) in den Belegnummern" if gaps else "")})


def dossier_part(book: Book, teil: str, jahr: int | None = None, format: str = "pdf", out: str | None = None) -> dict:
    """One part of the Abschlussunterlagen (several files come as a ZIP)."""
    from . import dossier as dos
    year = jahr or max(book.years())
    name, data = dos.build_part(book, year, teil, format)
    path = _write_report(book, out, data, name)
    return {"ok": True, "datei": path, "meldung": f"{Path(path).name} erstellt"}


def book_export(book: Book, datei: str, passwort: str | None = None, ohne_inbox: bool = False,
                ohne_historie: bool = False) -> dict:
    from . import austausch
    target = Path(datei)
    if not target.is_absolute():
        target = Path.cwd() / target
    return austausch.export_book(book, target, passwort or None, not ohne_inbox, not ohne_historie)


def book_import(datei: str, ziel: str, passwort: str | None = None) -> dict:
    from . import austausch
    return jsonable(austausch.import_book(Path(datei), Path(ziel), passwort or None))


def book_inspect(datei: str, passwort: str | None = None) -> dict:
    from . import austausch
    return jsonable(austausch.inspect(Path(datei), passwort or None))


def allocation_set(book: Book, jahr: int, dividende=0, reserve=0) -> dict:
    _guard(book)
    alloc, touched = statements.set_allocation(book, jahr, dividende, reserve)
    return _done(book, f"Gewinnverwendung {jahr} erfasst: Dividende {Decimal(str(dividende)):.2f}, "
                 f"Reserve {Decimal(str(reserve)):.2f}", touched, gewinnverwendung=alloc)


def allocation_book(book: Book, jahr: int, datum=None) -> dict:
    _guard(book)
    rows, touched = statements.book_allocation(book, jahr, datum)
    return _done(book, f"Gewinnverwendung {jahr} gebucht", touched, buchungen=rows)


def lock(book: Book, bis) -> dict:
    data, touched = checks.lock(book, bis)
    return _done(book, f"Bücher gesperrt bis {data['bis']}", touched, sperre=data)


def unlock(book: Book, bis, grund: str) -> dict:
    data, touched = checks.unlock(book, bis, grund)
    return _done(book, f"Sperre zurückgenommen auf {data['bis'] or '—'}: {grund}", touched)


def history(book: Book, limit: int = 20) -> list[dict]:
    return gitlog.log(book.root, limit)


# ---------- updates (used by the UI; also callable from CLI/MCP) ----------

_ADDRESS_KEYS = ("strasse", "nr", "plz", "ort", "land")


def _update_record(path: Path, fields: dict, numeric: tuple = (), notes: str | None = None,
                   nullable: tuple = ()) -> dict:
    from .files import read_frontmatter, write_frontmatter
    meta, body = read_frontmatter(path)
    adresse = dict(meta.get("adresse") or {})
    for key, value in fields.items():
        if value is None:
            if key in nullable:
                meta[key] = None
            continue
        if key in _ADDRESS_KEYS:
            adresse[key] = str(value)
        elif key in numeric:
            meta[key] = Decimal(str(value or 0))
        else:
            meta[key] = value
    if adresse:
        meta["adresse"] = adresse
    write_frontmatter(path, meta, body if notes is None else notes)
    return meta


def customer_update(book: Book, nummer: str, notizen: str | None = None, **fields) -> dict:
    _guard(book)
    nr = nummer
    cust = invoices.customer(book, nr)
    allowed = {"name", "firma", "rechnung_an", "email", "stundensatz", *_ADDRESS_KEYS}
    unknown = set(fields) - allowed
    if unknown:
        raise BookError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    if fields.get("rechnung_an") not in (None, "firma", "person"):
        raise BookError("rechnung_an muss 'firma' oder 'person' sein")
    meta = _update_record(cust["_pfad"], fields, numeric=("stundensatz",), notes=notizen)
    return _done(book, f"Kunde {nr} geändert", [cust["_pfad"]], kunde=meta)


EMPLOYEE_NUMERIC = ("monatslohn", "pensum", "stundenlohn", "standard_stunden", "vollzeit_stunden_woche",
                    "ferienzuschlag_satz", "bvg_betrag", "ag_bvg_betrag", "kinderzulagen", "qst_satz")


def employee_update(book: Book, nummer: str, **fields) -> dict:
    _guard(book)
    nr = nummer
    emp = payroll.employee(book, nr)
    allowed = {"vorname", "nachname", "ahv_nr", "geburtsdatum", "eintritt", "austritt", "lohnart",
               "ferien_inbegriffen", "qst", "aktiv", *EMPLOYEE_NUMERIC, *_ADDRESS_KEYS}
    unknown = set(fields) - allowed
    if unknown:
        raise BookError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    if fields.get("lohnart") not in (None, "monat", "stunde"):
        raise BookError("lohnart muss 'monat' oder 'stunde' sein")
    for key in ("geburtsdatum", "eintritt", "austritt"):
        if key in fields:
            fields[key] = parse_date(fields[key], key).isoformat() if fields[key] else None
    if "qst" in fields and fields["qst"]:
        q = fields["qst"]
        from . import qst as qstmod
        qstmod.parse_code(q.get("code", ""))
        fields["qst"] = {"kanton": str(q["kanton"]).upper(), "jahr": int(q["jahr"]), "code": q["code"].upper()}
    if "qst" in fields and not fields["qst"]:
        fields["qst"] = None
    meta = _update_record(emp["_pfad"], fields, numeric=EMPLOYEE_NUMERIC, nullable=("qst", "austritt"))
    return _done(book, f"Mitarbeiter {nr} geändert", [emp["_pfad"]], mitarbeiter=meta)


def account_update(book: Book, nr: str, name: str | None = None, gruppe: str | None = None,
                   aktiv: bool | None = None, eroeffnung=None, vorjahr=None,
                   waehrung: str | None = None, eroeffnung_fw=None) -> dict:
    _guard(book)
    acct = book.account(nr)
    if name is not None:
        acct.name = name.strip() or acct.name
    if gruppe:
        if gruppe not in statements.GROUP_LABEL:
            raise BookError(f"Unbekannte Gruppe {gruppe}")
        acct.gruppe = gruppe
    if aktiv is not None:
        acct.aktiv_ = bool(aktiv)
    if waehrung is not None and _currency(waehrung) != acct.waehrung:
        if any(nr in (r.soll, r.haben) for r in book.rows):
            raise BookError(f"Konto {nr} hat Buchungen — Währung nicht mehr änderbar (neues Konto anlegen)")
        if _currency(waehrung) != "CHF" and not acct.is_balance_sheet:
            raise BookError("Fremdwährung nur für Bilanzkonten")
        acct.waehrung = _currency(waehrung)
        if not acct.is_foreign:
            acct.eroeffnung_fw = Decimal(0)
    if eroeffnung_fw is not None:
        if not acct.is_foreign:
            raise BookError(f"Konto {nr} führt keine Fremdwährung")
        eroeffnung = eroeffnung if eroeffnung is not None else acct.eroeffnung
    if eroeffnung is not None or vorjahr is not None:
        lock = book.settings.sperre_bis
        if lock and lock.year >= book.settings.erstes_jahr:
            raise BookError("Eröffnungssalden sind gesperrt (erstes Jahr liegt in der gesperrten Periode)")
        if eroeffnung is not None:
            acct.eroeffnung = Decimal(str(eroeffnung or 0))
        if eroeffnung_fw is not None:
            acct.eroeffnung_fw = Decimal(str(eroeffnung_fw or 0))
        if vorjahr is not None:
            acct.vorjahr = Decimal(str(vorjahr or 0))
    book.save_accounts()
    return _done(book, f"Konto {nr} geändert", [book.root / "kontenplan.yaml"])


SETTINGS_KEYS = ("firma", "rechtsform", "uid", "telefon", "email", "iban", "qr_referenz_praefix",
                 "zahlungsfrist_tage", "agent_modus", "sprache", "co", "zahlungs_iban", "agent_backend")


def settings_update(book: Book, adresse: dict | None = None, konten: dict | None = None,
                    mwst: dict | None = None, bankkonten: dict | None = None, jev: dict | None = None,
                    kreditoren: dict | None = None,
                    **fields) -> dict:
    _guard(book)
    unknown = set(fields) - set(SETTINGS_KEYS)
    if unknown:
        raise BookError(f"Diese Einstellungen sind nicht änderbar: {', '.join(sorted(unknown))}")
    if fields.get("agent_modus") not in (None, "vorschlag", "direkt"):
        raise BookError("agent_modus muss 'vorschlag' oder 'direkt' sein")
    if fields.get("agent_backend") not in (None, "", "auto", "claude-code", "codex", "opencode", "api"):
        raise BookError("Agent: auto, claude-code, codex, opencode oder api")
    data = book.settings.data
    for key, value in fields.items():
        if value is not None:
            data[key] = int(value) if key == "zahlungsfrist_tage" else value
    if adresse:
        data.setdefault("adresse", {}).update({k: str(v) for k, v in adresse.items() if k in _ADDRESS_KEYS})
    if konten:
        for role, nr in konten.items():
            if nr:
                book.account(str(nr))
        data.setdefault("konten", {}).update({k: str(v) for k, v in konten.items() if v})
    if kreditoren is not None:
        current = dict(data.get("kreditoren") or {})
        if "agent_automatisch" in kreditoren:
            current["agent_automatisch"] = bool(kreditoren["agent_automatisch"])
        data["kreditoren"] = current
    if jev is not None:
        current = dict(data.get("jev") or {})
        current["aktiv"] = bool(jev.get("aktiv"))
        if jev.get("schwelle") not in (None, ""):
            value = float(jev["schwelle"])
            if not 0.3 <= value <= 0.99:
                raise BookError("Jev-Schwelle zwischen 0.3 und 0.99")
            current["schwelle"] = value
        if jev.get("modell"):
            current["modell"] = str(jev["modell"])
        data["jev"] = current
    if bankkonten is not None:
        clean = {}
        for iban, nr in bankkonten.items():
            iban = invoices.qr.normalize_iban(iban)
            if not iban:
                continue
            if not invoices.qr.iban_is_valid(iban):
                raise BookError(f"IBAN {iban}: Prüfsumme stimmt nicht")
            book.account(str(nr))
            clean[iban] = str(nr)
        data["bankkonten"] = clean
    if mwst is not None:
        methode = mwst.get("methode") or "keine"
        if methode not in ("keine", "effektiv", "saldo"):
            raise BookError("MWST-Methode: keine, effektiv oder saldo")
        if mwst.get("periode") not in (None, "", "quartal", "semester"):
            raise BookError("MWST-Periode: quartal oder semester")
        current = dict(data.get("mwst") or {})
        current["methode"] = methode
        current["periode"] = mwst.get("periode") or ("semester" if methode == "saldo" else "quartal")
        if mwst.get("saldosteuersatz") not in (None, ""):
            rate = Decimal(str(mwst["saldosteuersatz"]))
            if not (0 <= rate < 20):
                raise BookError("Saldosteuersatz in Prozent angeben, z.B. 6.2")
            current["saldosteuersatz"] = float(rate)
        if methode == "saldo" and not current.get("saldosteuersatz"):
            raise BookError("Für die Saldosteuersatzmethode den bewilligten Satz angeben")
        if mwst.get("taetigkeit") not in (None, ""):
            current["taetigkeit"] = str(mwst["taetigkeit"]).strip()
        if mwst.get("abrechnungsart") in ("vereinbart", "vereinnahmt"):
            current["abrechnungsart"] = mwst["abrechnungsart"]
        previous = (data.get("mwst") or {}).get("methode") or "keine"
        if methode != previous:
            from . import mwst as vat_check
            kinds = {vat_check.CODES[r.mwst].kind for r in book.rows if r.mwst in vat_check.CODES}
            if methode == "saldo" and kinds & {"vorsteuer", "investition"} or methode == "keine" and kinds:
                raise BookError("Im Buch gibt es bereits Buchungen mit MWST-Codes der bisherigen Methode. Ein "
                                "Methodenwechsel gilt erst ab einer neuen Steuerperiode: im neuen Geschäftsjahr "
                                "ein neues Buch eröffnen oder die Codes zuerst stornieren.")
        from . import mwst as vat
        for role, nr in (mwst.get("konten") or {}).items():
            if nr:
                book.account(str(nr))
                current.setdefault("konten", {})[role] = str(nr)
        if methode != "keine":
            for role, nr in {**vat.DEFAULT_KONTEN, **(current.get("konten") or {})}.items():
                if (role != "saldosteuer" or methode == "saldo") and role not in vat.ABGRENZUNG_KONTEN:
                    book.account(nr)          # the Abgrenzung accounts are added when first needed
        data["mwst"] = current
    book.save_settings()
    return _done(book, "Einstellungen geändert", [book.root / "batzen.yaml"])


def payroll_config_update(book: Book, saetze_an: dict | None = None, saetze_ag: dict | None = None,
                          ag_bvg: str | None = None, buchen: bool | None = None, konten: dict | None = None) -> dict:
    _guard(book)
    cfg = payroll.config(book)
    for key, values in (("saetze_an", saetze_an), ("saetze_ag", saetze_ag)):
        for code, rate in (values or {}).items():
            value = Decimal(str(rate))
            if not (0 <= value < 1):
                raise BookError(f"Satz {code} = {rate}: als Anteil angeben, z.B. 0.053 für 5.3 %")
            cfg[key][code] = float(value)
    if ag_bvg is not None:
        if ag_bvg not in ("gleich_an", "betrag"):
            raise BookError("ag_bvg muss 'gleich_an' oder 'betrag' sein")
        cfg["ag_bvg"] = ag_bvg
    if buchen is not None:
        cfg["buchen"] = bool(buchen)
    for role, nr in (konten or {}).items():
        if nr:
            book.account(str(nr))
            cfg["konten"][role] = str(nr)
    write_yaml(payroll.config_path(book), cfg)
    return _done(book, "Lohneinstellungen geändert", [payroll.config_path(book)])


def anhang_save(book: Book, jahr: int, text: str) -> dict:
    _guard(book)
    lock = book.settings.sperre_bis
    if lock and lock >= date(int(jahr), 12, 31):
        raise BookError(f"Das Geschäftsjahr {jahr} ist gesperrt")
    path = book.root / "abschluss" / str(jahr) / "anhang.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return _done(book, f"Anhang {jahr} gespeichert", [path])


def commit_diff(book: Book, commit: str) -> dict:
    return gitlog.show(book.root, commit)


def invoice_preview(book: Book, positionen: list[dict]) -> dict:
    from . import mwst as vat
    cfg = vat.config(book)
    pos = invoices.normalize_positions(positionen, book.settings.konto("ertrag"),
                                       "U81" if cfg["methode"] != "keine" else "")
    netto = sum((p["betrag"] for p in pos), Decimal("0"))
    breakdown = invoices.mwst_breakdown(pos)
    iban = invoices.qr.normalize_iban(book.settings.get("iban"))
    return jsonable({"positionen": pos, "netto": netto, "mwst": breakdown,
                     "total": netto + sum((b["steuer"] for b in breakdown), Decimal("0")),
                     "referenz_typ": invoices.qr.reference_type_for(iban) if iban else "NON"})


# ---------- one writer at a time ----------
# UI, CLI, MCP and the chat agent may all write the same book. A lock file
# serialises them; within a process an RLock keeps nested calls re-entrant.

import fcntl
import functools
import threading

_local_lock = threading.RLock()
_depth = threading.local()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(book, *args, **kwargs):
        with _local_lock:
            outer = getattr(_depth, "n", 0) == 0
            handle = None
            if outer:
                path = book.root / ".batzen" / "write.lock"
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(path, "w")
                fcntl.flock(handle, fcntl.LOCK_EX)
            _depth.n = getattr(_depth, "n", 0) + 1
            try:
                book.reload()          # see what the previous writer wrote
                return fn(book, *args, **kwargs)
            finally:
                _depth.n -= 1
                if handle:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()
    return wrapper


WRITES = ("add_account", "post_entry", "post_split", "reverse_entry", "propose", "approve", "reject",
          "customer_add", "invoice_create", "invoice_void", "invoice_pay", "invoice_credit", "employee_add",
          "payroll_run", "payslip_close", "payslip_reopen", "lohnausweis_create", "allocation_set",
          "allocation_book", "lock", "unlock", "customer_update", "employee_update", "account_update",
          "settings_update", "payroll_config_update", "anhang_save")
for _name in WRITES:
    globals()[_name] = _locked(globals()[_name])


# ---------- files from the UI ----------

def inbox_add(book: Book, filename: str, data: bytes) -> dict:
    """Store an uploaded file in inbox/ (never overwrites)."""
    from .files import slug
    name = Path(filename or "datei").name
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    target = book.root / "inbox" / f"{slug(stem)}{suffix}"
    n = 2
    while target.exists():
        target = book.root / "inbox" / f"{slug(stem)}-{n}{suffix}"
        n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return _done(book, f"Datei {target.name} in die Inbox gelegt", [target], datei=book.rel(target))


def attach_receipt(book: Book, beleg: str, source: str) -> dict:
    """Attach a receipt file to an already booked Beleg."""
    _guard(book)
    rows = [r for r in book.rows if r.beleg == beleg]
    if not rows:
        raise BookError(f"Beleg {beleg} nicht gefunden")
    src = Path(source) if Path(source).is_absolute() else book.root / source
    target = journal.attach(book, rows[0], src)
    return _done(book, f"Beleg-Datei an {beleg} angehängt", [target, src], datei=book.rel(target))


for _name in ("inbox_add", "attach_receipt"):
    globals()[_name] = _locked(globals()[_name])


# ---------- Kreditoren ----------

def supplier_list(book: Book) -> list[dict]:
    from . import kreditoren as kred
    return jsonable([{"nummer": k, "name": v.get("name"), "iban": v.get("iban"), "konto": v.get("konto"),
                      "mwst": v.get("mwst"), "ort": (v.get("adresse") or {}).get("ort")}
                     for k, v in kred.suppliers(book).items()])


def supplier_add(book: Book, **fields) -> dict:
    _guard(book)
    from . import kreditoren as kred
    meta, path = kred.add_supplier(book, **fields)
    return _done(book, f"Lieferant {meta['nummer']} {meta['name']} angelegt", [path], lieferant=meta)


def supplier_update(book: Book, nummer: str, notizen: str | None = None, **fields) -> dict:
    _guard(book)
    from . import kreditoren as kred
    sup = kred.supplier(book, nummer)
    allowed = {"name", "iban", "konto", "mwst", "email", *_ADDRESS_KEYS}
    unknown = set(fields) - allowed
    if unknown:
        raise BookError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    if fields.get("iban"):
        fields["iban"] = invoices.qr.normalize_iban(fields["iban"])
        if not invoices.qr.iban_is_valid(fields["iban"]):
            raise BookError("IBAN: Prüfsumme stimmt nicht")
    if fields.get("konto"):
        book.account(fields["konto"])
    if fields.get("mwst"):
        from . import mwst as vat
        fields["mwst"] = vat.code(fields["mwst"]).code
    meta = _update_record(sup["_pfad"], fields, notes=notizen)
    return _done(book, f"Lieferant {nummer} geändert", [sup["_pfad"]], lieferant=meta)


def qr_scan(book: Book, datei: str) -> dict:
    from . import kreditoren as kred
    return jsonable(kred.scan(book, datei))


def bill_add(book: Book, lieferant: str, betrag, entwurf: str = "", **fields) -> dict:
    """Enter and book a supplier bill; with `entwurf`, the draft it came from is closed in the same commit."""
    _guard(book)
    from . import erfassung, kreditoren as kred
    if entwurf:
        source = erfassung.draft(book, entwurf)
        fields.setdefault("datei", source.get("datei", ""))
    meta, touched = kred.add_bill(book, lieferant, betrag, **fields)
    if entwurf:
        touched += erfassung.discard(book, entwurf)
    return _done(book, f"Kreditor {meta['nummer']} {meta['name']} über {meta['betrag']:.2f} erfasst", touched,
                 kreditor=meta)


def bill_list(book: Book, status: str = "") -> list[dict]:
    from . import kreditoren as kred
    paid = kred.payments(book)
    out = [kred.state(book, m, paid.get(k, [])) for k, m in kred.bills(book).items()]
    return jsonable([s for s in out if not status or s["status"] == status])


def bill_pay(book: Book, nummer: str, datum=None, betrag=None, konto: str | None = None, kurs=None,
             fw=None) -> dict:
    """Book a payment. `betrag` is in the paying account's currency; for a foreign bill paid from a
    CHF account, the CHF actually debited (default: open amount × BAZG rate); `fw` pays only part."""
    _guard(book)
    from . import kreditoren as kred
    row, touched = kred.pay(book, nummer, datum, betrag, konto, kurs, fw)
    fwtext = f" ({row.waehrung} {row.fw:.2f})" if row.waehrung else ""
    return _done(book, f"Zahlung an Kreditor {nummer} verbucht{fwtext}: Beleg {row.beleg}", touched, buchung=row)


def bill_void(book: Book, nummer: str, grund: str = "") -> dict:
    _guard(book)
    from . import kreditoren as kred
    meta, touched = kred.void(book, nummer, grund)
    return _done(book, f"Kreditor {nummer} storniert" + (f": {grund}" if grund else ""), touched)


def payment_run(book: Book, nummern: list[str], ausfuehrung) -> dict:
    _guard(book)
    from . import kreditoren as kred
    info, touched = kred.create_run(book, nummern, ausfuehrung)
    return _done(book, f"Zahlungslauf {info['ausfuehrung']}: {info['anzahl']} Zahlung(en), {info['total']:.2f} — "
                 f"Datei {info['datei']} im E-Banking hochladen", touched, zahlungslauf=info)


def payment_run_book(book: Book, datei: str, datum=None) -> dict:
    _guard(book)
    from . import kreditoren as kred
    rows, touched = kred.book_run(book, datei, datum)
    return _done(book, f"Zahlungslauf {Path(datei).name}: {len(rows)} Zahlung(en) verbucht", touched, buchungen=rows)


def payables(book: Book) -> dict:
    from . import kreditoren as kred
    return jsonable(kred.open_payables(book))


# ---------- Bank ----------

def bank_import(book: Book, datei: str) -> dict:
    _guard(book)
    from . import bank
    source = Path(datei) if Path(datei).is_absolute() else book.root / datei
    if not source.is_file():
        source = Path(datei)
    if not source.is_file():
        raise BookError(f"Datei {datei} nicht gefunden")
    from . import erfassung
    summary, touched = bank.import_file(book, source)
    book.reload()
    touched += erfassung.rematch_payments(book)
    if source.resolve().is_relative_to((book.root / "inbox").resolve()):
        source.unlink()
        touched.append(source)
    msg = (f"Kontoauszug importiert: {summary['neu']} neue Bewegungen — {summary['gebucht']} gebucht, "
           f"{summary['abgeglichen']} abgeglichen, {summary['offen']} offen"
           + (f", {summary['doppelt']} schon bekannt" if summary["doppelt"] else "")
           + (f" (davon {summary['regeln']} per Bankregel)" if summary.get("regeln") else ""))
    return _done(book, msg, touched, import_=summary)


def bank_file_preview(book: Book, datei: str) -> dict:
    """The first lines of a statement file, for describing its format."""
    from . import bankformat
    path = bankformat._source(book, datei)
    data = path.read_bytes()
    known = bankformat.match(book, path.name, data, nur_bestaetigt=False)
    return jsonable({"datei": datei, "zeilen": bankformat.preview_text(path.name, data),
                     "format": known[0] if known else None,
                     "formate": sorted(bankformat.formats(book))})


def bank_format_propose(book: Book, datei: str, name: str, **spec) -> dict:
    """Store a described CSV/Excel format (unconfirmed) after reading the file with it."""
    from . import bankformat
    _guard(book)
    result, touched = bankformat.propose(book, datei, name, spec)
    return _done(book, f"Bankformat {result['format']} beschrieben (noch nicht bestätigt)", touched, **result)


def bank_format_check(book: Book, datei: str) -> dict:
    """Read a file with the (confirmed or not) format whose header it has: preview and checks, writes nothing."""
    from . import bankformat
    path = bankformat._source(book, datei)
    data = path.read_bytes()
    found = bankformat.match(book, path.name, data, nur_bestaetigt=False)
    if not found:
        raise BookError(f"Für {datei} ist kein Format beschrieben — batzen bank format lernen {datei}")
    name, spec = found
    statements, report = bankformat.read_with(book, name, spec, path.name, data)
    return jsonable({"format": name, "bestaetigt": bool(spec.get("bestaetigt")),
                     **bankformat.summary(statements, report)})


def bank_format_learn(book: Book, datei: str, konto: str = "") -> dict:
    """Let the book's agent describe the format of an unknown CSV/Excel statement."""
    from . import bankformat
    bankformat._source(book, datei)
    said = bankformat.run_agent(book.root, bankformat.format_prompt(datei, konto))
    try:
        result = bank_format_check(Book(book.root), datei)
    except BookError:
        raise BookError("Der Agent hat kein passendes Format beschrieben" + (f": {said[:300]}" if said else "")) from None
    return {**result, "agent": said}


def bank_format_confirm(book: Book, name: str) -> dict:
    from . import bankformat
    _guard(book)
    touched = bankformat.confirm(book, name)
    return _done(book, f"Bankformat {name} bestätigt", touched, format=name)


def bank_format_list(book: Book) -> list[dict]:
    from . import bankformat
    return jsonable([{"format": k, "name": v.get("name") or k, "bestaetigt": bool(v.get("bestaetigt")),
                      "konto": v.get("konto") or "", "datei": book.rel(v["_pfad"])}
                     for k, v in bankformat.formats(book).items()])


def card_statement_text(book: Book, datei: str) -> dict:
    from . import bankformat
    path = bankformat._source(book, datei)
    text = bankformat.pdf_text(path) if path.suffix.lower() == ".pdf" else path.read_text(errors="replace")
    return {"datei": datei, "text": text[:40000], "gekuerzt": len(text) > 40000}


def card_statement_propose(book: Book, datei: str, konto: str, saldo_alt, saldo_neu, buchungen: list[dict],
                           herausgeber: str = "", karte: str = "", von: str = "", bis: str = "") -> dict:
    """Store the transactions read from a credit card statement, with the balance and text checks."""
    from . import bankformat
    _guard(book)
    result, touched = bankformat.card_propose(book, datei, konto, saldo_alt, saldo_neu, buchungen,
                                              herausgeber, karte, von, bis)
    state = "geprüft" if result["pruefung"]["ok"] else "nicht geprüft"
    return _done(book, f"Kreditkartenabrechnung gelesen: {result['buchungen']} Buchungen ({state})", touched, **result)


def card_statement_read(book: Book, datei: str, konto: str) -> dict:
    """Let the agent read a credit card statement (PDF); import it when the checks pass."""
    from . import bankformat
    path = bankformat._source(book, datei)
    book.account(konto)
    said = bankformat.run_agent(book.root, bankformat.card_prompt(datei, konto))
    book = Book(book.root)
    data = path.read_bytes()
    card = bankformat.load_card(book, data)
    if card is None:
        raise BookError("Der Agent hat die Abrechnung nicht eingelesen" + (f": {said[:300]}" if said else ""))
    checks = bankformat.card_checks(card, bankformat.pdf_text(path))
    out = {"datei": book.rel(bankformat.card_path(book, data)), "pruefung": checks, "agent": said}
    if checks["ok"]:
        out["import"] = bank_import(book, datei)
    return jsonable(out)


def bank_list(book: Book, status: str = "") -> list[dict]:
    from . import bank
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in bank.transactions(book)
            if not status or t["Status"] == status]


def bank_book(book: Book, id: str, konto: str, text: str = "", mwst: str = "") -> dict:
    _guard(book)
    from . import bank
    row, touched = bank.book_transaction(book, id, konto, text, mwst)
    return _done(book, f"Bankbewegung {id} gebucht als Beleg {row.beleg}", touched, buchung=row)


def bank_assign(book: Book, id: str, nummer: str) -> dict:
    _guard(book)
    from . import bank
    row, touched = bank.assign(book, id, nummer)
    return _done(book, f"Bankbewegung {id} mit {nummer} verbucht (Beleg {row.beleg})", touched, buchung=row)


def bank_link(book: Book, id: str, beleg: str) -> dict:
    _guard(book)
    from . import bank
    touched = bank.link(book, id, beleg)
    return _done(book, f"Bankbewegung {id} mit Beleg {beleg} abgeglichen", touched)


def bank_ignore(book: Book, id: str, grund: str) -> dict:
    _guard(book)
    from . import bank
    touched = bank.ignore(book, id, grund)
    return _done(book, f"Bankbewegung {id} ignoriert: {grund}", touched)


def bank_suggest(book: Book, ids: list[str] | None = None, schwelle: float | None = None) -> dict:
    """Jev (TypeSafe): counter-account proposals for open bank transactions."""
    _guard(book)
    from . import jev
    res = jev.suggest_bank(book, ids, schwelle)
    touched = res.pop("_touched")
    msg = (f"Jev: {len(res['vorgeschlagen'])} Vorschläge, {len(res['unsicher'])} unsicher (nur Hinweis)"
           + (f", {len(res['uebersprungen'])} übersprungen (Mitarbeitende)" if res["uebersprungen"] else "")
           + (f", {len(res['fehler'])} Fehler" if res["fehler"] else ""))
    if touched:
        return _done(book, msg, touched, jev=res)
    book.reload()
    return jsonable({"ok": True, "meldung": msg, "commit": None, "jev": res})


def bank_suggestions(book: Book, ids: list[str] | None = None) -> dict:
    """What the open movements probably are: {ID: [suggestion, …]}, best first (see bank.suggestions)."""
    from . import bank
    return jsonable(bank.suggestions(book, ids))


def bank_accept(book: Book, id: str, art: str = "", ziel: str = "") -> dict:
    """Take a suggestion for an open movement; without art/ziel the first one."""
    _guard(book)
    from . import bank
    if not art:
        options = bank.suggestions(book, [id]).get(id)
        if not options:
            raise BookError(f"Für Bankbewegung {id} gibt es keinen Vorschlag")
        art, ziel = options[0]["art"], options[0]["ziel"]
    msg, touched = bank.accept(book, id, art, ziel)
    return _done(book, f"Bankbewegung {id}: {msg}", touched)


def bank_accept_all(book: Book) -> dict:
    """Take every sure suggestion (one per movement, best first) in one commit."""
    _guard(book)
    from . import bank
    done_, touched = [], []
    for tid, options in bank.suggestions(book).items():
        if not options[0]["sicher"]:
            continue
        try:
            # recomputed per movement: an invoice taken by one movement is no longer open for the next
            fresh = bank.suggestions(book, [tid]).get(tid) or []
            best = next((o for o in fresh if o["sicher"]), None)
            if best is None:
                continue
            msg, t = bank.accept(book, tid, best["art"], best["ziel"])
        except BookError:
            continue
        touched += t
        done_.append(f"{tid}: {msg}")
        book.reload()
    if not done_:
        return jsonable({"ok": True, "meldung": "Keine sicheren Vorschläge", "commit": None, "abgeglichen": []})
    return _done(book, f"{len(done_)} Bankbewegungen abgeglichen", touched, abgeglichen=done_)


def bank_reconciliation(book: Book) -> list[dict]:
    from . import bank
    return jsonable(bank.reconciliation(book))


# ---------- MWST ----------

def mwst_report(book: Book, periode: str) -> dict:
    from . import mwst
    return jsonable(mwst.report(book, periode))


def mwst_book(book: Book, periode: str) -> dict:
    _guard(book)
    from . import mwst
    rep, touched = mwst.book_report(book, periode)
    return _done(book, f"MWST-Abrechnung {rep['periode']} gebucht (Zahllast {rep['zahllast']:.2f})", touched,
                 abrechnung=rep)


mwst_book = _locked(mwst_book)


def mwst_abstimmung(book: Book, jahr: int, pdf_out: str | None = None, als_pdf: bool = False) -> dict:
    """Umsatz- und Steuerabstimmung eines Geschäftsjahres (Finalisierung, Art. 72 MWSTG)."""
    from . import mwst
    rep = mwst.abstimmung(book, int(jahr))
    out = jsonable(rep)
    if pdf_out or als_pdf:
        out["pdf"] = _write_report(book, pdf_out, pdf.mwst_abstimmung_pdf(book, rep),
                                   f"MWST-Umsatzabstimmung {jahr}.pdf")
    return out


def mwst_abgrenzung(book: Book, jahr: int, neu: bool = False) -> dict:
    """Vereinnahmte Entgelte: Steuer auf offenen Debitoren/Kreditoren per 31.12. abgrenzen (Rückbuchung 1.1.)."""
    _guard(book)
    from . import mwst
    saved, touched = mwst.book_abgrenzung(book, int(jahr), neu)
    return _done(book, f"MWST-Abgrenzung {jahr} gebucht (Umsatzsteuer offen {Decimal(str(saved['umsatzsteuer'])):.2f}, "
                 f"Vorsteuer offen {Decimal(str(saved['vorsteuer'])):.2f})", touched, abgrenzung=jsonable(saved))


mwst_abgrenzung = _locked(mwst_abgrenzung)


def mwst_export(book: Book, periode: str, korrektur: bool = False, out: str | None = None) -> dict:
    """eCH-0217 XML for upload in the ESTV portal (written to mwst/, not committed until the Abrechnung is booked)."""
    from . import mwst
    data = mwst.ech0217(book, periode, korrektur)
    label = mwst.resolve(periode)[2]
    path = Path(out) if out else book.root / "mwst" / f"eMWST {label}{' Korrektur' if korrektur else ''}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"ok": True, "meldung": f"eMWST-Datei {label} erstellt — im ESTV-Portal unter «MWST abrechnen» hochladen",
            "datei": str(path)}


def payslip_inputs(book: Book, monat: str, mitarbeiter: str, eingaben: dict) -> dict:
    """Replace a draft payslip's inputs and recalculate. An input left empty
    (None) falls back to the employee's standing value."""
    _guard(book)
    from .files import read_frontmatter, write_frontmatter
    y, m = _ym(monat)
    slip = payroll.load_payslip(book, y, m, mitarbeiter)
    if slip.get("status") == "abgeschlossen":
        raise BookError(f"Lohnabrechnung {mitarbeiter} {m:02d}/{y} ist abgeschlossen — zuerst wieder öffnen")
    allowed = ("stunden", "bvg", "kinderzulagen", "korrektur", "korrektur_text", "qst_satzbestimmend", "qst_gesamtpensum")
    unknown = set(eingaben) - set(allowed)
    if unknown:
        raise BookError(f"Unbekannte Eingaben: {', '.join(sorted(unknown))}")
    meta, body = read_frontmatter(slip["_pfad"])
    meta["eingaben"] = {k: eingaben.get(k) for k in allowed}
    meta["eingaben"]["korrektur"] = meta["eingaben"]["korrektur"] or 0
    meta["eingaben"]["korrektur_text"] = meta["eingaben"]["korrektur_text"] or ""
    write_frontmatter(slip["_pfad"], meta, body)
    results = payroll.run(book, y, m, mitarbeiter)
    w = results[0][0]["werte"]
    return _done(book, f"Lohnabrechnung {mitarbeiter} {m:02d}/{y} neu berechnet (netto {w['nettolohn']:.2f})",
                 [p for _, p in results])


payslip_inputs = _locked(payslip_inputs)


for _name in ("supplier_add", "supplier_update", "bill_add", "bill_pay", "bill_void", "payment_run", "payment_run_book",
              "bank_import", "bank_book", "bank_assign", "bank_link", "bank_ignore", "bank_suggest", "bank_accept",
              "bank_accept_all", "bank_format_propose", "bank_format_confirm", "card_statement_propose"):
    globals()[_name] = _locked(globals()[_name])

fx_revalue = _locked(fx_revalue)


# ---------- plugins ----------

def plugin_list(book: Book | None = None) -> list[dict]:
    from . import plugins
    return plugins.describe(book)


def plugin_enable(book: Book, name: str) -> dict:
    """Switch a plugin on for this book (it must be installed)."""
    from . import plugins
    _guard(book)
    plugin = plugins.installed().get(name)
    if plugin is None:
        raise BookError(f"Plugin '{name}' ist nicht installiert (pip install batzen-{name}); "
                        f"installiert: {', '.join(sorted(plugins.installed())) or 'keine'}")
    if not plugin.ok:
        raise BookError(f"Plugin '{name}' kann nicht laufen: {plugin.fehler}")
    names = list(plugins.enabled_names(book))
    if name in names:
        raise BookError(f"Plugin '{name}' ist bereits eingeschaltet")
    book.settings.data["plugins"] = names + [name]
    book.save_settings()
    return _done(book, f"Plugin {name} eingeschaltet", [book.root / "batzen.yaml"])


def plugin_disable(book: Book, name: str) -> dict:
    """Switch a plugin off. Refused while journal rows still belong to its documents."""
    from . import plugins
    names = list(plugins.enabled_names(book))
    if name not in names:
        raise BookError(f"Plugin '{name}' ist nicht eingeschaltet")
    plugin = plugins.installed().get(name)
    mine = ({src.prefix for src in plugin.module.batzen_sources() or []}
            if plugin and plugin.ok and hasattr(plugin.module, "batzen_sources") else set())
    used = sorted({r.quelle for r in book.rows if r.quelle.partition(":")[0] in mine})
    if used:
        raise BookError(f"Plugin '{name}' besitzt noch Journalzeilen ({', '.join(used[:3])} …) — "
                        "erst diese Dokumente stornieren")
    book.settings.data["plugins"] = [n for n in names if n != name]
    book.save_settings()
    return _done(book, f"Plugin {name} ausgeschaltet", [book.root / "batzen.yaml"])


def write(book: Book, message: str, fn, *args, **kwargs) -> dict:
    """The one way for a plugin to change a book: the book must be valid before,
    `fn(book, *args, **kwargs)` returns the paths it touched (or a tuple
    (result, paths)), then the book is checked and committed — or the change is refused."""
    _guard(book)
    out = fn(book, *args, **kwargs)
    result, paths = (out if isinstance(out, tuple) and len(out) == 2 else (None, out))
    return _done(book, message, list(paths or []), **({"ergebnis": result} if result is not None else {}))


plugin_enable = _locked(plugin_enable)
plugin_disable = _locked(plugin_disable)
write = _locked(write)


# ---------- Kreditoren drafts (upload → read → account → a person books) ----------

def bill_drafts(book: Book) -> list[dict]:
    from . import erfassung
    return jsonable([{k: v for k, v in d.items() if not k.startswith("_")} for d in erfassung.drafts(book).values()])


def bill_draft_create(book: Book, datei: str, art: str = "") -> dict:
    """Read a document (QR, text, OCR, plugin readers), recognise its kind (supplier bill, receipt,
    own invoice — or take `art`), assign accounts where it is sure and keep it as a draft.
    Nothing is booked."""
    from . import erfassung
    path = Path(datei) if Path(datei).is_absolute() else book.root / datei
    if not path.is_file():
        raise BookError(f"Datei {datei} nicht gefunden")
    fields, text, notes = erfassung.analyse(book, path)      # slow (OCR): outside the write lock
    return _bill_draft_store(book, datei, fields, text, notes, art)


def _bill_draft_store(book: Book, datei: str, fields: dict, text: str, notes: list[str], art: str = "") -> dict:
    from . import erfassung
    _guard(book)
    meta, touched = erfassung.create(book, datei, fields, text, notes, art)
    name = erfassung.value(meta, "name") or Path(datei).name
    return _done(book, f"Beleg-Entwurf {meta['id']} ({erfassung.ARTEN[meta['art']]}): {name} ({meta['status']})", touched,
                 entwurf={k: v for k, v in meta.items() if not k.startswith("_")})


def bill_draft_update(book: Book, entwurf: str, quelle: str = "Hand", konto: str = "", mwst: str | None = None,
                      begruendung: str = "", positionen: list[dict] | None = None, art: str = "",
                      zahlkonto: str = "", mitarbeiter: str = "", **fields) -> dict:
    from . import erfassung
    _guard(book)
    meta, touched = erfassung.update(book, entwurf, quelle, konto, mwst, begruendung, positionen, art, zahlkonto,
                                     mitarbeiter, **fields)
    return _done(book, f"Beleg-Entwurf {entwurf} ergänzt ({quelle}" + (f": Konto {konto}" if konto else "") + ")",
                 touched, entwurf=meta)


def bill_draft_mark(book: Book, entwurf: str, status: str, hinweis: str = "") -> dict:
    from . import erfassung
    _guard(book)
    meta, touched = erfassung.mark(book, entwurf, status, hinweis)
    return _done(book, f"Beleg-Entwurf {entwurf}: {status}", touched, entwurf=meta)


def bill_draft_discard(book: Book, entwurf: str) -> dict:
    from . import erfassung
    _guard(book)
    touched = erfassung.discard(book, entwurf)
    return _done(book, f"Beleg-Entwurf {entwurf} verworfen (Datei bleibt in der Inbox)", touched)


def bill_draft_agent(book: Book, entwurf: str) -> dict:
    """Let the book's agent read and account a draft (it writes through complete_bill_draft)."""
    from . import erfassung
    said = erfassung.run_agent(book.root, entwurf)
    meta = erfassung.draft(Book(book.root), entwurf)
    return jsonable({"ok": True, "entwurf": {k: v for k, v in meta.items() if not k.startswith("_")},
                     "agent": said})


_bill_draft_store = _locked(_bill_draft_store)
bill_draft_update = _locked(bill_draft_update)
bill_draft_mark = _locked(bill_draft_mark)
bill_draft_discard = _locked(bill_draft_discard)


def receipt_book(book: Book, entwurf: str, datum, text: str, betrag, konto: str = "", mwst: str = "",
                 positionen: list[dict] | None = None, waehrung: str = "", kurs=None,
                 zahlung: dict | None = None) -> dict:
    """Book a receipt draft — with its open bank movement, onto an existing booking (file only)
    or against a cash/bank/card account. The receipt is filed under belege/."""
    from . import erfassung
    _guard(book)
    rows, touched = erfassung.book_receipt(book, entwurf, datum, text, betrag, konto, mwst, positionen,
                                           waehrung, kurs, zahlung)
    pay = (zahlung or {}).get("art")
    msg = (f"Quittung {entwurf} an Beleg {rows[0].beleg} abgelegt" if pay == "buchung"
           else f"Quittung {entwurf} als Spesenbeleg {rows[0].beleg} erfasst — wird mit dem nächsten Lohn ausbezahlt"
           if pay == "spesen" else f"Quittung {entwurf} gebucht: Beleg {rows[0].beleg} {text}")
    return _done(book, msg, touched, buchungen=rows)


def invoice_external(book: Book, kunde: str, betrag, entwurf: str = "", **fields) -> dict:
    """Record an invoice issued outside batzen as an open item (Debitoren an Ertrag)."""
    from . import erfassung
    _guard(book)
    if entwurf:
        fields.setdefault("datei", erfassung.draft(book, entwurf).get("datei", ""))
    meta, touched = invoices.record_external(book, kunde, betrag, **fields)
    if entwurf:
        touched += erfassung.discard(book, entwurf)
    return _done(book, f"Externe Rechnung {meta['nummer']} an {meta['an']['name']} über {meta['total']:.2f} erfasst",
                 touched, rechnung=meta)


receipt_book = _locked(receipt_book)
invoice_external = _locked(invoice_external)


# ---------- Mahnwesen ----------

def reminders(book: Book, as_of=None) -> list[dict]:
    """Overdue invoices with the reminders sent and the next step."""
    from . import mahnungen
    return jsonable(mahnungen.overdue(book, parse_date(as_of, "datum") if as_of else None))


def reminder_create(book: Book, nummern: list[str], datum=None, frist_tage: int | None = None) -> dict:
    """Write the next reminder (PDF with QR-bill) for each invoice."""
    from . import mahnungen
    _guard(book)
    made, touched = [], []
    for nr in nummern:
        entry, paths = mahnungen.create(book, nr, datum, frist_tage)
        made.append({"rechnung": nr, **entry})
        touched += paths
    label = ", ".join(f"{m['rechnung']} ({m['bezeichnung']})" for m in made)
    return _done(book, f"Mahnungen erstellt: {label}", touched, mahnungen=made)


reminder_create = _locked(reminder_create)


# ---------- bank rules ----------

def bank_rules(book: Book) -> list[dict]:
    from . import bank
    return bank.rules(book)


def bank_rule_add(book: Book, konto: str, gegenpartei: str = "", text: str = "", betrag=None, richtung: str = "",
                  mwst: str = "", buchungstext: str = "", anwenden: bool = True) -> dict:
    """A rule that books recognised movements on import; with `anwenden` also the open ones now."""
    from . import bank
    _guard(book)
    rule, path = bank.add_rule(book, konto, gegenpartei, text, betrag, richtung, mwst, buchungstext)
    touched, booked = [path], []
    if anwenden:
        booked, t = bank.apply_rules(book)
        touched += t
    return _done(book, f"Bankregel {rule['id']} «{rule['name']}» → {konto}" + (f", {len(booked)} Bewegung(en) gebucht"
                                                                          if booked else ""),
                 touched, regel=rule, gebucht=booked)


def bank_rule_from(book: Book, id: str, mit_betrag: bool = False) -> dict:
    """«Immer so buchen» from a booked movement; books matching open movements right away."""
    from . import bank
    _guard(book)
    rule, path = bank.rule_from_transaction(book, id, mit_betrag)
    booked, t = bank.apply_rules(book)
    return _done(book, f"Bankregel {rule['id']} «{rule['name']}» → {rule['konto']}"
                 + (f", {len(booked)} Bewegung(en) gebucht" if booked else ""), [path] + t, regel=rule, gebucht=booked)


def bank_rule_remove(book: Book, regel: str) -> dict:
    from . import bank
    _guard(book)
    return _done(book, f"Bankregel {regel} entfernt", [bank.remove_rule(book, regel)])


bank_rule_add = _locked(bank_rule_add)
bank_rule_from = _locked(bank_rule_from)
bank_rule_remove = _locked(bank_rule_remove)


def dividend_pay(book: Book, jahr: int, datum=None, konto: str = "") -> dict:
    """Pay the dividend decided for `jahr`: 65 % to the shareholders, 35 % Verrechnungssteuer."""
    _guard(book)
    saved, touched = statements.pay_dividend(book, jahr, datum, konto)
    return _done(book, f"Dividende {jahr} ausbezahlt: netto {saved['netto']:.2f}, Verrechnungssteuer {saved['vst']:.2f} "
                 f"— Formular 103 bis {saved['frist']}", touched, dividende=saved)


dividend_pay = _locked(dividend_pay)


# ---------- Spesen ----------

def expense_add(book: Book, mitarbeiter: str, datum, text: str, betrag, konto: str = "", mwst: str = "",
                positionen: list[dict] | None = None, art: str = "uebrige", datei: str = "", waehrung: str = "",
                kurs=None) -> dict:
    """An expense an employee paid: booked against the expenses-owed account (2210), paid with the next payslip."""
    from . import spesen
    _guard(book)
    meta, touched = spesen.add(book, mitarbeiter, datum, text, betrag, konto, mwst, positionen, art, datei,
                               waehrung, kurs)
    return _done(book, f"Spesenbeleg {meta['nummer']} {meta['name']}: {meta['text']} ({meta['betrag']})", touched,
                 spesen=meta)


def expense_list(book: Book, mitarbeiter: str = "", offen: bool = False) -> list[dict]:
    from . import spesen
    return jsonable([s for s in spesen.summary(book) if (not mitarbeiter or s["mitarbeiter"] == mitarbeiter)
                     and (not offen or not s["lohn"])])


def expense_remove(book: Book, nummer: str) -> dict:
    from . import spesen
    _guard(book)
    return _done(book, f"Spesenbeleg {nummer} entfernt", spesen.remove(book, nummer))


expense_add = _locked(expense_add)
expense_remove = _locked(expense_remove)


# ---------- several bookings at once (the journal grid) ----------

class RowErrors(BookError):
    """Some lines of a batch were refused; `fehler` maps the line number (1-based) to the reason."""

    def __init__(self, fehler: dict[int, str]):
        self.fehler = fehler
        super().__init__("Nichts gebucht — bitte korrigieren:\n" +
                         "\n".join(f"Zeile {n}: {msg}" for n, msg in sorted(fehler.items())))


def post_entries(book: Book, zeilen: list[dict]) -> dict:
    """Book several simple bookings in one go — all or none, one commit. Each line:
    {datum, text, soll, haben, betrag, mwst?, waehrung?, kurs?, beleg?}."""
    from .files import FormatError
    _guard(book)
    if not zeilen:
        raise BookError("Keine Buchungen")
    taken: set[str] = set()
    prepared, errors = [], {}
    for n, z in enumerate(zeilen, 1):
        try:
            if not str(z.get("text") or "").strip():
                raise BookError("Text fehlt")
            for side in ("soll", "haben"):
                if not str(z.get(side) or "").strip():
                    raise BookError(f"{side.capitalize()}-Konto fehlt")
                book.account(str(z[side]).strip())
            row, rows = journal.prepare_entry(book, z.get("datum"), z["soll"], z["haben"], z.get("betrag"), z["text"],
                                              z.get("beleg") or "", mwst=(z.get("mwst") or "").upper(),
                                              waehrung=z.get("waehrung") or "", kurs=z.get("kurs") or None,
                                              taken=taken)
            taken.add(row.beleg)
            prepared.append((row, rows))
        except (BookError, FormatError, ValueError) as exc:
            errors[n] = str(exc).replace("Fehler: ", "")
    if errors:
        raise RowErrors(errors)
    touched = journal.post(book, [r for _, rows in prepared for r in rows])
    first, last = prepared[0][0].beleg, prepared[-1][0].beleg
    label = first if len(prepared) == 1 else f"{first} … {last}"
    return _done(book, f"{len(prepared)} Buchung(en) erfasst ({label})", touched,
                 buchungen=[row for row, _ in prepared])


post_entries = _locked(post_entries)

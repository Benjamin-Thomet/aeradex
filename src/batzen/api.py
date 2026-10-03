"""The operations of batzen, shared by the CLI and the MCP server.

Every function takes a Book (or a path), returns plain JSON-able data, and —
for writes — commits its changes to git with a descriptive message. Writes
refuse to start on a book that already has errors, so a bad state is never
compounded; what they write is validated before it touches a file.
"""
from __future__ import annotations

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
                "quelle": value.quelle}
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
                     "dateien": sorted({book.rel(p) for p in paths if Path(p).exists()}), **data})


# ---------- book ----------

def init_book(path: Path, firma: str, jahr: int | None = None, kontenplan: str = "kmu",
              rechtsform: str = "GmbH", git: bool = True, **settings) -> dict:
    root = Path(path).resolve()
    if (root / "batzen.yaml").exists():
        raise BookError(f"{root} enthält bereits ein Buch")
    root.mkdir(parents=True, exist_ok=True)
    template = DATA / "kontenplaene" / f"{kontenplan}.yaml"
    if not template.exists():
        raise BookError(f"Kontenplan-Vorlage '{kontenplan}' unbekannt")
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
                   "reserve": "2950"},
    }
    write_yaml(root / "batzen.yaml", data)
    shutil.copy(template, root / "kontenplan.yaml")
    for folder in ("journal", "belege", "inbox", "kunden", "rechnungen", "personal", "lohn", "abschluss"):
        (root / folder).mkdir(exist_ok=True)
        if not any((root / folder).iterdir()):
            (root / folder / ".gitkeep").write_text("")
    book = Book(root)
    payroll.write_default_config(book)
    (root / ".gitignore").write_text("berichte/\n.DS_Store\n__pycache__/\n")
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
    return [{"konto": a.nr, "name": a.name, "klasse": a.klasse, "gruppe": a.gruppe}
            for a in book.accounts.values()
            if a.aktiv_ and (not q or q in a.nr or q in a.name.lower())]


def add_account(book: Book, nr: str, name: str, klasse: str = "", gruppe: str = "") -> dict:
    _guard(book)
    from .book import _klasse_for, Account, KLASSEN
    if nr in book.accounts:
        raise BookError(f"Konto {nr} existiert bereits")
    klasse = klasse or _klasse_for(nr)
    if klasse not in KLASSEN:
        raise BookError(f"klasse muss eine von {', '.join(KLASSEN)} sein")
    book.accounts[nr] = Account(nr=nr, name=name, klasse=klasse,
                                gruppe=gruppe or statements.default_group(nr, klasse))
    book.save_accounts()
    return _done(book, f"Konto {nr} {name} angelegt", [book.root / "kontenplan.yaml"])


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
               datei: str | None = None) -> dict:
    _guard(book)
    row, touched = journal.book_entry(book, datum, soll, haben, betrag, text, beleg,
                                      attachment=Path(datei) if datei else None)
    return _done(book, f"Beleg {row.beleg} gebucht: {row.text} ({row.soll} an {row.haben} {row.betrag:.2f})",
                 touched, buchung=row)


def post_split(book: Book, datum, text: str, zeilen: list[dict], beleg: str = "",
               datei: str | None = None) -> dict:
    """A split (Sammel-)Buchung: several lines under one Beleg, each with soll
    and/or haben; the Beleg as a whole must balance."""
    _guard(book)
    d = parse_date(datum, "datum")
    ref = beleg or journal.next_beleg(book, d.year)
    rows = [Row(d, ref, str(z.get("text") or text), str(z.get("soll") or ""), str(z.get("haben") or ""),
                Decimal(str(z["betrag"]))) for z in zeilen]
    touched = journal.post(book, rows, Path(datei) if datei else None)
    return _done(book, f"Beleg {ref} gebucht (Sammelbuchung, {len(rows)} Zeilen): {text}", touched, buchungen=rows)


def reverse_entry(book: Book, beleg: str, datum=None, text: str = "") -> dict:
    _guard(book)
    rows, touched = journal.reverse(book, beleg, datum, text)
    return _done(book, f"Beleg {beleg} storniert mit {rows[0].beleg}", touched, buchungen=rows)


def propose(book: Book, datum, soll, haben, betrag, text, begruendung: str = "", datei: str = "") -> dict:
    _guard(book)
    cells, path = journal.propose(book, datum, soll, haben, betrag, text, begruendung, datei=datei or "")
    return _done(book, f"Vorschlag {cells['ID']}: {cells['Text']} ({cells['Soll']} an {cells['Haben']} {cells['Betrag']})",
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
                   zahlungsfrist: int | None = None) -> dict:
    _guard(book)
    meta, touched = invoices.issue_invoice(book, kunde, positionen, datum, text, zahlungsfrist)
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


def invoice_pay(book: Book, nr: str, betrag=None, datum=None, konto: str | None = None) -> dict:
    _guard(book)
    row, touched = invoices.pay_invoice(book, nr, betrag, datum, konto)
    return _done(book, f"Zahlung {row.betrag:.2f} auf Rechnung {nr} verbucht (Beleg {row.beleg})", touched, buchung=row)


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

"""Page handlers. Each GET collects data from the book and renders a template;
each POST calls exactly one `api` write through `act`."""
from __future__ import annotations

import asyncio
import os
import threading
import tempfile
from collections import OrderedDict, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import api, check as checks, invoices, journal, payroll, statements
from ..book import Book, BookError
from ..files import parse_amount, parse_date
from ..ledger import BalanceEngine, account_ledger, movements, resolve_period, trial_balance
from .app import UI, acct, act, done, fail

ZERO = Decimal("0")
QUELLEN = {"": "Alle Quellen", "manuell": "Manuell", "rechnung": "Rechnungen", "zahlung": "Zahlungen",
           "gutschrift": "Gutschriften", "lohn": "Lohn", "abschluss": "Abschluss",
           "bewertung": "Fremdwährungsbewertung"}


def current_year(book: Book) -> int:
    years = book.years()
    return date.today().year if date.today().year in years else max(years)


def year_param(request: Request, book: Book) -> int:
    try:
        return int(request.query_params.get("jahr") or current_year(book))
    except ValueError:
        return current_year(book)


def account_options(book: Book) -> list[dict]:
    return [{"nr": a.nr, "name": a.name + (f" ({a.waehrung})" if a.is_foreign else "")}
            for a in book.accounts.values() if a.aktiv_]


def currencies(book: Book) -> list[str]:
    """Currencies offered in booking forms: those of the chart plus the usual ones."""
    used = {a.waehrung for a in book.accounts.values() if a.is_foreign}
    return sorted(used | {"EUR", "USD", "GBP"})


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"):
        return "image"
    if suffix in (".txt", ".csv", ".md", ".xml", ".json"):
        return "text"
    return "other"


# ---------- Übersicht ----------

def overview_data(book: Book) -> dict:
    year = current_year(book)
    eng = BalanceEngine(book)
    liquid = [(a, eng.balance(nr, year)) for nr, a in sorted(book.accounts.items()) if a.gruppe == "fluessige"]
    liquid_total = sum((b for _, b in liquid), ZERO)
    months = []
    last_month = max([r.datum.month for r in eng.year_rows(year)] or [date.today().month])
    for m in range(1, last_month + 1):
        mv = movements([r for r in eng.year_rows(year) if r.datum.month == m])
        ertrag = -sum((v for k, v in mv.items() if k in book.accounts and book.accounts[k].klasse == "ertrag"), ZERO)
        aufwand = sum((v for k, v in mv.items() if k in book.accounts and book.accounts[k].klasse == "aufwand"), ZERO)
        months.append({"m": m, "ertrag": ertrag, "aufwand": aufwand,
                       "fluessig": sum((eng.balance_at(a.nr, _month_end(year, m)) for a, _ in liquid), ZERO)})
    peak = max([max(x["ertrag"], x["aufwand"]) for x in months] + [Decimal(1)])
    step = _nice_step(peak)
    top = ((peak // step) + 1) * step
    ertrag_total = -sum((eng.balance(nr, year) for nr, a in book.accounts.items() if a.klasse == "ertrag"), ZERO)
    aufwand_total = sum((eng.balance(nr, year) for nr, a in book.accounts.items() if a.klasse == "aufwand"), ZERO)
    personal = sum((eng.balance(nr, year) for nr, a in book.accounts.items() if a.gruppe == "personal"), ZERO)
    ar = invoices.aged_receivables(book, date.today() if date.today().year == year else date(year, 12, 31))
    overdue = [p for p in ar["posten"] if parse_date(p["faellig"]) < date.today()]
    spark = _sparkline([x["fluessig"] for x in months])
    return {"year": year, "liquid": liquid, "liquid_total": liquid_total, "months": months, "chart_top": top,
            "chart_step": step, "ertrag": ertrag_total, "aufwand": aufwand_total, "result": ertrag_total - aufwand_total,
            "personal_share": int(personal / aufwand_total * 100) if aufwand_total else 0,
            "ar": ar, "overdue": overdue, "spark": spark, "history": api.history(book, 8)}


def _month_end(year: int, month: int) -> date:
    from ..ledger import month_end
    return month_end(year, month)


def _nice_step(peak: Decimal) -> Decimal:
    for step in (100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000):
        if peak / step <= 4:
            return Decimal(step)
    return Decimal(2500000)


def _sparkline(values: list[Decimal]) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or Decimal(1)
    n = len(values) - 1
    return " ".join(f"{i * 200 / n:.1f},{30 - float((v - lo) / span) * 24:.1f}" for i, v in enumerate(values))


def todo_items(book: Book) -> list[dict]:
    """Everything that waits for a person, most urgent first."""
    items = []
    issues = checks.run(book)
    for i in issues:
        if i.level == "fehler":
            items.append({"level": "red", "title": i.message, "detail": i.where, "tag": "Fehler", "href": "/einstellungen#pruefung"})
    inbox = book.root / "inbox"
    for p in sorted(inbox.iterdir()) if inbox.exists() else []:
        if p.is_file() and not p.name.startswith("."):
            items.append({"level": "info", "title": p.name, "detail": "Datei in der Inbox", "tag": "Inbox",
                          "href": f"/pruefen?datei={p.name}"})
    for prop in journal.list_proposals(book):
        items.append({"level": "info", "title": f"{prop['Text']}, {prop['Betrag']}",
                      "detail": f"Vorschlag {prop['ID']} · {prop['Soll']} an {prop['Haben']}", "tag": "Vorschlag",
                      "href": "/pruefen#vorschlaege"})
    drafts = defaultdict(list)
    for slip in payroll.payslips(book):
        if slip.get("status") != "abgeschlossen":
            drafts[f"{slip['jahr']}-{int(slip['monat']):02d}"].append(slip)
    for month, slips in sorted(drafts.items()):
        items.append({"level": "warn", "title": f"Löhne {month[5:]}/{month[:4]} abschliessen",
                      "detail": " · ".join(f"{s.get('name')} {Decimal(str(s['werte']['nettolohn'])):,.2f} netto".replace(",", "'") for s in slips),
                      "tag": f"{len(slips)} Entwurf" + ("e" if len(slips) > 1 else ""), "href": f"/lohn?monat={month}"})
    from .. import kreditoren as kred
    from datetime import timedelta
    soon = (date.today() + timedelta(days=7)).isoformat()
    due = [r for r in kred.open_payables(book)["posten"] if r["status"] == "offen" and r["faellig"] <= soon]
    if due:
        items.append({"level": "warn", "title": f"{len(due)} Lieferantenrechnung{'en' if len(due) > 1 else ''} bald fällig",
                      "detail": " · ".join(f"{r['name']} {Decimal(str(r['offen'])):,.2f}".replace(",", "'") for r in due[:3]),
                      "tag": "Zahlen", "href": "/kreditoren?status=offen"})
    for i in issues:
        if i.level in ("warnung",) or (i.level == "hinweis" and i.where == "belege"):
            items.append({"level": "red" if i.level == "warnung" else "muted", "title": i.message, "detail": i.where,
                          "tag": "Warnung" if i.level == "warnung" else "Hinweis", "href": "/journal?ohne_beleg=1"})
    return items


async def uebersicht(ui: UI, request: Request):
    book = ui.book()
    data = await asyncio.to_thread(overview_data, book)
    return ui.render(request, "uebersicht.html", book=book, d=data, todos=todo_items(book))


# ---------- Prüfen ----------

def draft_rows(book: Book) -> list[dict]:
    out = []
    emps = payroll.employees(book)
    for slip in payroll.payslips(book):
        if slip.get("status") == "abgeschlossen":
            continue
        emp = emps.get(slip["mitarbeiter"], {})
        out.append({"slip": slip, "emp": emp, "warnings": payslip_warnings(slip, emp)})
    return out


def payslip_warnings(slip: dict, emp: dict) -> list[str]:
    warnings = []
    w = slip.get("werte") or {}
    inputs = slip.get("eingaben") or {}
    if payroll.uses_tarif(emp) and not Decimal(str(w.get("quellensteuer") or 0)):
        if not (inputs.get("qst_satzbestimmend") or inputs.get("qst_gesamtpensum")):
            warnings.append("QST-Eingabe fehlt: ohne satzbestimmendes Einkommen oder Gesamtpensum wird keine Quellensteuer abgezogen")
    if not Decimal(str(w.get("bruttolohn") or 0)):
        warnings.append("Bruttolohn ist 0")
    return warnings


async def pruefen(ui: UI, request: Request):
    book = ui.book()
    inbox = book.root / "inbox"
    from .. import erfassung
    all_drafts = list(erfassung.drafts(book).values())
    drafted = {d.get("datei") for d in all_drafts}
    files = [p for p in sorted(inbox.iterdir()) if p.is_file() and not p.name.startswith(".")
             and f"inbox/{p.name}" not in drafted] if inbox.exists() else []
    chosen = request.query_params.get("datei")
    current = next((p for p in files if p.name == chosen), files[0] if files else None)
    proposals = journal.list_proposals(book)
    linked = next((p for p in proposals if current and Path(p.get("Datei") or "").name == current.name), None)
    text = None
    if current and file_kind(current) == "text":
        text = current.read_text(encoding="utf-8", errors="replace")[:5000]
    issues = [i for i in checks.run(book) if not (i.level == "hinweis" and i.where.startswith("lohn/"))
              and i.where != "inbox"]
    qrbill = _qr_hint(book, f"inbox/{current.name}") if current and file_kind(current) in ("pdf", "image") else None
    from .. import bank
    bank_open = [t for t in bank.transactions(book) if t["Status"] == "offen"]
    return ui.render(request, "pruefen.html", book=book, files=files, current=current, qrbill=qrbill, bank_open=bank_open,
                     kind=file_kind(current) if current else None, text=text, proposals=proposals, linked=linked,
                     drafts=all_drafts, payslip_drafts=draft_rows(book), dv=erfassung.value,
                     issues=issues, accounts=account_options(book),
                     currencies=currencies(book), next_beleg=journal.next_beleg(book, date.today().year))


async def inbox_upload(ui: UI, request: Request):
    form = await request.form()
    upload = form.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    data = await upload.read()
    return await act(request, api.inbox_add, lambda r: f"/pruefen?datei={Path(r['datei']).name}", ui.book(),
                     upload.filename, data)


async def pruefen_buchen(ui: UI, request: Request):
    f = await request.form()
    datei = f.get("datei") or None
    return await act(request, api.post_entry, "/pruefen", ui.book(), f.get("datum"), acct(f.get("soll")),
                     acct(f.get("haben")), f.get("betrag"), f.get("text", ""), "", datei and f"inbox/{datei}",
                     f.get("mwst", ""), f.get("waehrung", ""), f.get("kurs") or None)


async def vorschlag(ui: UI, request: Request):
    f = await request.form()
    ids = f.getlist("id") or [request.path_params.get("id")]
    if request.path_params["aktion"] == "freigeben":
        return await act(request, api.approve, request.headers.get("hx-current-url", "/pruefen"), ui.book(), ids)
    return await act(request, api.reject, request.headers.get("hx-current-url", "/pruefen"), ui.book(), ids)


# ---------- Journal ----------

def journal_groups(book: Book, year: int, month: int | None, konto: str, quelle: str, q: str,
                   ohne_beleg: bool) -> list[dict]:
    rows = [r for r in book.rows if r.datum.year == year and (not month or r.datum.month == month)]
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        groups.setdefault(r.beleg, []).append(r)
    folder = book.root / "belege" / str(year)
    names = sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.exists() else []
    lock = book.settings.sperre_bis
    out = []
    for beleg, group in groups.items():
        first = group[0]
        kind = first.quelle.partition(":")[0] if first.quelle else "manuell"
        if quelle and kind != quelle:
            continue
        if konto and not any(konto in (r.soll, r.haben) for r in group):
            continue
        if q and not any(q.lower() in f"{r.text} {r.beleg} {r.soll} {r.haben}".lower() for r in group):
            continue
        receipts = [n for n in names if n == beleg or n.startswith(beleg + " ")]
        if ohne_beleg and (receipts or first.quelle):
            continue
        sollset = {r.soll for r in group if r.soll}
        habenset = {r.haben for r in group if r.haben}
        out.append({
            "beleg": beleg, "datum": first.datum, "rows": group, "kind": kind, "quelle": first.quelle,
            "text": first.text.split(" · ")[0] if len(group) > 1 else first.text,
            "soll": sollset.pop() if len(sollset) == 1 else "div.",
            "haben": habenset.pop() if len(habenset) == 1 else "div.",
            "total": sum((r.betrag for r in group if r.soll), ZERO),
            "receipts": receipts, "locked": bool(lock and first.datum <= lock),
            "doc": doc_link(first.quelle, book),
        })
    return out


def doc_link(quelle: str, book: Book | None = None) -> str | None:
    kind, _, ref = quelle.partition(":")
    if book is not None and kind:
        from .. import plugins
        src = plugins.sources(book).get(kind)
        if src is not None:
            return src.link(ref) if src.link else None
    if kind in ("rechnung", "zahlung", "gutschrift"):
        return f"/debitoren/rechnung/{ref}"
    if kind == "lohn":
        month, _, nr = ref.partition(":")
        return f"/lohn/abrechnung/{month}/{nr}"
    if kind == "abschluss":
        return f"/abschluss?jahr={ref}"
    if kind == "bewertung":
        return f"/abschluss?jahr={ref[:4]}&stichtag={ref}"
    return None


async def journal_page(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    qp = request.query_params
    counts = defaultdict(int)
    for r in book.rows:
        if r.datum.year == year:
            counts[r.datum.month] += 1
    month_raw = qp.get("monat")
    if month_raw is None:
        month = max(counts) if counts else date.today().month
    else:
        month = int(month_raw) if month_raw.isdigit() and 1 <= int(month_raw) <= 12 else None
    groups = journal_groups(book, year, month, qp.get("konto", ""), qp.get("quelle", ""), qp.get("q", ""),
                            bool(qp.get("ohne_beleg")))
    return ui.render(request, "journal.html", book=book, year=year, month=month, counts=counts, groups=groups,
                     names={a.nr: a.name for a in book.accounts.values()}, accounts=account_options(book),
                     quellen=QUELLEN, q=qp.get("q", ""), konto=qp.get("konto", ""), quelle=qp.get("quelle", ""),
                     ohne_beleg=bool(qp.get("ohne_beleg")), next_beleg=journal.next_beleg(book, year),
                     currencies=currencies(book))


async def journal_buchen(ui: UI, request: Request):
    f = await request.form()
    book = ui.book()
    upload = f.get("datei")
    tmp = None
    if upload is not None and getattr(upload, "filename", ""):
        tmpdir = Path(tempfile.mkdtemp(prefix="batzen-"))
        tmp = tmpdir / Path(upload.filename).name
        tmp.write_bytes(await upload.read())
    try:
        if f.get("modus") == "split":
            lines = []
            codes = f.getlist("z_mwst") or [""] * len(f.getlist("z_soll"))
            for soll, haben, betrag, text, code in zip(f.getlist("z_soll"), f.getlist("z_haben"), f.getlist("z_betrag"),
                                                       f.getlist("z_text"), codes):
                if not (soll or haben or betrag):
                    continue
                lines.append({"soll": acct(soll), "haben": acct(haben), "betrag": betrag or "0",
                              "text": text or f.get("text"), "mwst": code})
            return await act(request, api.post_split, request.headers.get("hx-current-url", "/journal"), book,
                             f.get("datum"), f.get("text", ""), lines, "", str(tmp) if tmp else None,
                             f.get("waehrung", ""), f.get("kurs") or None)
        return await act(request, api.post_entry, request.headers.get("hx-current-url", "/journal"), book,
                         f.get("datum"), acct(f.get("soll")), acct(f.get("haben")), f.get("betrag"), f.get("text", ""),
                         "", str(tmp) if tmp else None, f.get("mwst", ""), f.get("waehrung", ""), f.get("kurs") or None)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


async def journal_storno(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.reverse_entry, request.headers.get("hx-current-url", "/journal"), ui.book(),
                     f.get("beleg"), f.get("datum") or None, f.get("text", ""))


async def journal_beleg(ui: UI, request: Request):
    f = await request.form()
    upload = f.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    tmpdir = Path(tempfile.mkdtemp(prefix="batzen-"))
    tmp = tmpdir / Path(upload.filename).name
    tmp.write_bytes(await upload.read())
    try:
        return await act(request, api.attach_receipt, request.headers.get("hx-current-url", "/journal"), ui.book(),
                         f.get("beleg"), str(tmp))
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------- Konten ----------

async def konten(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    eng = BalanceEngine(book)
    by_klasse = OrderedDict((k, []) for k in ("aktiv", "passiv", "aufwand", "ertrag"))
    for nr, a in sorted(book.accounts.items()):
        by_klasse[a.klasse].append({"a": a, "saldo": eng.balance(nr, year)})
    return ui.render(request, "konten.html", book=book, year=year, by_klasse=by_klasse,
                     groups=statements.GROUPS, tab="kontenplan")


async def saldenliste(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    periode = request.query_params.get("periode", "jahr")
    try:
        start, end = resolve_period(year, periode)
    except ValueError:
        start, end = resolve_period(year, "jahr")
    rows = trial_balance(book, year, start, end)
    totals = {k: sum((r[k] for r in rows), ZERO) for k in ("soll", "haben")}
    return ui.render(request, "saldenliste.html", book=book, year=year, rows=rows, start=start, end=end,
                     periode=periode, totals=totals, tab="saldenliste")


async def kontoblatt(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    try:
        led = account_ledger(book, request.path_params["nr"], year)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return ui.render(request, "kontoblatt.html", book=book, year=year, led=led, tab="kontenplan")


async def konto_neu(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.add_account, "/konten", ui.book(), f.get("nr", "").strip(), f.get("name", "").strip(),
                     f.get("klasse", ""), f.get("gruppe", ""), f.get("waehrung", ""))


async def konto_aendern(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    kwargs = {"name": f.get("name"), "gruppe": f.get("gruppe") or None, "aktiv": f.get("aktiv") == "1"}
    if f.get("eroeffnung") not in (None, ""):
        kwargs["eroeffnung"] = parse_amount(f.get("eroeffnung"))
    if f.get("waehrung"):
        kwargs["waehrung"] = f.get("waehrung")
    if f.get("eroeffnung_fw") not in (None, ""):
        kwargs["eroeffnung_fw"] = parse_amount(f.get("eroeffnung_fw"))
    return await act(request, api.account_update, f"/konten#k{nr}", ui.book(), nr, **kwargs)


async def kurs(ui: UI, request: Request):
    """Rate hint next to a booking form's currency field (HTMX fragment)."""
    from html import escape
    from .. import fx
    qp = request.query_params
    cur = (qp.get("waehrung") or "").upper()
    if not cur or cur == "CHF":
        return HTMLResponse("")
    try:
        d = date.fromisoformat(qp.get("datum") or date.today().isoformat())
        rate = fx.rate(ui.book(), cur, d)
    except (BookError, ValueError) as exc:
        return HTMLResponse(f'<span class="muted">{escape(str(exc))}</span>')
    return HTMLResponse(f'<span class="muted">BAZG {d:%d.%m.%Y}: 1 {escape(cur)} = CHF {rate}</span>')


# ---------- Debitoren ----------

async def debitoren(ui: UI, request: Request):
    book = ui.book()
    status = request.query_params.get("status", "")
    rows = api.invoice_list(book, status)
    rows.sort(key=lambda r: r["nummer"], reverse=True)
    ar = invoices.aged_receivables(book)
    from .. import erfassung, jev
    return ui.render(request, "debitoren.html", book=book, rows=rows, status=status, ar=ar, tab="rechnungen",
                     drafts=list(erfassung.drafts(book, "debitor").values()), dv=erfassung.value,
                     ocr=bool(erfassung.ocr_languages()), jev=jev.config(book))


async def rechnung(ui: UI, request: Request):
    book = ui.book()
    nr = request.path_params["nr"]
    try:
        meta = invoices.invoice(book, nr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    state = invoices.invoice_state(book, meta)
    paid = invoices.settlements(book).get(nr, [])
    pdf = meta["_pfad"].with_suffix(".pdf")
    if meta.get("extern") and meta.get("datei"):        # issued outside batzen: show the original
        pdf = book.root / meta["datei"]
    return ui.render(request, "rechnung.html", book=book, meta=meta, state=state, paid=paid,
                     pdf=book.rel(pdf) if pdf.exists() else None, tab="rechnungen",
                     today=date.today().isoformat(), bank=book.settings.konto("bank"))


async def rechnung_neu(ui: UI, request: Request):
    book = ui.book()
    custs = invoices.customers(book)
    return ui.render(request, "rechnung_neu.html", book=book, customers=custs, accounts=account_options(book),
                     kunde=request.query_params.get("kunde", ""), default_konto=book.settings.konto("ertrag"),
                     today=date.today().isoformat(), tab="rechnungen")


def _positions(form) -> list[dict]:
    out = []
    codes = form.getlist("p_mwst") or [None] * len(form.getlist("p_text"))
    for text, menge, einheit, preis, konto, code in zip(form.getlist("p_text"), form.getlist("p_menge"),
                                                        form.getlist("p_einheit"), form.getlist("p_preis"),
                                                        form.getlist("p_konto"), codes):
        if not (text or preis):
            continue
        out.append({"text": text, "menge": menge or "1", "einheit": einheit, "preis": preis or "0",
                    "konto": acct(konto) or None, "mwst": code})
    return out


async def rechnung_vorschau(ui: UI, request: Request):
    f = await request.form()
    positions = _positions(f)
    if not positions:
        return ui.partial("_vorschau.html")
    try:
        prev = api.invoice_preview(ui.book(), positions)
    except (BookError, ValueError, ArithmeticError) as exc:
        return ui.partial("_vorschau.html", error=str(exc))
    return ui.partial("_vorschau.html", prev=prev)


async def rechnung_erstellen(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.invoice_create, lambda r: f"/debitoren/rechnung/{r['rechnung']['nummer']}",
                     ui.book(), f.get("kunde"), _positions(f), f.get("datum") or None, f.get("text", ""),
                     int(f.get("zahlungsfrist")) if f.get("zahlungsfrist") else None)


async def rechnung_aktion(ui: UI, request: Request):
    f = await request.form()
    nr, aktion = request.path_params["nr"], request.path_params["aktion"]
    book = ui.book()
    to = f"/debitoren/rechnung/{nr}"
    if aktion == "zahlung":
        return await act(request, api.invoice_pay, to, book, nr, f.get("betrag") or None, f.get("datum") or None,
                         acct(f.get("konto")) or None)
    if aktion == "gutschrift":
        return await act(request, api.invoice_credit, to, book, nr, f.get("betrag") or None, f.get("datum") or None,
                         acct(f.get("konto")) or None, f.get("grund", ""))
    if aktion == "storno":
        return await act(request, api.invoice_void, to, book, nr, f.get("grund", ""))
    return fail("Unbekannte Aktion")


async def zuordnen(ui: UI, request: Request):
    f = await request.form()
    try:
        res = api.invoice_match(ui.book(), f.get("betrag") or "0", f.get("text", ""))
    except (BookError, ValueError, ArithmeticError) as exc:
        return ui.partial("_treffer.html", error=str(exc))
    return ui.partial("_treffer.html", res=res, betrag=f.get("betrag"), today=date.today().isoformat())


async def offene_posten(ui: UI, request: Request):
    book = ui.book()
    stichtag = request.query_params.get("stichtag")
    ar = invoices.aged_receivables(book, parse_date(stichtag) if stichtag else None)
    return ui.render(request, "offene_posten.html", book=book, ar=ar, tab="offene")


async def kunden(ui: UI, request: Request):
    book = ui.book()
    custs = invoices.customers(book)
    states = defaultdict(lambda: {"anzahl": 0, "offen": ZERO})
    paid = invoices.settlements(book)
    for meta in invoices.invoices(book).values():
        st = invoices.invoice_state(book, meta, paid.get(meta["nummer"], []))
        states[meta.get("kunde")]["anzahl"] += 1
        states[meta.get("kunde")]["offen"] += st["offen"]
    return ui.render(request, "kunden.html", book=book, customers=custs, states=states, tab="kunden",
                     edit=request.query_params.get("edit"))


async def kunde_speichern(ui: UI, request: Request):
    f = await request.form()
    fields = {k: (f.get(k) or "").strip() for k in ("name", "firma", "strasse", "nr", "plz", "ort", "land", "email")}
    fields["rechnung_an"] = f.get("rechnung_an") or "firma"
    nr = request.path_params.get("nr")
    if not fields["name"]:
        return fail("Name ist nötig (Kontaktperson oder Privatperson).")
    if nr:
        return await act(request, api.customer_update, "/debitoren/kunden", ui.book(), nr,
                         notizen=f.get("notizen"), **fields)
    return await act(request, api.customer_add, "/debitoren/kunden", ui.book(), notizen=f.get("notizen", ""), **fields)


# ---------- Lohn ----------

def payroll_months(book: Book) -> list[str]:
    months = sorted({f"{p['jahr']}-{int(p['monat']):02d}" for p in payroll.payslips(book)})
    this = f"{date.today().year}-{date.today().month:02d}"
    if this not in months:
        months.append(this)
    return sorted(months)


async def lohn(ui: UI, request: Request):
    book = ui.book()
    months = payroll_months(book)
    monat = request.query_params.get("monat") or next((m for m in months if any(
        p.get("status") != "abgeschlossen" for p in payroll.payslips(book) if f"{p['jahr']}-{int(p['monat']):02d}" == m)),
        months[-1])
    y, m = (int(x) for x in monat.split("-"))
    emps = payroll.employees(book)
    slips = {p["mitarbeiter"]: p for p in payroll.payslips(book, y) if int(p["monat"]) == m}
    rows = []
    for nr, emp in emps.items():
        slip = slips.get(nr)
        if not slip and not emp.get("aktiv", True):
            continue
        rows.append({"emp": emp, "slip": slip, "warnings": payslip_warnings(slip, emp) if slip else []})
    totals = {k: sum((Decimal(str(r["slip"]["werte"][k])) for r in rows if r["slip"]), ZERO)
              for k in ("bruttolohn", "total_abzuege", "nettolohn")}
    return ui.render(request, "lohn.html", book=book, monat=monat, months=months, rows=rows, totals=totals,
                     y=y, m=m, tab="lauf")


async def lohnlauf(ui: UI, request: Request):
    f = await request.form()
    monat = f.get("monat")
    return await act(request, api.payroll_run, f"/lohn?monat={monat}", ui.book(), monat, f.get("mitarbeiter") or None)


async def abrechnung(ui: UI, request: Request):
    book = ui.book()
    monat, nr = request.path_params["monat"], request.path_params["nr"]
    y, m = (int(x) for x in monat.split("-"))
    try:
        slip = payroll.load_payslip(book, y, m, nr)
        emp = payroll.employee(book, nr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    pdf = slip["_pfad"].with_suffix(".pdf")
    cfg = payroll.config(book)
    return ui.render(request, "abrechnung.html", book=book, slip=slip, emp=emp, monat=monat, y=y, m=m,
                     cfg=cfg, warnings=payslip_warnings(slip, emp), pdf=book.rel(pdf) if pdf.exists() else None,
                     ag_order=payroll.AG_ORDER, ag_label=payroll.AG_LABEL, tab="lauf",
                     tarif=payroll.uses_tarif(emp))


async def abrechnung_aktion(ui: UI, request: Request):
    monat, nr, aktion = request.path_params["monat"], request.path_params["nr"], request.path_params["aktion"]
    to = f"/lohn/abrechnung/{monat}/{nr}"
    book = ui.book()
    if aktion == "eingaben":
        f = await request.form()
        inputs = {}
        for key in ("stunden", "bvg", "kinderzulagen", "korrektur", "qst_satzbestimmend", "qst_gesamtpensum"):
            raw = (f.get(key) or "").strip()
            inputs[key] = str(parse_amount(raw)) if raw else None
        inputs["korrektur_text"] = (f.get("korrektur_text") or "").strip()
        return await act(request, api.payslip_inputs, to, book, monat, nr, inputs)
    if aktion == "abschliessen":
        return await act(request, api.payslip_close, to, book, monat, nr)
    if aktion == "oeffnen":
        return await act(request, api.payslip_reopen, to, book, monat, nr)
    return fail("Unbekannte Aktion")


async def mitarbeiter(ui: UI, request: Request):
    book = ui.book()
    from .. import qst
    return ui.render(request, "mitarbeiter.html", book=book, emps=payroll.employees(book),
                     edit=request.query_params.get("edit"), neu=request.query_params.get("neu"),
                     tarife=qst.available(), tab="mitarbeiter")


EMP_TEXT = ("vorname", "nachname", "strasse", "nr", "plz", "ort", "ahv_nr", "geburtsdatum", "eintritt", "austritt", "lohnart")
EMP_NUM = ("monatslohn", "pensum", "stundenlohn", "standard_stunden", "vollzeit_stunden_woche", "bvg_betrag",
           "ag_bvg_betrag", "kinderzulagen", "qst_satz")


async def mitarbeiter_speichern(ui: UI, request: Request):
    f = await request.form()
    fields = {k: (f.get(k) or "").strip() for k in EMP_TEXT}
    for k in EMP_NUM:
        raw = (f.get(k) or "").strip()
        if raw:
            fields[k] = str(parse_amount(raw))
    if (f.get("ferienzuschlag") or "").strip():
        fields["ferienzuschlag_satz"] = str(parse_amount(f.get("ferienzuschlag")) / 100)
    if (f.get("qst_satz_pct") or "").strip():
        fields["qst_satz"] = str(parse_amount(f.get("qst_satz_pct")) / 100)
    fields["ferien_inbegriffen"] = f.get("ferien_inbegriffen") == "1"
    code = (f.get("qst_code") or "").strip().upper()
    if code:
        kanton, _, jahr = (f.get("qst_tabelle") or "").partition("-")
        fields["qst"] = {"kanton": kanton, "jahr": int(jahr or 0), "code": code}
    else:
        fields["qst"] = None
    nr = request.path_params.get("nr")
    book = ui.book()
    if nr:
        fields["aktiv"] = f.get("aktiv") == "1"
        fields = {k: v for k, v in fields.items() if v != "" or k in ("austritt",)}
        return await act(request, api.employee_update, "/lohn/mitarbeiter", book, nr, **fields)
    if not (fields["vorname"] and fields["nachname"]):
        return fail("Vor- und Nachname sind nötig.")
    vorname, nachname = fields.pop("vorname"), fields.pop("nachname")
    fields.pop("austritt", None)
    fields = {k: v for k, v in fields.items() if v not in ("", None) or k == "qst"}
    return await act(request, api.employee_add, "/lohn/mitarbeiter", book, vorname, nachname, **fields)


async def lohnkonto(ui: UI, request: Request):
    book = ui.book()
    year, nr = int(request.path_params["jahr"]), request.path_params["nr"]
    lk = payroll.lohnkonto(book, year, nr)
    emp = payroll.employee(book, nr)
    from .. import lohnausweis
    la = lohnausweis.annual_totals(book, year, nr)
    la_path = book.root / "lohnausweise" / str(year) / f"{nr}.pdf"
    return ui.render(request, "lohnkonto.html", book=book, lk=lk, emp=emp, year=year, la=la,
                     la_pdf=book.rel(la_path) if la_path.exists() else None, tab="mitarbeiter",
                     ag_order=payroll.AG_ORDER, ag_label=payroll.AG_LABEL)


async def lohnausweis_erstellen(ui: UI, request: Request):
    year, nr = int(request.path_params["jahr"]), request.path_params["nr"]
    return await act(request, api.lohnausweis_create, f"/lohn/lohnkonto/{year}/{nr}", ui.book(), year, nr)


# ---------- Abschluss ----------

async def abschluss(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    st = statements.year_end_statement(book, year)
    lock_path = checks.locks_path(book)
    from ..files import read_yaml
    verlauf = (read_yaml(lock_path).get("verlauf") or []) if lock_path.exists() else []
    fx_view = None
    from .. import kreditoren as kred
    has_fw_bills = any(kred.is_foreign(m) for m in kred.bills(book).values())
    if st.get("fremdwaehrung") or has_fw_bills:
        from .. import fx
        try:
            stichtag = date.fromisoformat(request.query_params.get("stichtag", ""))
        except ValueError:
            stichtag = min(date(year, 12, 31), date.today())
        done = sorted(p.stem for p in (book.root / "bewertung").glob(f"{year}-*.yaml"))
        try:
            fx_view = {"stichtag": stichtag, "konten": fx.preview(book, stichtag), "bewertet": fx.path(book, stichtag).exists(),
                       "erledigt": done, "kreditoren": fx.preview_bills(book, stichtag)}
        except BookError as exc:          # no rates yet (future date, offline)
            fx_view = {"stichtag": stichtag, "konten": [], "bewertet": fx.path(book, stichtag).exists(), "fehler": str(exc),
                       "erledigt": done}
    return ui.render(request, "abschluss.html", book=book, year=year, st=st, years=book.years(),
                     anhang=statements.anhang(book, year), verlauf=list(reversed(verlauf))[:5],
                     gv_date=date(year + 1, 6, 30).isoformat(), fx=fx_view)


async def abschluss_aktion(ui: UI, request: Request):
    f = await request.form()
    aktion = request.path_params["aktion"]
    book = ui.book()
    year = int(f.get("jahr") or current_year(book))
    to = f"/abschluss?jahr={year}"
    if aktion == "gewinnverwendung":
        return await act(request, api.allocation_set, to, book, year, f.get("dividende") or "0", f.get("reserve") or "0")
    if aktion == "gewinnverwendung-buchen":
        return await act(request, api.allocation_book, to, book, year, f.get("datum") or None)
    if aktion == "bewertung":
        return await act(request, api.fx_revalue, to, book, f.get("stichtag") or f"{year}-12-31")
    if aktion == "anhang":
        return await act(request, api.anhang_save, to, book, year, f.get("text", ""))
    if aktion == "sperre":
        return await act(request, api.lock, to, book, f.get("bis"))
    if aktion == "entsperren":
        return await act(request, api.unlock, to, book, f.get("bis") or None, f.get("grund", ""))
    return fail("Unbekannte Aktion")


# ---------- Bank ----------

async def bank_page(ui: UI, request: Request):
    from .. import bank, invoices as inv, kreditoren as kred
    book = ui.book()
    status = request.query_params.get("status", "offen")
    rows = [t for t in bank.transactions(book) if not status or t["Status"] == status]
    rows.sort(key=lambda t: (t["Datum"], t["ID"]), reverse=status != "offen")
    paid = inv.settlements(book)
    open_inv = [inv.invoice_state(book, m, paid.get(k, [])) for k, m in inv.invoices(book).items()]
    open_inv = [x for x in open_inv if x["status"] in ("offen", "teilbezahlt")]
    kp = kred.payments(book)
    open_bills = [kred.state(book, m, kp.get(k, [])) for k, m in kred.bills(book).items()]
    open_bills = [x for x in open_bills if x["status"] in ("offen", "angewiesen")]
    counts = defaultdict(int)
    for t in bank.transactions(book):
        counts[t["Status"]] += 1
    from .. import jev
    from .. import plugins
    formats = plugins.bank_formats(book)
    return ui.render(request, "bank.html", book=book, rows=rows, status=status, counts=counts, jev=jev.config(book),
                     bank_accept=",".join(sorted({x for f in formats for x in f.suffixes} | {".camt", ".053"})),
                     bank_labels=", ".join(f.label for f in formats),
                     rec=bank.reconciliation(book), open_inv=open_inv, open_bills=open_bills,
                     accounts=account_options(book))


async def bank_upload(ui: UI, request: Request):
    f = await request.form()
    upload = f.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    tmpdir = Path(tempfile.mkdtemp(prefix="batzen-"))
    tmp = tmpdir / Path(upload.filename).name
    tmp.write_bytes(await upload.read())
    try:
        return await act(request, api.bank_import, "/bank", ui.book(), str(tmp))
    finally:
        if tmp.exists():
            tmp.unlink()


async def bank_jev(ui: UI, request: Request):
    return await act(request, api.bank_suggest, "/bank", ui.book())


async def jev_einstellung(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.settings_update, "/einstellungen#jev", ui.book(),
                     jev={"aktiv": f.get("aktiv") == "1", "schwelle": f.get("schwelle") or None})


async def plugin_einstellung(ui: UI, request: Request):
    f = await request.form()
    fn = api.plugin_enable if request.path_params["aktion"] == "ein" else api.plugin_disable
    return await act(request, fn, "/einstellungen#plugins", ui.book(), f.get("name", ""))


async def bank_aktion(ui: UI, request: Request):
    f = await request.form()
    tid, aktion = request.path_params["id"], request.path_params["aktion"]
    to = request.headers.get("hx-current-url", "/bank")
    book = ui.book()
    if aktion == "buchen":
        return await act(request, api.bank_book, to, book, tid, acct(f.get("konto")), f.get("text", ""), f.get("mwst", ""))
    if aktion == "zuordnen":
        return await act(request, api.bank_assign, to, book, tid, f.get("nummer", ""))
    if aktion == "abgleichen":
        return await act(request, api.bank_link, to, book, tid, (f.get("beleg") or "").strip())
    if aktion == "ignorieren":
        return await act(request, api.bank_ignore, to, book, tid, f.get("grund", ""))
    return fail("Unbekannte Aktion")


# ---------- Kreditoren ----------

def _qr_hint(book: Book, rel: str) -> dict | None:
    """Scan an inbox file for a Swiss QR-bill (quietly: no QR, no hint)."""
    from .. import kreditoren as kred
    try:
        return kred.scan(book, rel)
    except Exception:
        return None


async def kreditoren_page(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    status = request.query_params.get("status", "")
    paid = kred.payments(book)
    rows = [kred.state(book, m, paid.get(k, [])) for k, m in kred.bills(book).items()]
    rows = [r for r in rows if not status or r["status"] == status]
    rows.sort(key=lambda r: (r["status"] not in ("offen", "angewiesen"), r["faellig"], r["nummer"]))
    from .. import erfassung, jev
    drafts = list(erfassung.drafts(book, "kreditor").values())
    return ui.render(request, "kreditoren.html", book=book, rows=rows, status=status, op=kred.open_payables(book),
                     tab="rechnungen", default_date=_next_workday().isoformat(), drafts=drafts,
                     auto_agent=erfassung.agent_auto(book), jev=jev.config(book),
                     ocr=bool(erfassung.ocr_languages()), dv=erfassung.value)


# ---------- Kreditoren drafts: upload → read → account (supplier, Jev, agent) ----------

_agent_jobs = threading.Lock()


def _agent_in_background(root: Path, ids: list[str]) -> None:
    """Hand drafts to the agent one after another; the page refreshes as each draft changes."""
    def work():
        with _agent_jobs:                     # one agent run at a time per process
            for draft_id in ids:
                try:
                    api.bill_draft_agent(Book(root), draft_id)
                except Exception:             # recorded on the draft by run_agent
                    continue
    if ids:
        threading.Thread(target=work, daemon=True).start()


EINGANG_BACK = {"kreditor": "/kreditoren#entwuerfe", "debitor": "/debitoren#entwuerfe", "quittung": "/pruefen#eingang",
                "": "/pruefen#eingang"}


async def eingang_einlesen(ui: UI, request: Request):
    """Upload (or an inbox file from Prüfen) → drafts. PDFs and images are read; anything else
    (bank statements, CSV) only lands in the inbox."""
    from .. import erfassung
    form = await request.form()
    art = (form.get("art") or ("kreditor" if request.url.path.startswith("/kreditoren") else "")).strip()
    uploads = [u for u in form.getlist("dateien") if getattr(u, "filename", "")]
    datei = form.get("datei")
    if not uploads and not datei:
        return fail("Keine Datei gewählt.")
    created, errors_, stored = [], [], 0
    targets = [datei] if datei else []
    for upload in uploads:
        try:
            res = await asyncio.to_thread(api.inbox_add, ui.book(), upload.filename, await upload.read())
        except BookError as exc:
            errors_.append(f"{upload.filename}: {exc}")
            continue
        if file_kind(Path(res["datei"])) in ("pdf", "image") or res["datei"].endswith(".txt"):
            targets.append(res["datei"])
        else:
            stored += 1
    for target in targets:
        try:
            res = await asyncio.to_thread(api.bill_draft_create, ui.book(), target, art)
            created.append(res["entwurf"])
        except BookError as exc:
            errors_.append(f"{Path(target).name}: {exc}")
    if erfassung.agent_auto(ui.book()):
        _agent_in_background(ui.root, [d["id"] for d in created if erfassung.needs_agent(d)])
    if errors_ and not created and not stored:
        return fail(" · ".join(errors_))
    kinds = defaultdict(int)
    for d in created:
        kinds[erfassung.ARTEN[d["art"]]] += 1
    msg = ", ".join(f"{n}× {k}" for k, n in kinds.items()) or "Datei(en) in der Inbox"
    if stored and created:
        msg += f", {stored} weitere Datei(en) in der Inbox"
    if errors_:
        msg += f" · Probleme: {' · '.join(errors_)}"
    return done(msg, EINGANG_BACK.get(art, "/pruefen#eingang"), "warn" if errors_ else "ok")


async def eingang_entwurf(ui: UI, request: Request):
    draft_id, aktion = request.path_params["id"], request.path_params["aktion"]
    back = request.headers.get("hx-current-url") or "/pruefen#eingang"
    if aktion == "agent":
        try:
            await asyncio.to_thread(api.bill_draft_mark, ui.book(), draft_id, "agent", "An den Agenten übergeben")
        except BookError as exc:
            return fail(str(exc))
        _agent_in_background(ui.root, [draft_id])
        return done(f"{draft_id} ist beim Agenten — die Seite aktualisiert sich, sobald er fertig ist", back)
    if aktion == "verwerfen":
        return await act(request, api.bill_draft_discard, back, ui.book(), draft_id)
    if aktion == "art":
        f = await request.form()
        return await act(request, api.bill_draft_update, back, ui.book(), draft_id, "Hand", art=f.get("art", ""))
    return fail("Unbekannte Aktion")


async def quittung_pruefen(ui: UI, request: Request):
    from .. import erfassung
    book = ui.book()
    try:
        meta = erfassung.draft(book, request.query_params.get("entwurf", ""))
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    v = erfassung.form_values(meta)
    pay = meta.get("zahlung") or {}
    from .. import bank
    open_tx = [t for t in bank.transactions(book) if t["Status"] == "offen" and t["Betrag"].startswith("-")]
    liquid = [a for a in book.accounts.values() if a.aktiv_ and (a.klasse == "aktiv" and a.nr.startswith("10")
                                                                    or a.klasse == "passiv")]
    return ui.render(request, "quittung.html", book=book, entwurf=meta, ev=v, pay=pay, open_tx=open_tx,
                     liquid=liquid, kind=file_kind(book.root / meta["datei"]), quellen_felder=meta.get("felder") or {},
                     accounts=account_options(book), currencies=currencies(book), tab="pruefen",
                     today=date.today().isoformat())


async def quittung_buchen(ui: UI, request: Request):
    f = await request.form()
    draft_id = f.get("entwurf", "")
    how = f.get("zahlung") or "konto"
    if how.startswith("bank:"):
        zahlung = {"art": "bank", "bank": how[5:]}
    elif how.startswith("buchung:"):
        zahlung = {"art": "buchung", "beleg": how[8:]}
    else:
        zahlung = {"art": "konto", "konto": acct(f.get("zahlkonto")) or ui.book().settings.konto("bank")}
    codes = f.getlist("p_mwst") or [""] * len(f.getlist("p_konto"))
    lines = [{"konto": acct(k), "betrag": b, "text": t, "mwst": m}
             for k, b, t, m in zip(f.getlist("p_konto"), f.getlist("p_betrag"), f.getlist("p_text"), codes) if k or b]
    cur = (f.get("waehrung") or "CHF").upper()
    return await act(request, api.receipt_book, "/pruefen#eingang", ui.book(), draft_id, f.get("datum"),
                     (f.get("text") or "").strip() or "Quittung", f.get("betrag"), acct(f.get("konto")),
                     f.get("mwst") or "", lines or None, "" if cur == "CHF" else cur, f.get("kurs") or None, zahlung)


async def debitor_extern(ui: UI, request: Request):
    from .. import erfassung
    book = ui.book()
    meta, v = None, {}
    if request.query_params.get("entwurf"):
        try:
            meta = erfassung.draft(book, request.query_params["entwurf"])
        except BookError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        v = erfassung.form_values(meta)
    custs = invoices.customers(book)
    return ui.render(request, "debitor_extern.html", book=book, entwurf=meta, ev=v, customers=custs,
                     chosen=v.get("kunde") if v.get("kunde") in custs else "",
                     kind=file_kind(book.root / meta["datei"]) if meta else None,
                     quellen_felder=(meta or {}).get("felder") or {}, accounts=account_options(book),
                     tab="rechnungen", today=date.today().isoformat())


async def debitor_extern_erfassen(ui: UI, request: Request):
    f = await request.form()
    book = ui.book()
    kunde = f.get("kunde") or ""
    try:
        if kunde == "neu":
            res = await asyncio.to_thread(api.customer_add, book, name=(f.get("c_name") or "").strip(),
                                          firma=(f.get("c_firma") or "").strip(), strasse=f.get("c_strasse", ""),
                                          nr=f.get("c_nr", ""), plz=f.get("c_plz", ""), ort=f.get("c_ort", ""),
                                          land=f.get("c_land") or "CH")
            kunde = res["kunde"]["nummer"]
            book = ui.book()
    except (BookError, ValueError) as exc:
        return fail(str(exc))
    fields = {k: (f.get(k) or "").strip() for k in ("datum", "faellig", "rechnungsnr", "referenz", "mwst", "entwurf")}
    fields = {k: v for k, v in fields.items() if v}
    if f.get("konto"):
        fields["konto"] = acct(f.get("konto"))
    if f.get("datei") and not fields.get("entwurf"):
        fields["datei"] = f.get("datei")
    return await act(request, api.invoice_external, lambda r: f"/debitoren/rechnung/{r['rechnung']['nummer']}", book,
                     kunde, f.get("betrag"), **fields)


async def kreditoren_einstellung(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.settings_update, "/kreditoren#entwuerfe", ui.book(),
                     kreditoren={"agent_automatisch": f.get("agent_automatisch") == "1"})


def _next_workday() -> date:
    from datetime import timedelta
    d = date.today() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


async def kreditor_neu(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    datei = request.query_params.get("datei", "")
    scan, scan_error = None, None
    entwurf_id = request.query_params.get("entwurf", "")
    if entwurf_id:
        from .. import erfassung
        try:
            meta = erfassung.draft(book, entwurf_id)
        except BookError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        v = erfassung.form_values(meta)
        sc = {"betrag": v["betrag"], "iban": v["iban"], "referenz": v["referenz"], "referenz_typ": v["referenz_typ"],
              "mitteilung": v["mitteilung"], "rechnungsinfo": v["rechnungsnr"],
              "kreditor": {k: v[k] for k in ("name", "strasse", "nr", "plz", "ort", "land") if v[k]}}
        kind = file_kind(book.root / meta["datei"])
        sups = kred.suppliers(book)
        return ui.render(request, "kreditor_neu.html", book=book, datei=meta["datei"], kind=kind, scan=None, sc_override=sc,
                         scan_error=None, suppliers=sups, chosen=v["lieferant"] if v["lieferant"] in sups else "",
                         accounts=account_options(book), tab="rechnungen", today=date.today().isoformat(),
                         entwurf=meta, ev=v, quellen_felder=meta.get("felder") or {}, currencies=currencies(book),
                         warnungen=erfassung.warnings(book, meta))
    if datei:
        try:
            scan = kred.scan(book, datei)
        except Exception as exc:
            scan_error = str(exc)
    sups = kred.suppliers(book)
    chosen = (scan or {}).get("lieferant") or request.query_params.get("lieferant") or ""
    kind = file_kind(book.root / datei) if datei else None
    return ui.render(request, "kreditor_neu.html", book=book, datei=datei, kind=kind, scan=scan, scan_error=scan_error,
                     suppliers=sups, chosen=chosen, accounts=account_options(book), tab="rechnungen",
                     today=date.today().isoformat(), currencies=currencies(book))


async def kreditor_erfassen(ui: UI, request: Request):
    from .. import kreditoren as kred
    f = await request.form()
    book = ui.book()
    lieferant = f.get("lieferant") or ""
    try:
        if lieferant == "neu":
            res = await asyncio.to_thread(api.supplier_add, book, name=(f.get("s_name") or "").strip(),
                                          strasse=f.get("s_strasse", ""), nr=f.get("s_nr", ""), plz=f.get("s_plz", ""),
                                          ort=f.get("s_ort", ""), land=f.get("s_land") or "CH", iban=f.get("iban", ""),
                                          konto=acct(f.get("konto")), mwst=f.get("mwst", ""))
            lieferant = res["lieferant"]["nummer"]
            book = ui.book()
    except (BookError, ValueError) as exc:
        return fail(str(exc))
    fields = {k: (f.get(k) or "").strip() for k in ("datum", "faellig", "rechnungsnr", "referenz", "referenz_typ",
                                                     "mitteilung", "iban", "datei", "entwurf")}
    fields = {k: v for k, v in fields.items() if v}
    if f.get("konto"):
        fields["konto"] = acct(f.get("konto"))
    if f.get("mwst") is not None:
        fields["mwst"] = f.get("mwst") or ""
    if (f.get("waehrung") or "CHF").upper() != "CHF":
        fields["waehrung"] = f.get("waehrung").upper()
        if f.get("kurs"):
            fields["kurs"] = f.get("kurs")
    codes = f.getlist("p_mwst") or [""] * len(f.getlist("p_konto"))
    lines = [{"konto": acct(k), "betrag": b, "text": t, "mwst": m}
             for k, b, t, m in zip(f.getlist("p_konto"), f.getlist("p_betrag"), f.getlist("p_text"), codes) if k or b]
    if lines:
        fields["positionen"] = lines
    return await act(request, api.bill_add, lambda r: f"/kreditoren/rechnung/{r['kreditor']['nummer']}", book,
                     lieferant, f.get("betrag"), **fields)


async def kreditor_detail(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    nr = request.path_params["nr"]
    try:
        meta = kred.bill(book, nr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    st = kred.state(book, meta)
    pay_accounts = [a for a in book.accounts.values() if a.klasse == "aktiv" and a.nr.startswith("10") and a.aktiv_
                    and (not a.is_foreign or a.waehrung == st["waehrung"])]
    pay_default = book.settings.konto("bank")
    if kred.is_foreign(meta):
        try:
            pay_default = kred.payment_account(book, st["waehrung"])[1]
        except BookError:
            pass
    return ui.render(request, "kreditor.html", book=book, meta=meta, st=st, paid=kred.payments(book).get(nr, []),
                     booked=kred.booked_chf(book, meta) if kred.is_foreign(meta) else None,
                     pay_accounts=pay_accounts, pay_default=pay_default,
                     kind=file_kind(book.root / meta["datei"]) if meta.get("datei") else None,
                     tab="rechnungen", today=date.today().isoformat())


async def kreditor_aktion(ui: UI, request: Request):
    f = await request.form()
    nr, aktion = request.path_params["nr"], request.path_params["aktion"]
    to = f"/kreditoren/rechnung/{nr}"
    if aktion == "zahlung":
        return await act(request, api.bill_pay, to, ui.book(), nr, f.get("datum") or None, f.get("betrag") or None,
                         f.get("konto") or None, None, f.get("fw") or None)
    if aktion == "storno":
        return await act(request, api.bill_void, to, ui.book(), nr, f.get("grund", ""))
    return fail("Unbekannte Aktion")


async def zahlungslauf_erstellen(ui: UI, request: Request):
    f = await request.form()
    nummern = f.getlist("nr")
    if not nummern:
        return fail("Keine Rechnung ausgewählt.")
    return await act(request, api.payment_run, "/kreditoren/zahlungen", ui.book(), nummern, f.get("datum"))


async def zahlungen_page(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    try:
        debtor, debtor_error = kred.debtor_account(book), None
    except BookError as exc:
        debtor, debtor_error = None, str(exc)
    return ui.render(request, "zahlungen.html", book=book, runs=kred.runs(book), tab="zahlungen",
                     debtor=debtor, debtor_error=debtor_error)


async def zahlungslauf_bezahlt(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.payment_run_book, "/kreditoren/zahlungen", ui.book(), f.get("datei"),
                     f.get("datum") or None)


async def lieferanten_page(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    paid = kred.payments(book)
    open_by = defaultdict(lambda: ZERO)
    for m in kred.bills(book).values():
        st = kred.state(book, m, paid.get(m["nummer"], []))
        open_by[m.get("lieferant")] += st["offen"]
    return ui.render(request, "lieferanten.html", book=book, suppliers=kred.suppliers(book), open_by=open_by,
                     accounts=account_options(book), edit=request.query_params.get("edit"), tab="lieferanten")


async def lieferant_speichern(ui: UI, request: Request):
    f = await request.form()
    fields = {k: (f.get(k) or "").strip() for k in ("name", "strasse", "nr", "plz", "ort", "land", "iban", "email", "mwst")}
    fields["konto"] = acct(f.get("konto"))
    nr = request.path_params.get("nr")
    if not fields["name"]:
        return fail("Name ist nötig.")
    if nr:
        return await act(request, api.supplier_update, "/kreditoren/lieferanten", ui.book(), nr,
                         notizen=f.get("notizen"), **fields)
    return await act(request, api.supplier_add, "/kreditoren/lieferanten", ui.book(), notizen=f.get("notizen", ""),
                     **fields)


# ---------- MWST ----------

ZIFFERN = [
    ("200", "Total der vereinbarten Entgelte", "line"),
    ("220", "Steuerbefreite Leistungen (z.B. Exporte)", "line"),
    ("230", "Von der Steuer ausgenommene Leistungen", "line"),
    ("289", "Total Abzüge", "zw"),
    ("299", "Steuerbares Gesamtentgelt", "zw"),
]
ZIFFERN_EFFEKTIV = [
    ("303", "Leistungen zum Normalsatz 8.1 %", "steuer"),
    ("313", "Leistungen zum reduzierten Satz 2.6 %", "steuer"),
    ("343", "Beherbergungsleistungen 3.8 %", "steuer"),
    ("399", "Total geschuldete Steuer", "zw"),
    ("400", "Vorsteuer auf Material- und Dienstleistungsaufwand", "line"),
    ("405", "Vorsteuer auf Investitionen und übrigem Betriebsaufwand", "line"),
    ("479", "Total Vorsteuer", "zw"),
]


async def mwst_page(ui: UI, request: Request):
    from .. import mwst
    book = ui.book()
    cfg = mwst.config(book)
    year = year_param(request, book)
    periods = mwst.periods(book, year)
    periode = request.query_params.get("periode")
    if not periode:
        current = date.today()
        idx = (current.month - 1) // (6 if cfg["periode"] == "semester" else 3)
        periode = periods[min(idx, len(periods) - 1)] if current.year == year else periods[-1]
    rep = mwst.report(book, periode) if cfg["methode"] != "keine" else None
    overview = [mwst.report(book, p) for p in periods] if cfg["methode"] != "keine" else []
    if cfg["methode"] == "saldo":
        lines = ZIFFERN + [("322", f"Leistungen zum Saldosteuersatz {cfg['saldosteuersatz']} %", "steuer"),
                           ("399", "Total geschuldete Steuer", "zw")]
    else:
        lines = ZIFFERN + ZIFFERN_EFFEKTIV
    return ui.render(request, "mwst.html", book=book, cfg=cfg, rep=rep, year=year, periods=periods,
                     overview=overview, lines=lines, codes=mwst.CODES)


async def mwst_buchen(ui: UI, request: Request):
    f = await request.form()
    periode = f.get("periode")
    return await act(request, api.mwst_book, f"/mwst?periode={periode}&jahr={periode[:4]}", ui.book(), periode)


async def mwst_xml(ui: UI, request: Request):
    from .. import mwst
    book = ui.book()
    periode = request.query_params.get("periode", "")
    try:
        data = mwst.ech0217(book, periode, request.query_params.get("korrektur") == "1")
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    label = mwst.resolve(periode)[2]
    return Response(data, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="eMWST {label}.xml"'})


async def mwst_pdf(ui: UI, request: Request):
    from .. import mwst, pdf as pdfmod
    book = ui.book()
    rep = mwst.report(book, request.query_params.get("periode", ""))
    data = pdfmod.mwst_pdf(book, rep)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="MWST {rep["periode"]}.pdf"'})


# ---------- Verlauf ----------

async def verlauf(ui: UI, request: Request):
    book = ui.book()
    return ui.render(request, "verlauf.html", book=book, log=api.history(book, 200), agent_mark="Agent")


async def commit(ui: UI, request: Request):
    book = ui.book()
    data = api.commit_diff(book, request.path_params["hash"])
    if not data.get("dateien") and not data.get("hash"):
        return PlainTextResponse("Commit nicht gefunden", status_code=404)
    return ui.render(request, "commit.html", book=book, c=data)


# ---------- Einstellungen ----------

async def einstellungen(ui: UI, request: Request):
    book = ui.book()
    from ..qrbill_ch import iban_problem, is_qr_iban, normalize_iban
    iban = normalize_iban(book.settings.get("iban"))
    from .chat import credentials_status
    return ui.render(request, "einstellungen.html", book=book, cfg=payroll.config(book),
                     iban_problem=iban_problem(iban) if iban else "Keine IBAN hinterlegt",
                     qr_iban=is_qr_iban(iban) if iban else False, issues=checks.run(book),
                     accounts=account_options(book), cred=credentials_status(book.settings.get("agent_backend")),
                     jev=__import__("batzen.jev", fromlist=["config"]).config(book),
                     backends=__import__("batzen.web.chat", fromlist=["BACKENDS"]).BACKENDS,
                     plugin_rows=api.plugin_list(book))


async def einstellungen_speichern(ui: UI, request: Request):
    f = await request.form()
    fields = {k: (f.get(k) or "").strip() for k in ("firma", "rechtsform", "uid", "telefon", "email", "iban",
                                                     "qr_referenz_praefix", "zahlungsfrist_tage", "agent_modus", "co",
                                                     "zahlungs_iban")}
    vat = {"methode": f.get("mwst_methode") or "keine", "periode": f.get("mwst_periode") or "",
           "saldosteuersatz": (f.get("mwst_saldosteuersatz") or "").strip(),
           "taetigkeit": (f.get("mwst_taetigkeit") or "").strip(), "abrechnungsart": f.get("mwst_abrechnungsart") or ""}
    adresse = {k: (f.get(f"a_{k}") or "").strip() for k in ("strasse", "nr", "plz", "ort", "land")}
    konten = {k: acct(f.get(f"k_{k}")) for k in ("bank", "debitoren", "ertrag", "gutschrift", "gewinnvortrag",
                                                  "jahresergebnis", "dividende", "reserve")}
    if not fields["firma"]:
        return fail("Firmenname ist nötig.")
    return await act(request, api.settings_update, "/einstellungen", ui.book(), adresse=adresse, konten=konten,
                     mwst=vat if f.get("mwst_methode") is not None else None, **fields)


async def agent_einstellung(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.settings_update, "/einstellungen", ui.book(), agent_backend=f.get("agent_backend") or "auto")


async def lohn_einstellungen(ui: UI, request: Request):
    f = await request.form()
    an = {k: str(parse_amount(f.get(f"an_{k}")) / 100) for k in ("ahv", "alv", "uvg", "ktg") if f.get(f"an_{k}")}
    ag = {k: str(parse_amount(f.get(f"ag_{k}")) / 100) for k in ("ahv", "alv", "fak", "uvg", "ktg", "verwaltung")
          if f.get(f"ag_{k}")}
    return await act(request, api.payroll_config_update, "/einstellungen#lohn", ui.book(), an, ag,
                     f.get("ag_bvg") or None, f.get("buchen") == "1")


# ---------- PDFs on demand ----------

async def pdf_report(ui: UI, request: Request):
    from .. import pdf as pdfmod
    book = ui.book()
    kind = request.path_params["kind"]
    year = year_param(request, book)
    if kind == "jahresrechnung":
        st = statements.year_end_statement(book, year)
        data = pdfmod.statement_pdf(book, st, statements.parse_anhang(statements.anhang(book, year)))
        name = f"Jahresrechnung {year}.pdf"
    elif kind == "journal":
        data = pdfmod.journal_pdf(book, [r for r in book.rows if r.datum.year == year], f"Journal {year}")
        name = f"Journal {year}.pdf"
    elif kind == "kontoblatt":
        nr = request.query_params.get("konto", "")
        data = pdfmod.ledger_pdf(book, account_ledger(book, nr, year))
        name = f"Konto {nr} {year}.pdf"
    elif kind == "debitoren":
        data = pdfmod.receivables_pdf(book, invoices.aged_receivables(book))
        name = "Offene Debitoren.pdf"
    else:
        return PlainTextResponse("Unbekannter Bericht", status_code=404)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


# ---------- routing ----------

def routes(ui: UI) -> list[Route]:
    def h(fn):
        async def handler(request):
            return await fn(ui, request)
        handler.__name__ = fn.__name__
        return handler

    return [
        Route("/", h(uebersicht)),
        Route("/pruefen", h(pruefen)),
        Route("/pruefen/upload", h(inbox_upload), methods=["POST"]),
        Route("/pruefen/buchen", h(pruefen_buchen), methods=["POST"]),
        Route("/vorschlaege/{aktion:str}", h(vorschlag), methods=["POST"]),
        Route("/journal", h(journal_page)),
        Route("/journal/buchen", h(journal_buchen), methods=["POST"]),
        Route("/journal/storno", h(journal_storno), methods=["POST"]),
        Route("/journal/beleg", h(journal_beleg), methods=["POST"]),
        Route("/konten", h(konten)),
        Route("/kurs", h(kurs)),
        Route("/konten/neu", h(konto_neu), methods=["POST"]),
        Route("/konten/saldenliste", h(saldenliste)),
        Route("/konten/{nr:str}", h(kontoblatt)),
        Route("/konten/{nr:str}/aendern", h(konto_aendern), methods=["POST"]),
        Route("/debitoren", h(debitoren)),
        Route("/debitoren/neu", h(rechnung_neu)),
        Route("/debitoren/neu", h(rechnung_erstellen), methods=["POST"]),
        Route("/debitoren/vorschau", h(rechnung_vorschau), methods=["POST"]),
        Route("/debitoren/zuordnen", h(zuordnen), methods=["POST"]),
        Route("/debitoren/offene-posten", h(offene_posten)),
        Route("/debitoren/kunden", h(kunden)),
        Route("/debitoren/kunden/neu", h(kunde_speichern), methods=["POST"]),
        Route("/debitoren/kunden/{nr:str}", h(kunde_speichern), methods=["POST"]),
        Route("/debitoren/rechnung/{nr:str}", h(rechnung)),
        Route("/debitoren/rechnung/{nr:str}/{aktion:str}", h(rechnung_aktion), methods=["POST"]),
        Route("/bank", h(bank_page)),
        Route("/bank/import", h(bank_upload), methods=["POST"]),
        Route("/bank/jev", h(bank_jev), methods=["POST"]),
        Route("/bank/{id:str}/{aktion:str}", h(bank_aktion), methods=["POST"]),
        Route("/kreditoren", h(kreditoren_page)),
        Route("/kreditoren/neu", h(kreditor_neu)),
        Route("/kreditoren/einlesen", h(eingang_einlesen), methods=["POST"]),
        Route("/kreditoren/entwurf/{id:str}/{aktion:str}", h(eingang_entwurf), methods=["POST"]),
        Route("/eingang/einlesen", h(eingang_einlesen), methods=["POST"]),
        Route("/eingang/quittung", h(quittung_pruefen)),
        Route("/eingang/quittung", h(quittung_buchen), methods=["POST"]),
        Route("/eingang/{id:str}/{aktion:str}", h(eingang_entwurf), methods=["POST"]),
        Route("/debitoren/extern", h(debitor_extern)),
        Route("/debitoren/extern", h(debitor_extern_erfassen), methods=["POST"]),
        Route("/kreditoren/einstellung", h(kreditoren_einstellung), methods=["POST"]),
        Route("/kreditoren/neu", h(kreditor_erfassen), methods=["POST"]),
        Route("/kreditoren/rechnung/{nr:str}", h(kreditor_detail)),
        Route("/kreditoren/rechnung/{nr:str}/{aktion:str}", h(kreditor_aktion), methods=["POST"]),
        Route("/kreditoren/zahlungslauf", h(zahlungslauf_erstellen), methods=["POST"]),
        Route("/kreditoren/zahlungslauf/bezahlt", h(zahlungslauf_bezahlt), methods=["POST"]),
        Route("/kreditoren/zahlungen", h(zahlungen_page)),
        Route("/kreditoren/lieferanten", h(lieferanten_page)),
        Route("/kreditoren/lieferanten/neu", h(lieferant_speichern), methods=["POST"]),
        Route("/kreditoren/lieferanten/{nr:str}", h(lieferant_speichern), methods=["POST"]),
        Route("/lohn", h(lohn)),
        Route("/lohn/lauf", h(lohnlauf), methods=["POST"]),
        Route("/lohn/abrechnung/{monat:str}/{nr:str}", h(abrechnung)),
        Route("/lohn/abrechnung/{monat:str}/{nr:str}/{aktion:str}", h(abrechnung_aktion), methods=["POST"]),
        Route("/lohn/mitarbeiter", h(mitarbeiter)),
        Route("/lohn/mitarbeiter/neu", h(mitarbeiter_speichern), methods=["POST"]),
        Route("/lohn/mitarbeiter/{nr:str}", h(mitarbeiter_speichern), methods=["POST"]),
        Route("/lohn/lohnkonto/{jahr:int}/{nr:str}", h(lohnkonto)),
        Route("/lohn/lohnausweis/{jahr:int}/{nr:str}", h(lohnausweis_erstellen), methods=["POST"]),
        Route("/abschluss", h(abschluss)),
        Route("/abschluss/{aktion:str}", h(abschluss_aktion), methods=["POST"]),
        Route("/mwst", h(mwst_page)),
        Route("/mwst/buchen", h(mwst_buchen), methods=["POST"]),
        Route("/mwst/pdf", h(mwst_pdf)),
        Route("/mwst/xml", h(mwst_xml)),
        Route("/verlauf", h(verlauf)),
        Route("/verlauf/{hash:str}", h(commit)),
        Route("/einstellungen", h(einstellungen)),
        Route("/einstellungen", h(einstellungen_speichern), methods=["POST"]),
        Route("/einstellungen/lohn", h(lohn_einstellungen), methods=["POST"]),
        Route("/einstellungen/agent", h(agent_einstellung), methods=["POST"]),
        Route("/einstellungen/jev", h(jev_einstellung), methods=["POST"]),
        Route("/einstellungen/plugins/{aktion:str}", h(plugin_einstellung), methods=["POST"]),
        Route("/pdf/{kind:str}", h(pdf_report)),
    ]

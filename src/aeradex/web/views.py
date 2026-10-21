"""Page handlers. Each GET collects data from the book and renders a template;
each POST calls exactly one `api` write through `act`."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import tempfile
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import api, check as checks, invoices, jahreswechsel, journal, payroll, statements
from ..book import Book, BookError
from ..files import FormatError, parse_amount, parse_date
from ..ledger import BalanceEngine, account_ledger, movements, resolve_period, trial_balance
from .app import UI, acct, act, datum, done, fail

ZERO = Decimal("0")
QUELLEN = {"": "Alle Quellen", "manuell": "Manuell", "rechnung": "Rechnungen", "zahlung": "Zahlungen",
           "gutschrift": "Gutschriften", "kreditor": "Kreditoren", "kzahlung": "Kreditorenzahlungen", "lohn": "Lohn", "abschluss": "Abschluss",
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


def unread_inbox(book: Book) -> list[Path]:
    """Files in the inbox that are not read in yet (no draft points to them)."""
    from .. import erfassung
    inbox = book.root / "inbox"
    drafted = {d.get("datei") for d in erfassung.drafts(book).values()}
    return [p for p in sorted(inbox.iterdir()) if p.is_file() and not p.name.startswith(".")
            and f"inbox/{p.name}" not in drafted] if inbox.exists() else []


def todo_items(book: Book) -> list[dict]:
    """Everything that waits for a person, one line per stack, most urgent first. Each line leads to
    the one place where that work is done."""
    from .. import bank, erfassung
    items = []
    issues = checks.run(book)
    for i in issues:
        if i.level == "fehler":
            items.append({"level": "red", "title": i.message, "detail": i.where, "tag": "Fehler", "href": "/einstellungen#pruefung"})
    unread = unread_inbox(book)
    if unread:
        items.append({"level": "info", "title": f"{len(unread)} Datei{'en' if len(unread) > 1 else ''} in der Inbox einlesen",
                      "detail": " · ".join(p.name for p in unread[:3]) + (" …" if len(unread) > 3 else ""),
                      "tag": "Inbox", "href": "#inbox"})
    drafts = list(erfassung.drafts(book).values())
    for group, label, href in (("einkauf", "Einkauf", "/kreditoren?status=entwurf"),
                               ("verkauf", "Verkauf", "/debitoren?status=entwurf")):
        mine = [d for d in drafts if (d.get("art") == "debitor") == (group == "verkauf")]
        if mine:
            ready = sum(1 for d in mine if d.get("status") == "bereit")
            items.append({"level": "warn" if any(d.get("status") == "konflikt" for d in mine) else "info",
                          "title": f"{len(mine)} Entw{'ürfe' if len(mine) > 1 else 'urf'} im {label} freigeben",
                          "detail": f"{ready} bereit" + (f" · {len(mine) - ready} brauchen noch Angaben" if ready < len(mine) else ""),
                          "tag": "Entwürfe", "href": href})
    open_tx = [t for t in bank.transactions(book) if t["Status"] == "offen"]
    if open_tx:
        sure = sum(1 for o in bank.suggestions(book).values() if o[0]["sicher"])
        items.append({"level": "info", "title": f"{len(open_tx)} Bankbewegung{'en' if len(open_tx) > 1 else ''} abgleichen",
                      "detail": f"{sure} mit sicherem Vorschlag" if sure else "ohne sicheren Vorschlag",
                      "tag": "Bank", "href": "/bank"})
    proposals = journal.list_proposals(book)
    if proposals:
        items.append({"level": "info", "title": f"{len(proposals)} Vorschl{'äge' if len(proposals) > 1 else 'ag'} des Agenten freigeben",
                      "detail": " · ".join(p["Text"] for p in proposals[:3]), "tag": "Vorschläge", "href": "/vorschlaege"})
    slips_by_month = defaultdict(list)
    for slip in payroll.payslips(book):
        if slip.get("status") != "abgeschlossen":
            slips_by_month[f"{slip['jahr']}-{int(slip['monat']):02d}"].append(slip)
    for month, slips in sorted(slips_by_month.items()):
        items.append({"level": "warn", "title": f"Löhne {month[5:]}/{month[:4]} abschliessen",
                      "detail": " · ".join(f"{s.get('name')} {Decimal(str(s['werte']['nettolohn'])):,.2f} netto".replace(",", "'") for s in slips),
                      "tag": f"{len(slips)} Entwurf" + ("e" if len(slips) > 1 else ""), "href": f"/lohn?monat={month}"})
    from .. import kunden as crm
    for e in crm.wiedervorlagen(book, date.today())[:6]:
        items.append({"level": "warn" if e["nachfassen"] < date.today().isoformat() else "info",
                      "title": f"Nachfassen: {e['name']}", "detail": f"{datum(e['nachfassen'])} · {e['text']}",
                      "tag": "Wiedervorlage", "href": f"/debitoren/kunden/{e['kunde']}"})
    from .. import plugins
    for t in plugins.todos(book):
        items.append({"level": t.level, "title": t.titel, "detail": t.detail, "tag": t.tag, "href": t.href})
    from .. import jahresabschluss
    due = jahresabschluss.due_year(book)
    if due is not None:
        try:
            cl = jahresabschluss.checklist(book, due)
        except BookError:
            cl = None
        if cl and cl["erledigt"] < cl["total"]:
            todo = [st["titel"] for ph in cl["phasen"] for st in ph["schritte"] if not st["erledigt"]]
            items.append({"level": "warn" if date.today() > date(due, 12, 31) else "info",
                          "title": f"Abschluss {due}: {cl['erledigt']} von {cl['total']} erledigt",
                          "detail": " · ".join(todo[:3]) + (" …" if len(todo) > 3 else ""), "tag": "Abschluss",
                          "href": f"/abschluss?jahr={due}"})
    from .. import kreditoren as kred
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
    from .mobil_views import wants_phone
    if wants_phone(request):
        return RedirectResponse("/m", status_code=303)
    book = ui.book()
    data = await asyncio.to_thread(overview_data, book)
    return ui.render(request, "uebersicht.html", book=book, d=data, todos=todo_items(book), inbox=unread_inbox(book))


# ---------- Prüfen ----------

def payslip_warnings(slip: dict, emp: dict) -> list[str]:
    warnings = []
    w = slip.get("werte") or {}
    if payroll.uses_tarif(emp) and 'qst_basis' not in w:
        warnings.append("Ältere QST-Berechnung: Entwurf neu rechnen; abgeschlossene Abrechnung prüfen")
    if not Decimal(str(w.get("bruttolohn") or 0)):
        warnings.append("Bruttolohn ist 0")
    return warnings


async def pruefen(ui: UI, request: Request):
    """The old review page: its parts now live where the work is done (Übersicht, Einkauf/Verkauf
    › Entwürfe, Bank › Abgleichen, Buchhaltung › Vorschläge)."""
    return RedirectResponse("/#inbox" if request.query_params.get("datei") else "/", status_code=303)


async def vorschlaege_page(ui: UI, request: Request):
    book = ui.book()
    return ui.render(request, "vorschlaege.html", book=book, proposals=journal.list_proposals(book))


async def inbox_upload(ui: UI, request: Request):
    form = await request.form()
    upload = form.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    data = await upload.read()
    return await act(request, api.inbox_add, "/#inbox", ui.book(),
                     upload.filename, data)


async def pruefen_buchen(ui: UI, request: Request):
    f = await request.form()
    datei = f.get("datei") or None
    return await act(request, api.post_entry, "/journal", ui.book(), f.get("datum"), acct(f.get("soll")),
                     acct(f.get("haben")), f.get("betrag"), f.get("text", ""), "", datei and f"inbox/{datei}",
                     f.get("mwst", ""), f.get("waehrung", ""), f.get("kurs") or None)


async def vorschlag(ui: UI, request: Request):
    f = await request.form()
    ids = f.getlist("id") or [request.path_params.get("id")]
    if request.path_params["aktion"] == "freigeben":
        return await act(request, api.approve, request.headers.get("hx-current-url", "/vorschlaege"), ui.book(), ids)
    return await act(request, api.reject, request.headers.get("hx-current-url", "/vorschlaege"), ui.book(), ids)


# ---------- Journal ----------

def _amount_filter(raw: str):
    """'120' → exactly 120.00, '100-200' → a range, '>500' / '<50' → open ranges; None = no filter."""
    import re as _re
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        m = _re.fullmatch(r"([<>])\s*(.+)", raw)
        if m:
            v = Decimal(grid_amount(m.group(2)))
            return (lambda x: x > v) if m.group(1) == ">" else (lambda x: x < v)
        m = _re.fullmatch(r"(.+?)\s*[-–]\s*(.+)", raw)
        if m:
            lo, hi = Decimal(grid_amount(m.group(1))), Decimal(grid_amount(m.group(2)))
            return lambda x: lo <= x <= hi
        v = Decimal(grid_amount(raw))
        return lambda x: x == v
    except (InvalidOperation, ValueError):
        return None


def journal_groups(book: Book, year: int, month: int | None, konto: str, quelle: str, q: str,
                   ohne_beleg: bool, mwst_code: str = "", betrag: str = "") -> list[dict]:
    rows = [r for r in book.rows if r.datum.year == year and (not month or r.datum.month == month)]
    amount_ok = _amount_filter(betrag)
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
        if q and not any(q.lower() in f"{r.text} {r.beleg} {r.soll} {r.haben} {r.betrag:.2f}".lower() for r in group):
            continue
        if mwst_code == "ohne" and any(r.mwst for r in group):
            continue
        if mwst_code and mwst_code != "ohne" and not any(r.mwst == mwst_code for r in group):
            continue
        total = sum((r.betrag for r in group if r.soll), ZERO)
        if amount_ok and not (amount_ok(total) or any(amount_ok(r.betrag) for r in group)):
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
            "total": total,
            "receipts": receipts, "locked": bool(lock and first.datum <= lock),
            "recode": (kind in ("manuell", "kreditor") and not (lock and first.datum <= lock)
                       and not (kind == "manuell" and any(r.waehrung for r in group))),
            "doc": doc_link(first.quelle, book),
            "edit": (journal.edit_lines(book, group)
                     if not first.quelle and not (lock and first.datum <= lock) and not any(r.waehrung for r in group)
                     else None),
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
        return f"/lohn/abrechnung/{month}/{nr}" if nr else f"/lohn?monat={month}"
    if kind == "abschluss":
        return f"/abschluss?jahr={ref}"
    if kind == "bewertung":
        return f"/abschluss?jahr={ref[:4]}&stichtag={ref}"
    return None


async def journal_aendern(ui: UI, request: Request):
    f = await request.form()
    zeilen = [{"soll": acct(s), "haben": acct(h), "betrag": b, "text": t, "mwst": m}
              for s, h, b, t, m in zip(f.getlist("z_soll"), f.getlist("z_haben"), f.getlist("z_betrag"),
                                       f.getlist("z_text"), f.getlist("z_mwst") or [""] * len(f.getlist("z_soll")))]
    back = request.headers.get("hx-current-url") or "/journal"
    return await act(request, api.amend_entry, back, ui.book(), f.get("beleg", ""), f.get("datum"),
                     f.get("text", ""), zeilen)


async def journal_page(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    qp = request.query_params
    konto = acct(qp.get("konto", ""))        # a datalist choice arrives as '1020  Bank'
    filters = (konto, qp.get("quelle", ""), qp.get("q", ""), bool(qp.get("ohne_beleg")),
               qp.get("mwst", "").upper() if qp.get("mwst") != "ohne" else "ohne", qp.get("betrag", ""))
    # Month tabs count what the filters match, so a filter never hides bookings in another month unnoticed.
    counts = defaultdict(int)
    for g in journal_groups(book, year, None, *filters):
        counts[g["datum"].month] += len(g["rows"])
    month_raw = qp.get("monat")
    if month_raw is None:
        month = max(counts) if counts else date.today().month
    else:
        month = int(month_raw) if month_raw.isdigit() and 1 <= int(month_raw) <= 12 else None
    groups = journal_groups(book, year, month, *filters)
    return ui.render(request, "journal.html", book=book, year=year, month=month, counts=counts, groups=groups,
                     wer=_who_booked(book, groups),
                     names={a.nr: a.name for a in book.accounts.values()}, accounts=account_options(book),
                     quellen=QUELLEN, q=qp.get("q", ""), konto=konto, quelle=qp.get("quelle", ""),
                     mwst_filter=filters[4], betrag_filter=filters[5],
                     ohne_beleg=bool(qp.get("ohne_beleg")), next_beleg=journal.next_beleg(book, year),
                     currencies=currencies(book),
                     account_names={a.nr: a.name + (f" ({a.waehrung})" if a.is_foreign else "")
                                    for a in book.accounts.values() if a.aktiv_})


def _who_booked(book: Book, groups: list[dict]) -> dict:
    """Per Beleg shown: short name of who booked it, and a tooltip with who booked and changed it when."""
    from .. import gitlog
    index = gitlog.beleg_index(book.root)
    out = {}
    for g in groups:
        events = index.get(g["beleg"]) or []
        if not events:
            continue
        first = next((e for e in events if e["art"] == "erfasst"), events[0])
        changed = [e for e in events if e["art"] == "geändert"]
        tip = f"erfasst von {first['autor']}, {first['datum'][:16]}"
        if changed:
            tip += f" · zuletzt geändert von {changed[-1]['autor']}, {changed[-1]['datum'][:16]}"
        out[g["beleg"]] = {"kurz": gitlog.short_name(first["autor"], first.get("email", "")),
                           "agent": gitlog.is_agent(first["autor"], first.get("email", "")),
                           "geaendert": bool(changed), "tip": tip}
    return out


async def journal_buchen(ui: UI, request: Request):
    f = await request.form()
    book = ui.book()
    upload = f.get("datei")
    tmp = None
    if upload is not None and getattr(upload, "filename", ""):
        tmpdir = Path(tempfile.mkdtemp(prefix="aeradex-"))
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


def grid_date(raw: str, previous: date | None, year: int) -> date:
    """Excel-ish dates: 2026-03-05, 05.03.2026, 5.3.26, 5.3. (year of the line above) — empty = as above."""
    import re as _re
    raw = (raw or "").strip()
    if not raw:
        if previous is None:
            raise BookError("Datum fehlt")
        return previous
    m = _re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?(\d{2}|\d{4})?", raw)
    if m:
        y = m.group(3)
        y = (2000 + int(y) if len(y) == 2 else int(y)) if y else (previous.year if previous else year)
        try:
            return date(y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            raise BookError(f"'{raw}' ist kein Datum") from None
    return parse_date(raw, "datum")


def grid_amount(raw: str) -> str:
    """'1'234.50', '1 234,50', '1234,5', 'CHF 12.00' → '1234.50'."""
    import re as _re
    text = _re.sub(r"(?i)chf|fr\.|sfr|['’ \u00a0]", "", raw or "").strip()
    if "," in text and "." in text:
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    return text


async def journal_raster(ui: UI, request: Request):
    """The booking grid: every non-empty line is one booking; all or none are booked."""
    f = await request.form()
    book = ui.book()
    year = int(f.get("jahr") or date.today().year)
    cols = {k: f.getlist(k) for k in ("datum", "text", "soll", "haben", "betrag", "mwst")}
    files = f.getlist("datei")              # one file field per grid row, empty where nothing is attached
    refs, groups = f.getlist("ref"), f.getlist("gruppe")   # a row read from a receipt: its file, its statement
    n = max((len(v) for v in cols.values()), default=0)
    tmpdir = Path(tempfile.mkdtemp(prefix="aeradex-"))
    entries, where, problems, previous = [], [], {}, None
    for i in range(n):
        cell = {k: (v[i] if i < len(v) else "").strip() for k, v in cols.items()}
        if not any(cell[k] for k in ("text", "soll", "haben", "betrag")):
            if cell["datum"]:
                previous = None
            continue
        try:
            previous = grid_date(cell["datum"], previous, year)
        except (BookError, ValueError) as exc:
            problems[i + 1] = str(exc)
            continue
        entry = {"datum": previous.isoformat(), "text": cell["text"], "soll": acct(cell["soll"]),
                 "haben": acct(cell["haben"]), "betrag": grid_amount(cell["betrag"]),
                 "mwst": cell["mwst"].split(" ", 1)[0].upper()}
        if entry["betrag"].startswith("-"):     # a refund on a statement: the other way round
            entry["soll"], entry["haben"], entry["betrag"] = entry["haben"], entry["soll"], entry["betrag"][1:]
        ref = refs[i] if i < len(refs) else ""
        if ref:
            from .. import auslesen
            try:
                entry["datei"] = str(auslesen.file_of(book, ref))
            except BookError as exc:
                problems[i + 1] = str(exc)
                continue
            if i < len(groups) and groups[i]:
                entry["gruppe"] = groups[i]
        upload = files[i] if i < len(files) else None
        if upload is not None and getattr(upload, "filename", "") and not ref:
            tmp = tmpdir / f"{i}" / Path(upload.filename).name
            tmp.parent.mkdir()
            tmp.write_bytes(await upload.read())
            entry["datei"] = str(tmp)
        entries.append(entry)
        where.append(i + 1)
    if not entries and not problems:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return fail("Keine Buchungen im Raster.")
    if not problems:
        try:
            result = await asyncio.to_thread(api.post_entries, book, entries)
        except api.RowErrors as exc:
            problems = {where[k - 1]: msg for k, msg in exc.fehler.items()}
        except BookError as exc:
            return fail(str(exc))
        else:
            from .. import auslesen
            auslesen.cleanup(book, {r for r in refs if r})
            return done(result.get("meldung", "Gebucht"), request.headers.get("hx-current-url") or "/journal")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    shutil.rmtree(tmpdir, ignore_errors=True)
    response = fail("Nichts gebucht — " + "; ".join(f"Zeile {k}: {v}" for k, v in sorted(problems.items())))
    response.headers["HX-Trigger"] = json.dumps({"rasterFehler": {str(k): v for k, v in problems.items()}})
    return response


async def journal_vorlage(ui: UI, request: Request):
    data = await asyncio.to_thread(api.journal_template, ui.book())
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="Buchungen Vorlage.xlsx"'})


async def journal_import(ui: UI, request: Request):
    """A filled-in template → rows for the grid (JSON); the grid books them."""
    f = await request.form()
    upload = f.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return JSONResponse({"fehler": "Keine Datei gewählt."}, status_code=400)
    try:
        res = await asyncio.to_thread(api.journal_import_read, ui.book(), await upload.read(), upload.filename)
    except BookError as exc:
        return JSONResponse({"fehler": str(exc)}, status_code=400)
    return JSONResponse(res)


async def journal_auslesen(ui: UI, request: Request):
    """A receipt or a card statement → lines for the grid (JSON), counter account open. A statement (or a receipt
    Jev cannot account) is read by the agent, which takes a while."""
    f = await request.form()
    upload = f.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return JSONResponse({"fehler": "Keine Datei gewählt."}, status_code=400)
    try:
        res = await asyncio.to_thread(api.journal_read, ui.book(), upload.filename, await upload.read())
    except BookError as exc:
        return JSONResponse({"fehler": str(exc)}, status_code=400)
    return JSONResponse(res)


def _recode_form(f) -> tuple[list[str], str, str, str]:
    return f.getlist("beleg"), acct(f.get("konto_alt", "")), acct(f.get("konto_neu", "")), f.get("mwst_neu", "")


async def journal_umbuchen_vorschau(ui: UI, request: Request):
    """The preview of a recode: before/after per Beleg, skipped ones with their reason (HTML fragment)."""
    f = await request.form()
    belege, alt, neu, code = _recode_form(f)
    book = ui.book()
    try:
        res = await asyncio.to_thread(api.recode_preview, book, belege, alt, neu, code)
    except BookError as exc:
        return fail(str(exc))
    return ui.render(request, "_umbuchen_vorschau.html", res=res, alt=alt, neu=neu, code=code,
                     names={a.nr: a.name for a in book.accounts.values()})


async def journal_umbuchen(ui: UI, request: Request):
    f = await request.form()
    belege, alt, neu, code = _recode_form(f)
    return await act(request, api.recode, request.headers.get("hx-current-url") or "/journal", ui.book(),
                     belege, alt, neu, code)


async def journal_storno(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.reverse_entry, request.headers.get("hx-current-url", "/journal"), ui.book(),
                     f.get("beleg"), f.get("datum") or None, f.get("text", ""))


async def journal_beleg(ui: UI, request: Request):
    f = await request.form()
    upload = f.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    tmpdir = Path(tempfile.mkdtemp(prefix="aeradex-"))
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
                     groups=statements.GROUPS, eigenkapital=book.settings.konto("gewinnvortrag")
                     if book.settings.rechtsform_art == "einzelfirma" else "")


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
                     periode=periode, totals=totals)


def _balance_chart(led: dict, year: int, flip: bool) -> dict | None:
    """Saldoverlauf as a step line over the year (end-of-day balances). Passive and revenue accounts
    are drawn with the credit side up, so a growing liability or revenue rises."""
    sign = Decimal(-1) if flip else Decimal(1)
    start, end = date(year, 1, 1), date(year, 12, 31)
    if year == date.today().year:
        end = max(date.today(), max((z["datum"] for z in led["zeilen"]), default=start))
    points = [(start, led["eroeffnung"] * sign)]
    for z in led["zeilen"]:
        if len(points) > 1 and points[-1][0] == z["datum"]:
            points[-1] = (z["datum"], z["saldo"] * sign)
        else:
            points.append((z["datum"], z["saldo"] * sign))
    if len(points) < 2 and not led["zeilen"]:
        return None
    values = [v for _, v in points]
    lo, hi = min(min(values), ZERO), max(max(values), ZERO)
    step = _nice_step(max(abs(lo), abs(hi)) or Decimal(100))
    lo = (lo / step).to_integral_value(rounding="ROUND_FLOOR") * step
    hi = (hi / step).to_integral_value(rounding="ROUND_CEILING") * step
    if hi == lo:
        hi = lo + step
    left, right, top, bottom = 70.0, 710.0, 12.0, 168.0
    days = max((end - start).days, 1)
    x = lambda d: left + (right - left) * min((d - start).days, days) / days  # noqa: E731
    y = lambda v: bottom - (bottom - top) * float((v - lo) / (hi - lo))  # noqa: E731
    path = f"M{x(points[0][0]):.1f},{y(points[0][1]):.1f}"
    for (d0, v0), (d1, v1) in zip(points, points[1:]):
        path += f" H{x(d1):.1f} V{y(v1):.1f}"
    path += f" H{x(end):.1f}"
    ticks = []
    v = lo
    while v <= hi:
        ticks.append({"y": round(y(v), 1), "v": v})
        v += step
    months = [{"x": round(x(date(year, m, 1)), 1), "m": m} for m in range(1, 13)
              if date(year, m, 1) <= end and x(date(year, m, 1)) < right - 24]
    ends = []
    for m in range(1, 13):
        last = date(year, m + 1, 1) - timedelta(days=1) if m < 12 else date(year, 12, 31)
        if date(year, m, 1) > end:
            break
        val = next((v for d, v in reversed(points) if d <= last), points[0][1])
        ends.append({"x": round(x(min(last, end)), 1), "y": round(y(val), 1), "m": m, "v": val})
    return {"path": path, "ticks": ticks, "months": months, "ends": ends, "zero": round(y(ZERO), 1),
            "area": path + f" V{y(ZERO):.1f} H{x(start):.1f} Z", "flip": flip,
            "min": min(values), "max": max(values)}


async def kontoblatt(ui: UI, request: Request):
    book = ui.book()
    year = year_param(request, book)
    nr = request.path_params["nr"]
    window = None
    try:
        q = request.query_params
        if q.get("von") or q.get("bis"):          # drill-down from a report: just that period
            window = (max(parse_date(q.get("von") or f"{year}-01-01"), date(year, 1, 1)),
                      min(parse_date(q.get("bis") or f"{year}-12-31"), date(year, 12, 31)))
        led = account_ledger(book, nr, year, start=window[0] if window else None, end=window[1] if window else None)
    except (BookError, FormatError) as exc:
        return PlainTextResponse(str(exc), status_code=404)
    acct = book.accounts[nr]
    numbers = sorted(n for n, a in book.accounts.items() if a.aktiv_ or n == nr)
    i = numbers.index(nr)
    chart = _balance_chart(led, year, acct.klasse in ("passiv", "ertrag"))
    return ui.render(request, "kontoblatt.html", book=book, year=year, led=led, chart=chart, window=window,
                     klasse=acct.klasse, prev_nr=numbers[i - 1] if i > 0 else None,
                     next_nr=numbers[i + 1] if i + 1 < len(numbers) else None,
                     konten_liste=[book.accounts[n] for n in numbers])


async def konto_neu(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.add_account, "/konten", ui.book(), f.get("nr", "").strip(), f.get("name", "").strip(),
                     f.get("klasse", ""), f.get("gruppe", ""), f.get("waehrung", ""))


async def konto_aendern(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    kwargs = {"name": f.get("name"), "gruppe": f.get("gruppe") or None, "aktiv": f.get("aktiv") == "1"}
    if "abschluss_feld" in f:
        kwargs["abschluss"] = ui.book().settings.konto("gewinnvortrag") if f.get("abschluss") == "1" else ""
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
    drafts = group_drafts(book, "verkauf")
    status = request.query_params.get("status") or ("entwurf" if drafts else "offen")
    if status != "entwurf" and status not in VERKAUF_TABS:
        status = "alle"
    wanted = VERKAUF_TABS.get(status)
    rows = [r for r in api.invoice_list(book) if wanted is None or r["status"] in wanted] if status != "entwurf" else []
    rows.sort(key=lambda r: r["nummer"], reverse=True)
    metas = invoices.invoices(book) if rows else {}
    for r in rows:
        r["suche"] = invoice_search_text(r, metas.get(r["nummer"], {}))
    ar = invoices.aged_receivables(book)
    from .. import erfassung, jev
    return ui.render(request, "debitoren.html", book=book, rows=rows, status=status, ar=ar,
                     q=request.query_params.get("q", ""), drafts=drafts, dv=erfassung.value,
                     ocr=bool(erfassung.ocr_languages()), jev=jev.config(book))


def _amount_words(value) -> list[str]:
    """An amount as people type it: 1920, 1920.00, 1'920.00."""
    if value in (None, ""):
        return []
    v = Decimal(str(value))
    return [f"{v:.2f}", f"{v:,.2f}".replace(",", "'")] + ([f"{v:.0f}"] if v == v.to_integral_value() else [])


def invoice_search_text(row: dict, meta: dict) -> str:
    """Everything the invoice list search looks in: number, customer, dates, amounts, reference, the
    invoice text and every position."""
    an = meta.get("an") or {}
    parts = [row.get("nummer"), row.get("kunde"), row.get("name"), an.get("zusatz"), an.get("ort"), row.get("status"),
             row.get("referenz"), row.get("extern"), meta.get("_text"),
             *(datum(row.get(k)) for k in ("datum", "faellig") if row.get(k)),
             *_amount_words(row.get("total")),
             *(str(p.get("text") or "") for p in meta.get("positionen") or [])]
    return " ".join(str(x) for x in parts if x)


def customer_search_text(nr: str, c: dict) -> str:
    a = c.get("adresse") or {}
    log = [e.get("text") for e in c.get("log") or [] if isinstance(e, dict)]
    return " ".join(str(x) for x in (nr, c.get("firma"), c.get("name"), a.get("strasse"), a.get("nr"), a.get("plz"),
                                     a.get("ort"), c.get("email"), c.get("telefon"), c.get("mobil"),
                                     c.get("kategorie"), c.get("herkunft"), c.get("_notizen"), *log) if x)


async def mahnungen_page(ui: UI, request: Request):
    from .. import mahnungen
    book = ui.book()
    return ui.render(request, "mahnungen.html", book=book, rows=mahnungen.overdue(book),
                     today=date.today().isoformat(), frist=mahnungen.config(book)["frist_tage"])


async def mahnungen_erstellen(ui: UI, request: Request):
    f = await request.form()
    nummern = f.getlist("nr")
    if not nummern:
        return fail("Keine Rechnung gewählt.")
    frist = int(f.get("frist")) if (f.get("frist") or "").isdigit() else None
    return await act(request, api.reminder_create, "/debitoren/mahnungen", ui.book(), nummern,
                     f.get("datum") or None, frist)


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
    if meta.get("extern") and meta.get("datei"):        # issued outside aeradex: show the original
        pdf = book.root / meta["datei"]
    from .. import mahnungen
    from .. import pdf as pdfmod
    return ui.render(request, "rechnung.html", book=book, meta=meta, state=state, paid=paid,
                     abzuege=pdfmod.invoice_deductions(book, meta), art=pdfmod.invoice_kind(meta),
                     angerechnet_in=invoices.angerechnet(book).get(nr) if invoices.is_akonto(meta) else None,
                     reminders=mahnungen.history(book, nr),
                     pdf=book.rel(pdf) if pdf.exists() else None,
                     today=date.today().isoformat(), bank=book.settings.konto("bank"))


async def rechnung_neu(ui: UI, request: Request):
    book = ui.book()
    custs = invoices.customers(book)
    kunde = request.query_params.get("kunde", "")
    first = kunde if kunde in custs else next(iter(custs), "")
    return ui.render(request, "rechnung_neu.html", book=book, customers=custs, accounts=account_options(book),
                     kunde=kunde, offen=invoices.open_akonto(book, first) if first else [],
                     objekt=request.query_params.get("objekt", ""), art=request.query_params.get("art", ""),
                     default_konto=book.settings.konto("ertrag"),
                     today=date.today().isoformat())


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


def _art(f) -> tuple[str, str | None, list[str]]:
    """Art, Rabatt and the ticked Akontorechnungen of the invoice form (an Akontorechnung has neither)."""
    art = f.get("art") or "rechnung"
    if art == "akonto":
        return art, None, []
    return art, f.get("rabatt") or None, f.getlist("akonto")


async def rechnung_vorschau(ui: UI, request: Request):
    f = await request.form()
    positions = _positions(f)
    if not positions:
        return ui.partial("_vorschau.html")
    art, rabatt, akonto = _art(f)
    book = ui.book()
    try:
        prev = api.invoice_preview(book, positions, f.get("kunde") or "", rabatt, art, akonto)
    except (BookError, ValueError, ArithmeticError) as exc:
        return ui.partial("_vorschau.html", error=str(exc))
    from .. import pdf as pdfmod
    return ui.partial("_vorschau.html", prev=prev, art=art, abzuege=pdfmod.invoice_deductions(book, prev),
                      anzahl=sum(1 for p in prev["positionen"] if not p.get("rabatt") and not p.get("akonto")))


async def rechnung_akonto(ui: UI, request: Request):
    """The open Akontorechnungen of the customer picked in the invoice form, ticked to be credited."""
    kunde = request.query_params.get("kunde") or ""
    offen = invoices.open_akonto(ui.book(), kunde) if kunde else []
    return ui.partial("_akonto_box.html", offen=offen)


async def rechnung_erstellen(ui: UI, request: Request):
    f = await request.form()
    art, rabatt, akonto = _art(f)
    return await act(request, api.invoice_create, lambda r: f"/debitoren/rechnung/{r['rechnung']['nummer']}",
                     ui.book(), f.get("kunde"), _positions(f), f.get("datum") or None, f.get("text", ""),
                     int(f.get("zahlungsfrist")) if f.get("zahlungsfrist") else None,
                     "" if (f.get("waehrung") or "CHF") == "CHF" else f.get("waehrung"), f.get("kurs") or None,
                     f.get("leistung_von") or None, f.get("leistung_bis") or None, rabatt, art, akonto,
                     f.get("objekt") or "")


async def rechnung_aktion(ui: UI, request: Request):
    f = await request.form()
    nr, aktion = request.path_params["nr"], request.path_params["aktion"]
    book = ui.book()
    to = f"/debitoren/rechnung/{nr}"
    if aktion == "zahlung":
        return await act(request, api.invoice_pay, to, book, nr, f.get("betrag") or None, f.get("datum") or None,
                         acct(f.get("konto")) or None, None, f.get("fw") or None)
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
    try:
        ar = invoices.aged_receivables(book, parse_date(stichtag, "stichtag") if stichtag else None)
    except FormatError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return ui.render(request, "offene_posten.html", book=book, ar=ar)


async def kreditoren_offene_posten(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    stichtag = request.query_params.get("stichtag")
    try:
        ap = kred.open_payables(book, parse_date(stichtag, "stichtag") if stichtag else None)
    except FormatError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return ui.render(request, "kreditoren_offene_posten.html", book=book, ap=ap)


def customer_states(book: Book) -> dict:
    """Per customer: invoice count, open amount, open Akonto and this year's revenue (customer lists)."""
    from .. import kunden as crm
    states = defaultdict(lambda: {"anzahl": 0, "offen": ZERO})
    paid = invoices.settlements(book)
    metas = invoices.invoices(book)
    for meta in metas.values():
        st = invoices.invoice_state(book, meta, paid.get(meta["nummer"], []))
        states[meta.get("kunde")]["anzahl"] += 1
        states[meta.get("kunde")]["offen"] += st["offen"]
    for a in invoices.open_akonto(book):
        states[a["kunde"]]["akonto"] = states[a["kunde"]].get("akonto", ZERO) + a["total"]
    year = date.today().year
    for nr, per in crm.revenue_by_year(book, metas).items():
        states[nr]["umsatz"] = per.get(year, ZERO)
    return states


async def kunden(ui: UI, request: Request):
    from .. import kunden as crm
    book = ui.book()
    custs = invoices.customers(book)
    states = customer_states(book)
    year = date.today().year
    kategorien = sorted({c.get("kategorie") for c in custs.values() if c.get("kategorie")})
    kat = request.query_params.get("kategorie", "")
    shown = {nr: c for nr, c in custs.items() if not kat or c.get("kategorie") == kat}
    edit = request.query_params.get("edit")
    if edit:                                  # old links to the inline form: the detail page has it now
        return RedirectResponse(f"/debitoren/kunden/{edit}#bearbeiten", status_code=303)
    return ui.render(request, "kunden.html", book=book, customers=shown, alle=len(custs), states=states, jahr=year,
                     suche={nr: customer_search_text(nr, c) for nr, c in shown.items()},
                     kategorien=kategorien, kategorie=kat, KATEGORIEN=crm.KATEGORIEN,
                     q=request.query_params.get("q", ""))


async def kunde(ui: UI, request: Request):
    from .. import kunden as crm
    book = ui.book()
    nr = request.path_params["nr"]
    try:
        d = await asyncio.to_thread(crm.dossier, book, nr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return ui.render(request, "kunde.html", book=book, d=d, c=d["kunde"], nr=nr, teile=kunde_teile_html(ui, book, nr, d),
                     arten=crm.LOG_ARTEN, KATEGORIEN=crm.KATEGORIEN, today=date.today().isoformat(),
                     filter=request.query_params.get("rechnungen", "alle"))


def kunde_teile_html(ui: UI, book: Book, nr: str, d: dict) -> list[dict]:
    """The plugin parts of a customer's dossier, rendered (a broken part shows a line instead of the page failing)."""
    teile = []
    for plugin, teil in d["teile"]:
        html = ""
        if teil.template:
            try:
                html = ui.env.get_template(f"plugins/{plugin}/{teil.template}").render(
                    **teil.context, book=book, kunde=nr)
            except Exception as exc:          # a broken plugin part must not take the customer page down
                html = f'<div class="panel"><div class="body small muted">{teil.titel}: nicht verfügbar ({type(exc).__name__})</div></div>'
        teile.append({"titel": teil.titel, "html": html, "kennzahlen": teil.kennzahlen, "aktionen": teil.aktionen})
    return teile


async def kunde_log(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    return await act(request, api.customer_log_add, f"/debitoren/kunden/{nr}", ui.book(), nr, f.get("text") or "",
                     f.get("art") or "notiz", f.get("datum") or None, f.get("nachfassen") or None)


async def kunde_log_erledigt(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    return await act(request, api.customer_log_done, request.headers.get("hx-current-url") or f"/debitoren/kunden/{nr}",
                     ui.book(), nr, int(request.path_params["index"]), f.get("wieder") != "1")


async def kunde_infos(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    return await act(request, api.customer_update, f"/debitoren/kunden/{nr}", ui.book(), nr,
                     notizen=f.get("notizen") or "")


async def kunde_speichern(ui: UI, request: Request, base: str = "/debitoren/kunden"):
    """New customer or changed master data; `base` is where the customer's page lives (desktop or phone)."""
    f = await request.form()
    fields = {k: (f.get(k) or "").strip() for k in ("name", "firma", "strasse", "nr", "plz", "ort", "land", "email",
                                                    "telefon", "mobil", "kategorie", "herkunft")}
    fields["rechnung_an"] = f.get("rechnung_an") or "firma"
    nr = request.path_params.get("nr")
    if not fields["name"]:
        return fail("Name ist nötig (Kontaktperson oder Privatperson).")
    satz = (f.get("stundensatz") or "").strip()
    if nr:
        if satz:
            fields["stundensatz"] = satz
        notes = {"notizen": f.get("notizen")} if "notizen" in f else {}
        return await act(request, api.customer_update, f"{base}/{nr}", ui.book(), nr, **notes, **fields)
    return await act(request, api.customer_add, lambda r: f"{base}/{r['kunde']['nummer']}", ui.book(),
                     notizen=f.get("notizen", ""), stundensatz=satz or None, **fields)


# ---------- Lohn ----------

def payroll_years(book: Book, selected: int | None = None) -> list[int]:
    years = set(book.years()) | {date.today().year}
    years.update(int(p["jahr"]) for p in payroll.payslips(book))
    if selected is not None:
        years.add(selected)
    return list(range(min(years), max(years) + 1))


def payroll_months(book: Book, year: int | None = None) -> list[str]:
    return [f"{y}-{m:02d}" for y in ([year] if year is not None else payroll_years(book)) for m in range(1, 13)]


async def lohn(ui: UI, request: Request):
    book = ui.book()
    from ..lohnausweis import employment_months
    all_slips = payroll.payslips(book)
    requested = request.query_params.get("monat")
    y = int(requested.split("-")[0]) if requested else int(request.query_params.get("jahr") or date.today().year)
    months = payroll_months(book, y)
    monat = requested or next((f"{y}-{int(p['monat']):02d}" for p in all_slips
                              if int(p["jahr"]) == y and p.get("status") != "abgeschlossen"),
                             f"{y}-{date.today().month:02d}")
    y, m = (int(x) for x in monat.split("-"))
    emps = payroll.employees(book)
    employed = {nr: employment_months(emp, y) for nr, emp in emps.items()}
    year_slips = [p for p in all_slips if int(p["jahr"]) == y]
    slips = {p["mitarbeiter"]: p for p in year_slips if int(p["monat"]) == m}
    month_status = []
    for mo in range(1, 13):
        entries = {p["mitarbeiter"]: p for p in year_slips if int(p["monat"]) == mo}
        expected = {nr for nr, months_of_employment in employed.items() if mo in months_of_employment}
        missing = len(expected - entries.keys())
        drafts = sum(p.get("status") != "abgeschlossen" for p in entries.values())
        closed = sum(p.get("status") == "abgeschlossen" for p in entries.values())
        status = "nicht gerechnet" if missing else "Entwurf" if drafts else "abgeschlossen" if closed else "keine Löhne"
        month_status.append({"monat": f"{y}-{mo:02d}", "m": mo, "status": status,
                             "fehlend": missing, "entwuerfe": drafts, "abgeschlossen": closed})
    rows = []
    for nr, emp in emps.items():
        slip = slips.get(nr)
        if not slip and m not in employed[nr]:
            continue
        rows.append({"emp": emp, "slip": slip, "warnings": payslip_warnings(slip, emp) if slip else []})
    totals = {k: sum((Decimal(str(r["slip"]["werte"][k])) for r in rows if r["slip"]), ZERO)
              for k in ("bruttolohn", "total_abzuege", "nettolohn")}
    from .. import spesen as sp, payroll_payments
    expense_rows = sp.summary(book)
    return ui.render(request, "lohn.html", spesen=expense_rows, accounts=account_options(book), book=book, monat=monat, months=months, rows=rows, totals=totals,
                     y=y, m=m, years=payroll_years(book, y), month_status=month_status,
                     zahlung=payroll_payments.active(book, y, m))


async def lohnzahlung(ui: UI, request: Request):
    f = await request.form()
    monat = f.get('monat', '')
    if request.path_params['aktion'] == 'zurueckziehen':
        return await act(request, api.payroll_payment_cancel, f'/lohn?monat={monat}', ui.book(), monat)
    return await act(request, api.payroll_payment_export, f'/lohn?monat={monat}', ui.book(), monat, f.get('datum'))


async def qst_importieren(ui: UI, request: Request):
    f = await request.form()
    try:
        year = int(f.get('jahr', ''))
    except ValueError:
        return fail('Ungültiges Tarifjahr')
    return await act(request, api.qst_sync, '/einstellungen#qst-tarife', ui.book(), f.get('kanton', ''), year)


async def lohnlauf_abschliessen(ui: UI, request: Request):
    f = await request.form()
    monat = f.get("monat") or ""
    return await act(request, api.payroll_close, f"/lohn?monat={monat}", ui.book(), monat)


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
                     ag_order=payroll.AG_ORDER, ag_label=payroll.AG_LABEL,
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


def qst_info(book: Book, emp: dict) -> dict:
    """Liability, canton and tariff of an employee as the pay run will see them (list and status box)."""
    pflichtig, grund = payroll.qst_pflichtig(emp)
    info = {"pflichtig": pflichtig, "grund": grund, "tarif": ((emp.get("qst") or {}).get("code") or ""),
            "kanton": "", "kanton_grund": "", "fehler": ""}
    if not pflichtig:
        return info
    if Decimal(str(emp.get("qst_satz") or 0)) and not info["tarif"]:
        info["tarif"] = f"vereinfacht {Decimal(str(emp['qst_satz'])) * 100:g} %"
        return info
    try:
        info["kanton"], info["kanton_grund"] = payroll.qst_kanton(book, emp)
    except BookError as exc:
        info["fehler"] = str(exc)
    if not info["tarif"]:
        info["fehler"] = info["fehler"] or "Tarif fehlt"
    return info


async def mitarbeiter(ui: UI, request: Request):
    book = ui.book()
    year = int(request.query_params.get("jahr") or date.today().year)
    from .. import qst_estv
    emps = payroll.employees(book)
    return ui.render(request, "mitarbeiter.html", book=book, emps=emps,
                     edit=request.query_params.get("edit"), neu=request.query_params.get("neu"),
                     qst_info={nr: qst_info(book, e) for nr, e in emps.items()},
                     aufenthalt=payroll.AUFENTHALT, tarife=payroll.QST_TARIFE, kantone=qst_estv.CANTONS,
                     year=year, years=payroll_years(book, year))


def _qst_from_form(f) -> tuple[dict | None, str]:
    """The tariff code from Tarif + Kinder + Kirchensteuer, and the canton override."""
    tarif = (f.get("qst_tarif") or "").strip().upper()
    kanton = (f.get("qst_kanton") or "").strip().upper()
    if not tarif:
        return None, kanton
    code = f"{tarif}{(f.get('qst_kinder') or '0').strip()}{'Y' if f.get('qst_kirche') == '1' else 'N'}"
    return {"code": code, **({"kanton": kanton} if kanton else {})}, kanton


async def qst_status(ui: UI, request: Request):
    """The live box in the Quellensteuer tab: liable or not and why, the canton and where it comes from."""
    from markupsafe import escape
    qp = request.query_params
    book = ui.book()
    try:
        q, _ = _qst_from_form(qp)
        rate = (qp.get("qst_satz_pct") or "").strip()
        emp = {"vorname": qp.get("vorname", ""), "nachname": qp.get("nachname", ""),
               "adresse": {"plz": qp.get("plz", ""), "ort": qp.get("ort", ""), "land": qp.get("land", "CH")},
               "aufenthalt": qp.get("aufenthalt", ""), "qst_pflicht": qp.get("qst_pflicht", "auto"),
               "qst": q or ({"kanton": qp.get("qst_kanton").upper()} if qp.get("qst_kanton") else None),
               # the core parser: rejects NaN/Infinity and junk like the save would
               "qst_satz": parse_amount(rate, "Satz vereinfachtes Verfahren") / 100 if rate else Decimal(0)}
        payroll.check_qst_fields(emp)
        info = qst_info(book, emp)
    except (BookError, FormatError, InvalidOperation, ValueError) as exc:
        return HTMLResponse(f'<div class="qstline warn">{escape(str(exc))}</div>')
    if not info["pflichtig"]:
        text = f"<strong>Nicht quellensteuerpflichtig</strong> · {escape(info['grund'])}"
        return HTMLResponse(f'<div class="qstline ok">{text}</div>')
    parts = [f"<strong>Quellensteuerpflichtig</strong> · {escape(info['grund'])}"]
    if info["kanton"]:
        parts.append(f"Kanton <strong>{escape(info['kanton'])}</strong> — {escape(info['kanton_grund'])}")
    if info["tarif"]:
        parts.append(f"Tarif {escape(info['tarif'])}")
    if info["fehler"]:
        parts.append(f'<span class="pill red">{escape(info["fehler"])}</span>')
    return HTMLResponse(f'<div class="qstline info">{" · ".join(parts)}</div>')


EMP_TEXT = ("vorname", "nachname", "strasse", "nr", "plz", "ort", "ahv_nr", "geburtsdatum", "geschlecht", "eintritt", "austritt", "lohnart", "iban", "land")
# the Quellensteuer fields are read in mitarbeiter_speichern (_qst_from_form)
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
    fields["qst_satz"] = str(parse_amount(f.get("qst_satz_pct")) / 100) if (f.get("qst_satz_pct") or "").strip() else "0"
    fields["ferien_inbegriffen"] = f.get("ferien_inbegriffen") == "1"
    fields["aufenthalt"] = (f.get("aufenthalt") or "").strip()
    fields["qst_pflicht"] = (f.get("qst_pflicht") or "auto").strip()
    fields["qst"], kanton = _qst_from_form(f)
    if fields["qst"]:
        fields["qst_satz"] = "0"
    elif kanton:
        return fail("Kanton abweichend nur zusammen mit einem Tarif erfassen")
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
    selected = int(request.query_params.get("jahr") or year)
    if selected != year:
        return RedirectResponse(f"/lohn/lohnkonto/{selected}/{nr}", status_code=303)
    emp = payroll.employee(book, nr)
    from .. import lohnausweis
    la = lohnausweis.annual_totals(book, year, nr)
    la_path = book.root / "lohnausweise" / str(year) / f"{nr}.pdf"
    return ui.render(request, "lohnkonto.html", book=book, lk=payroll.lohnkonto_matrix(book, year, nr), emp=emp,
                     year=year, la=la, years=payroll_years(book, year), emps=payroll.employees(book),
                     la_pdf=book.rel(la_path) if la_path.exists() else None)


async def lohnkonto_firma(ui: UI, request: Request):
    """Lohnkonto of the whole company; choosing a person leads to that person's Lohnkonto."""
    book = ui.book()
    q = request.query_params
    year = int(q.get("jahr") or date.today().year)
    bereich = q.get("bereich") or "firma"
    if bereich != "firma":
        return RedirectResponse(f"/lohn/lohnkonto/{year}/{bereich}", status_code=303)
    return ui.render(request, "lohnkonto.html", book=book, lk=payroll.lohnkonto_matrix(book, year), emp=None,
                     year=year, years=payroll_years(book, year), emps=payroll.employees(book))


async def lohnmeldungen(ui: UI, request: Request):
    from .. import lohnmeldung
    book = ui.book()
    year = int(request.query_params.get("jahr") or date.today().year - (date.today().month < 4))
    m = lohnmeldung.jahresmeldung(book, year)
    return ui.render(request, "lohnmeldungen.html", book=book, m=m, year=year, years=payroll_years(book, year),
                     tables=lohnmeldung.meldung_tables(m))


async def lohn_datei(ui: UI, request: Request):
    """PDF/Excel/CSV of the Lohnkonto (art=lohnkonto, bereich=firma|M0001) or the Lohnmeldungen (art=meldungen)."""
    from urllib.parse import quote
    from .. import lohnmeldung
    book = ui.book()
    q = request.query_params
    fmt, year = request.path_params["fmt"], int(q.get("jahr") or date.today().year)
    if request.path_params["art"] not in ("lohnkonto", "meldungen") or fmt not in ("pdf", "xlsx", "csv"):
        return PlainTextResponse("Nicht gefunden", status_code=404)
    try:
        if request.path_params["art"] == "meldungen":
            name, data = await asyncio.to_thread(lohnmeldung.meldung_document, book, year, fmt)
        else:
            nr = q.get("bereich") or "firma"
            name, data = await asyncio.to_thread(lohnmeldung.lohnkonto_document, book, year,
                                                 None if nr == "firma" else nr, fmt)
    except (BookError, ValueError) as exc:
        return PlainTextResponse(str(exc), status_code=400)
    media = {"pdf": "application/pdf", "csv": "text/csv; charset=utf-8",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    disposition = "inline" if fmt == "pdf" else "attachment"
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}"})


async def lohnausweis_erstellen(ui: UI, request: Request):
    year, nr = int(request.path_params["jahr"]), request.path_params["nr"]
    return await act(request, api.lohnausweis_create, f"/lohn/lohnkonto/{year}/{nr}", ui.book(), year, nr)


# ---------- Abschluss ----------

ABSCHLUSS_TABS = ("uebersicht", "buchungen", "jahresrechnung", "unterlagen", "abschliessen")


async def abschluss(ui: UI, request: Request):
    """Abschluss in tabs: Übersicht (checklist), Abschlussbuchungen, Jahresrechnung, Unterlagen, Abschliessen.
    Each tab computes only what it shows."""
    book = ui.book()
    year = year_param(request, book)
    tab = request.path_params.get("tab", "uebersicht")
    if tab not in ABSCHLUSS_TABS:
        return HTMLResponse("Unbekannte Seite", status_code=404)
    st = statements.year_end_statement(book, year)
    ctx = {"tab": tab, "checkliste": _closing_list(book, year)}
    cl = ctx["checkliste"]
    ctx["buchungen_offen"] = sum(1 for ph in (cl or {}).get("phasen", []) if ph["key"] in ("bewerten", "buchen")
                                 for s in ph["schritte"] if not s["erledigt"])
    if tab == "uebersicht":
        ctx["jahreswechsel"] = jahreswechsel.checklist(book, year)
    elif tab == "buchungen":
        ctx["fx"] = _fx_view(book, request, year, st)
        ctx.update(_abgrenzungen(book, year), mwst_jahr=_mwst_year(book, year))
    elif tab == "jahresrechnung":
        from ..files import read_yaml
        ctx.update(gv_date=date(year + 1, 6, 30).isoformat(),
                   div=(read_yaml(statements.dividend_path(book, year)) if statements.dividend_path(book, year).exists()
                        else None))
    elif tab == "unterlagen":
        from .. import dossier
        ctx.update(unterlagen=dossier.overview(book, year), luecken=dossier.belegluecken(book, year))
    else:
        from ..files import read_yaml
        lock_path = checks.locks_path(book)
        verlauf = (read_yaml(lock_path).get("verlauf") or []) if lock_path.exists() else []
        ctx.update(verlauf=list(reversed(verlauf))[:5], anhang=statements.anhang(book, year),
                   anhang_an=statements.anhang_enabled(book),
                   einzelfirma=book.settings.rechtsform_art == "einzelfirma",
                   naechstes_jahr=jahreswechsel.next_year(book),
                   kann_eroeffnen=jahreswechsel.next_year(book) <= date.today().year + 1)
    return ui.render(request, "abschluss.html", book=book, year=year, st=st, years=book.years(), **ctx)


def _fx_view(book: Book, request: Request, year: int, st: dict) -> dict | None:
    from .. import kreditoren as kred
    has_fw_bills = (any(kred.is_foreign(m) for m in kred.bills(book).values())
                    or any(invoices.is_foreign(m) for m in invoices.invoices(book).values()))
    if not (st.get("fremdwaehrung") or has_fw_bills):
        return None
    from .. import fx
    try:
        stichtag = date.fromisoformat(request.query_params.get("stichtag", ""))
    except ValueError:
        stichtag = min(date(year, 12, 31), date.today())
    done = sorted(p.stem for p in (book.root / "bewertung").glob(f"{year}-*.yaml"))
    try:
        return {"stichtag": stichtag, "konten": fx.preview(book, stichtag), "bewertet": fx.path(book, stichtag).exists(),
                "erledigt": done, "kreditoren": fx.preview_bills(book, stichtag),
                "debitoren": fx.preview_invoices(book, stichtag)}
    except BookError as exc:          # no rates yet (future date, offline)
        return {"stichtag": stichtag, "konten": [], "bewertet": fx.path(book, stichtag).exists(), "fehler": str(exc),
                "erledigt": done}


def _closing_list(book: Book, year: int) -> dict | None:
    from .. import jahresabschluss
    try:
        return jahresabschluss.checklist(book, year)
    except BookError:
        return None


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return getattr(user, "anzeige", "") or getattr(user, "name", "") or ""


async def abschluss_buchung(ui: UI, request: Request):
    """One closing entry: a stock to count or estimate per 31.12., or the private shares."""
    from .. import jahresabschluss as ja
    book = ui.book()
    year = year_param(request, book)
    art = request.path_params["art"]
    try:
        v = ja.view(book, year, art)
    except BookError as exc:
        return HTMLResponse(str(exc), status_code=404)
    qp = request.query_params
    rows = [dict(p) for p in ((v["gebucht"] or {}).get("positionen") or [])]
    if qp.get("vorschlaege") == "1":
        rows = [dict(p) for p in v["vorschlaege"] if p.get("betrag") not in (None, "")] or rows
    fz = None
    if art == ja.PRIVAT and qp.get("fz_preis"):
        try:
            months = int(qp.get("fz_monate") or 12)
            amount = ja.fahrzeug_anteil(parse_amount(qp.get("fz_preis")), months)
            fz = {"art": "fahrzeug", "konto": ja.PRIVAT_ARTEN["fahrzeug"][1], "betrag": f"{amount:.2f}",
                  "text": f"Fahrzeug {qp.get('fz_name') or ''}".strip(),
                  "grundlage": f"0.9 % × {parse_amount(qp.get('fz_preis')):,.2f} × {months} Monate".replace(",", "'")}
            rows.append(fz)
        except (BookError, ValueError) as exc:
            fz = {"fehler": str(exc)}
    rows += [{} for _ in range(3)]
    return ui.render(request, "abschluss_buchung.html", book=book, year=year, art=art, v=v, rows=rows, fz=fz,
                     privat=art == ja.PRIVAT, mwst_codes=_mwst_codes_for(book),
                     accounts=account_options(book))


def _mwst_codes_for(book: Book) -> list[str]:
    from .. import mwst
    if mwst.config(book)["methode"] != "effektiv":
        return []
    return [c for c in mwst.CODES if c.startswith("U") and c not in ("U0", "UA")]


async def abschluss_buchung_post(ui: UI, request: Request):
    from .. import jahresabschluss as ja
    f = await request.form()
    art = request.path_params["art"]
    year = int(f.get("jahr") or current_year(ui.book()))
    to = f"/abschluss/buchungen?jahr={year}"
    if request.url.path.endswith("/entfernen"):
        return await act(request, api.closing_entry_remove, to, ui.book(), year, art)
    texts, amounts = f.getlist("text"), f.getlist("betrag")
    arten, konten, codes = f.getlist("art"), f.getlist("konto"), f.getlist("mwst")
    pos = []
    for i, text in enumerate(texts):
        item = {"text": text, "betrag": amounts[i] if i < len(amounts) else ""}
        if art == ja.PRIVAT:
            item.update(art=arten[i] if i < len(arten) else "", konto=acct(konten[i] if i < len(konten) else ""),
                        mwst=codes[i] if i < len(codes) else "")
        pos.append(item)
    return await act(request, api.closing_entry_book, to, ui.book(), year, art, pos, None, f.get("notiz", ""),
                     _who(request))


def _mwst_year(book: Book, year: int) -> dict | None:
    """The VAT part of the year-end: Abrechnungen of the year and the Umsatzabstimmung. The running
    quarters stay on /mwst; none of it for a book without VAT."""
    from .. import mwst
    if mwst.config(book)["methode"] == "keine":
        return None
    try:
        return mwst.abstimmung(book, year)
    except BookError as exc:
        return {"fehler": str(exc), "perioden": [], "hinweise": [], "ok": False}


def _statement_view(book: Book, st: dict) -> dict:
    """What the preview prints besides the figures, as in pdf.statement_pdf: prior-year column, signing body."""
    s = book.settings
    rows = st["aktiven"] + st["passiven"] + st["erfolg"]
    art = s.rechtsform_art
    return {"prior": st["jahr"] > s.erstes_jahr or any(r["vorjahr"] for r in rows),
            "organ": {"einzelfirma": "Inhaber/in", "verein": "Vorstand"}.get(
                art, "Verwaltungsrat" if str(s.get("rechtsform") or "").strip().upper() == "AG" else "Geschäftsführung"),
            "ort": s.adresse.get("ort") or "", "rechtsform": s.get("rechtsform") or "",
            "uid": (s.get("uid") or "").replace(" MWST", "")}


async def abschluss_vorschau(ui: UI, request: Request):
    """The Jahresrechnung as it will be printed (Bilanz, Erfolgsrechnung, Verwendung des Ergebnisses, Anhang),
    and with ?gliederung=1 every group of both sides as a place to move accounts to."""
    book = ui.book()
    year = year_param(request, book)
    st = statements.year_end_statement(book, year)
    gliederung = request.query_params.get("gliederung") == "1"
    alle = request.query_params.get("alle") == "1"
    layout = {seite: statements.layout(book, year, seite, alle=alle) for seite in ("bilanz", "erfolg")} if gliederung else None
    prior_locked = bool(book.settings.sperre_bis and book.settings.sperre_bis >= date(year - 1, 12, 31)) \
        and year - 1 in book.years()
    return ui.render(request, "abschluss_vorschau.html", book=book, year=year, st=st, years=book.years(),
                     prior_locked=prior_locked,
                     view=_statement_view(book, st), gliederung=gliederung, alle=alle, layout=layout,
                     groups=statements.GROUPS, anhang=statements.anhang_blocks(book, year))


async def abschluss_gruppe(ui: UI, request: Request):
    f = await request.form()
    year = int(f.get("jahr") or current_year(ui.book()))
    back = request.headers.get("hx-current-url") or f"/abschluss/vorschau?jahr={year}&gliederung=1"
    if "negativ" in f:
        return await act(request, api.account_negative_group_set, back, ui.book(), f.get("konto", ""),
                         f.get("negativ", ""), year)
    return await act(request, api.account_group_set, back, ui.book(), f.get("konto", ""), f.get("gruppe", ""), year)


def _abgrenzungen(book: Book, year: int) -> dict:
    from .. import abgrenzung
    try:
        return {"abgr": abgrenzung.proposals(book, year), "abgr_fehler": ""}
    except BookError as exc:
        return {"abgr": [], "abgr_fehler": str(exc)}


async def abschluss_aktion(ui: UI, request: Request):
    f = await request.form()
    aktion = request.path_params["aktion"]
    book = ui.book()
    year = int(f.get("jahr") or current_year(book))
    to = f"/abschluss?jahr={year}"
    tab = {"gewinnverwendung": "jahresrechnung", "gewinnverwendung-buchen": "jahresrechnung",
           "dividende": "jahresrechnung", "bewertung": "buchungen", "abgrenzungen": "buchungen", "anhang": "abschliessen",
           "anhang-an": "abschliessen", "anhang-aus": "abschliessen", "sperre": "abschliessen",
           "entsperren": "abschliessen"}.get(aktion)
    if tab:
        to = f"/abschluss/{tab}?jahr={year}"
    if aktion == "gewinnverwendung":
        return await act(request, api.allocation_set, to, book, year, f.get("dividende") or "0", f.get("reserve") or "0")
    if aktion == "gewinnverwendung-buchen":
        return await act(request, api.allocation_book, to, book, year, f.get("datum") or None)
    if aktion == "bewertung":
        return await act(request, api.fx_revalue, to, book, f.get("stichtag") or f"{year}-12-31")
    if aktion == "dividende":
        return await act(request, api.dividend_pay, to, book, year, f.get("datum") or None, f.get("konto") or "")
    if aktion == "anhang":
        return await act(request, api.anhang_save, to, book, year, f.get("text", ""))
    if aktion in ("anhang-an", "anhang-aus"):
        return await act(request, api.anhang_enable, to + "#anhang", book, aktion == "anhang-an")
    if aktion == "abgrenzungen":
        return await act(request, api.accruals_book, to + "#abgrenzungen", book, year, f.getlist("id"))
    if aktion == "jahr-eroeffnen":
        neu = jahreswechsel.next_year(book)
        return await act(request, api.year_open, f"/abschluss?jahr={neu}", book, neu)
    if aktion == "schritt":
        return await act(request, api.closing_mark, to, book, year, f.get("schritt", ""),
                         f.get("erledigt") == "1", f.get("notiz", ""), _who(request))
    if aktion == "sperre":
        return await act(request, api.lock, to, book, f.get("bis"))
    if aktion == "entsperren":
        return await act(request, api.unlock, to, book, f.get("bis") or None, f.get("grund", ""))
    return fail("Unbekannte Aktion")


# ---------- Bank ----------

def _bank_common(book) -> dict:
    """What every bank page needs for the import form at the top."""
    from .. import plugins
    formats = plugins.bank_formats(book)
    return {"bank_accept": ",".join(sorted({x for f in formats for x in f.suffixes}
                                           | {".camt", ".053", ".csv", ".txt", ".xlsx", ".pdf"})),
            "bank_labels": ", ".join(f.label for f in formats), "accounts": account_options(book)}


def _rec_latest(rec: list[dict]) -> list[dict]:
    """The latest reconciliation per account (the one line the reconcile page shows)."""
    latest: dict[str, dict] = {}
    for r in rec:
        if r["konto"] not in latest or r["datum"] >= latest[r["konto"]]["datum"]:
            latest[r["konto"]] = r
    return list(latest.values())


async def bank_page(ui: UI, request: Request):
    """Bank › Abgleichen: one card per open movement, the best match next to it, one click to take it."""
    from .. import bank, jev
    book = ui.book()
    rows = sorted((t for t in bank.transactions(book) if t["Status"] == "offen"), key=lambda t: (t["Datum"], t["ID"]))
    sugg = bank.suggestions(book) if rows else {}
    rec = bank.reconciliation(book)
    return ui.render(request, "bank_abgleich.html", book=book, rows=rows, sugg=sugg, jev=jev.config(book),
                     sicher=sum(1 for o in sugg.values() if o[0]["sicher"]), pending=_bank_pending(book),
                     rec=_rec_latest(rec), **_bank_common(book))


async def bank_bewegungen(ui: UI, request: Request):
    from .. import bank
    book = ui.book()
    status = request.query_params.get("status", "")
    rows = [t for t in bank.transactions(book) if not status or t["Status"] == status]
    rows.sort(key=lambda t: (t["Datum"], t["ID"]), reverse=True)
    counts = defaultdict(int)
    for t in bank.transactions(book):
        counts[t["Status"]] += 1
    return ui.render(request, "bank.html", book=book, rows=rows, status=status, counts=counts,
                     pending=_bank_pending(book), **_bank_common(book))


async def bank_regeln(ui: UI, request: Request):
    from .. import bank
    book = ui.book()
    return ui.render(request, "bank_regeln.html", book=book, rules=bank.rules(book), **_bank_common(book))


async def bank_abstimmung(ui: UI, request: Request):
    from .. import bank
    book = ui.book()
    return ui.render(request, "bank_abstimmung.html", book=book, rec=bank.reconciliation(book), **_bank_common(book))


async def bank_suchen(ui: UI, request: Request):
    """«Suchen & zuordnen» on a reconcile card: candidates for one open movement (HTMX partial)."""
    book = ui.book()
    tid = request.path_params["id"]
    try:
        hits = await asyncio.to_thread(api.bank_candidates, book, tid, request.query_params.get("q", ""))
    except BookError as exc:
        return HTMLResponse(f'<div class="alert error">{exc}</div>')
    return ui.partial("_bank_treffer.html", hits=hits, tid=tid, q=request.query_params.get("q", ""))


async def bank_upload(ui: UI, request: Request):
    f = await request.form()
    upload = f.get("datei")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    from .. import bankformat
    data = await upload.read()
    name = Path(upload.filename).name
    tmpdir = Path(tempfile.mkdtemp(prefix="aeradex-"))
    tmp = tmpdir / name
    tmp.write_bytes(data)
    try:
        try:
            result = await asyncio.to_thread(api.bank_import, ui.book(), str(tmp))
        except (BookError, FormatError) as exc:
            suffix = Path(name).suffix.lower()
            # an unknown format, or a known one the bank changed: the agent learns it (a person confirms it)
            unknown = "Unbekanntes Kontoauszugsformat" in str(exc) or isinstance(exc, bankformat.FormatChanged)
            if not unknown or suffix not in bankformat.TEXT + bankformat.EXCEL + (".pdf",):
                return fail(str(exc))
            return await _bank_learn(ui, request, name, data, acct(f.get("konto")), suffix)
        return done(result.get("meldung", "Importiert"), "/bank")
    finally:
        if tmp.exists():
            tmp.unlink()


async def _bank_learn(ui: UI, request: Request, name: str, data: bytes, konto: str, suffix: str):
    """An unknown statement: keep it in the inbox and let the agent read it — the format of a CSV/Excel
    file (a person confirms it below), or the transactions of a credit card statement."""
    if suffix == ".pdf" and not konto:
        return fail("Für eine Kreditkartenabrechnung das Kreditkartenkonto angeben (Passivkonto, z.B. 2040).")
    try:
        datei = (await asyncio.to_thread(api.inbox_add, ui.book(), name, data))["datei"]
        if suffix == ".pdf":
            res = await asyncio.to_thread(api.card_statement_read, ui.book(), datei, konto)
            if res.get("import"):
                return done(res["import"]["meldung"], "/bank")
            return done("Kreditkartenabrechnung gelesen, aber nicht geprüft — siehe unten", "/bank#neu")
        await asyncio.to_thread(api.bank_format_learn, ui.book(), datei, konto)
    except (BookError, FormatError, RuntimeError) as exc:
        return fail(str(exc))
    return done("Format erkannt — bitte unten prüfen und bestätigen", "/bank#neu")


def _bank_pending(book) -> dict:
    """Files in the inbox the agent has read but nobody confirmed yet: CSV/Excel with an unconfirmed
    format, card statements whose checks failed."""
    from .. import bankformat
    formats, cards = [], []
    for path in sorted((book.root / "inbox").glob("*")):
        suffix = path.suffix.lower()
        try:
            if suffix in bankformat.TEXT + bankformat.EXCEL:
                found = bankformat.match(book, path.name, path.read_bytes(), nur_bestaetigt=False)
                if found and not found[1].get("bestaetigt"):
                    formats.append({"datei": book.rel(path), "format": found[0], "name": found[1].get("name") or found[0],
                                    **api.bank_format_check(book, book.rel(path))})
            elif suffix == ".pdf":
                card = bankformat.load_card(book, path.read_bytes())
                if card:
                    cards.append({"datei": book.rel(path), "karte": card,
                                  "yaml": book.rel(bankformat.card_path(book, path.read_bytes())),
                                  "pruefung": bankformat.card_checks(card, bankformat.pdf_text(path))})
        except (BookError, FormatError) as exc:
            formats.append({"datei": book.rel(path), "fehler": str(exc)})
    return {"formate": formats, "karten": cards}


async def bank_format_ok(ui: UI, request: Request):
    f = await request.form()
    book, name, datei = ui.book(), request.path_params["name"], f.get("datei") or ""

    def confirm_and_import():
        api.bank_format_confirm(book, name)
        return api.bank_import(Book(book.root), datei)
    return await act(request, confirm_and_import, "/bank")


async def bank_karte_import(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.bank_import, "/bank", ui.book(), f.get("datei") or "")


async def bank_jev(ui: UI, request: Request):
    return await act(request, api.bank_suggest, "/bank", ui.book())


async def jev_einstellung(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.settings_update, "/einstellungen#jev", ui.book(),
                     jev={"aktiv": f.get("aktiv") == "1", "schwelle": f.get("schwelle") or None})


def _katalog(book):
    from .. import marktplatz
    try:
        return {"eintraege": marktplatz.entries(book), "quelle": marktplatz.source(), "fehler": ""}
    except Exception as exc:  # an unreachable catalog must not break the settings page
        return {"eintraege": [], "quelle": marktplatz.source(), "fehler": str(exc)}


async def plugin_installieren(ui: UI, request: Request):
    """Local mode only: pip install a catalog plugin into this environment; a restart loads it."""
    from .. import marktplatz
    if ui.auth_dir is not None:
        return fail("Auf dem Server werden Plugins nicht aus dem Browser installiert — den angezeigten Befehl "
                    "auf dem Server ausführen und aeradex neu starten.")
    f = await request.form()
    name = f.get("name", "")
    try:
        await asyncio.to_thread(marktplatz.install, name, f.get("ungeprueft_ok") == "1")
    except BookError as exc:
        return fail(str(exc))
    ui.neustart_noetig = True
    return done(f"{name} installiert — aeradex neu starten, um es zu laden", "/einstellungen#funktionen")


async def plugin_neustart(ui: UI, request: Request):
    from .app import restart_later
    if ui.auth_dir is not None:
        return fail("Den Server bitte über systemd neu starten (systemctl restart …).")
    restart_later(ui)
    return done("aeradex startet neu …", "/einstellungen#funktionen")


async def plugin_einstellung(ui: UI, request: Request):
    f = await request.form()
    fn = api.plugin_enable if request.path_params["aktion"] == "ein" else api.plugin_disable
    return await act(request, fn, "/einstellungen#funktionen", ui.book(), f.get("name", ""))


def _book_and_rule(book, tid: str, konto: str, text: str, mwst: str, rule: bool) -> dict:
    """«Neu buchen» on a reconcile card; with «Immer so buchen» also a rule for the next ones."""
    res = api.bank_book(book, tid, konto, text, mwst)
    if rule:
        res = api.bank_rule_from(Book(book.root), tid)
    return res


async def bank_aktion(ui: UI, request: Request):
    f = await request.form()
    tid, aktion = request.path_params["id"], request.path_params["aktion"]
    to = request.headers.get("hx-current-url", "/bank")
    book = ui.book()
    if aktion == "buchen":
        call = (_book_and_rule, book, tid, acct(f.get("konto")), f.get("text", ""), f.get("mwst", ""), f.get("regel") == "1")
    elif aktion == "zuordnen":
        call = (api.bank_assign, book, tid, f.get("nummer", ""))
    elif aktion == "abgleichen":
        call = (api.bank_link, book, tid, (f.get("beleg") or "").strip())
    elif aktion == "ignorieren":
        call = (api.bank_ignore, book, tid, f.get("grund", ""))
    elif aktion == "regel":
        call = (api.bank_rule_from, book, tid)
    elif aktion == "loesen":
        call = (api.bank_unlink, book, tid)
    elif aktion == "uebernehmen":
        call = (api.bank_accept, book, tid, f.get("art", ""), f.get("ziel", ""))
    else:
        return fail("Unbekannte Aktion")
    if not f.get("karte"):
        return await act(request, call[0], to, *call[1:])
    try:
        res = await asyncio.to_thread(*call)
    except (BookError, FormatError, ValueError, KeyError, RuntimeError) as exc:
        return fail(str(exc))
    from markupsafe import escape
    msg = res.get("meldung", "Erledigt") if isinstance(res, dict) else "Erledigt"
    return HTMLResponse(f'<div class="bankcard done" id="karte-{escape(tid)}" role="status">✓ {escape(msg)}</div>',
                        headers={"HX-Retarget": f"#karte-{tid}", "HX-Reswap": "outerHTML"})


async def bank_alle(ui: UI, request: Request):
    return await act(request, api.bank_accept_all, request.headers.get("hx-current-url", "/bank"), ui.book())


async def bank_regel(ui: UI, request: Request):
    f = await request.form()
    to = request.headers.get("hx-current-url", "/bank")
    if request.path_params.get("id"):
        return await act(request, api.bank_rule_remove, to, ui.book(), request.path_params["id"])
    return await act(request, api.bank_rule_add, to, ui.book(), acct(f.get("konto")), f.get("gegenpartei", ""),
                     f.get("text", ""), f.get("betrag") or None, f.get("richtung", ""), f.get("mwst", ""),
                     f.get("buchungstext", ""))


async def spesen_aktion(ui: UI, request: Request):
    f = await request.form()
    back = request.headers.get("hx-current-url") or "/lohn#spesen"
    if request.path_params.get("nr"):
        return await act(request, api.expense_remove, back, ui.book(), request.path_params["nr"])
    upload = f.get("datei")
    datei = None
    if upload is not None and getattr(upload, "filename", ""):
        res = await asyncio.to_thread(api.inbox_add, ui.book(), upload.filename, await upload.read())
        datei = res["datei"]
    return await act(request, api.expense_add, back, ui.book(), f.get("mitarbeiter", ""), f.get("datum"),
                     f.get("text", ""), f.get("betrag"), acct(f.get("konto")), f.get("mwst") or "", None,
                     f.get("art") or "uebrige", datei or "")


# ---------- pages of plugins ----------

def _plugin_page(book: Book, plugin: str, slug: str):
    from .. import plugins
    for name, page in plugins.pages(book):
        if name == plugin and page.slug == slug:
            return page
    return None


async def plugin_seite(ui: UI, request: Request):
    book = ui.book()
    page = _plugin_page(book, request.path_params["plugin"], request.path_params["slug"])
    if page is None:
        return PlainTextResponse("Seite nicht gefunden (Plugin nicht eingeschaltet?)", status_code=404)
    try:
        ctx = await asyncio.to_thread(page.context, book, dict(request.query_params))
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return ui.render(request, f"plugins/{request.path_params['plugin']}/{page.template}", book=book,
                     page=page, plugin=request.path_params["plugin"], **(ctx or {}))


async def _render_fragment(ui: UI, request: Request, book: Book, page, name: str, query: dict,
                           meldung: str = "") -> Response:
    template, context = page.fragments[name]
    try:
        ctx = await asyncio.to_thread(context, book, query)
    except BookError as exc:
        return fail(str(exc))
    response = ui.render(request, f"plugins/{request.path_params['plugin']}/{template}", book=book, page=page,
                         plugin=request.path_params["plugin"], **(ctx or {}))
    if meldung:
        response.headers["HX-Trigger"] = json.dumps({"aeradexToast": meldung})
    return response


async def plugin_teil(ui: UI, request: Request):
    """A fragment of a plugin page (Page.fragments), fetched on its own."""
    book = ui.book()
    page = _plugin_page(book, request.path_params["plugin"], request.path_params["slug"])
    name = request.path_params["teil"]
    if page is None or name not in page.fragments:
        return PlainTextResponse("Nicht gefunden", status_code=404)
    return await _render_fragment(ui, request, book, page, name, dict(request.query_params))


async def plugin_aktion(ui: UI, request: Request):
    book = ui.book()
    page = _plugin_page(book, request.path_params["plugin"], request.path_params["slug"])
    fn = page.actions.get(request.path_params["aktion"]) if page else None
    if fn is None:
        return fail("Unbekannte Aktion")
    form = await request.form()
    data = {k: (form.getlist(k) if len(form.getlist(k)) > 1 else form.get(k)) for k in form.keys()}
    back = request.headers.get("hx-current-url") or f"/p/{request.path_params['plugin']}/{page.slug}"
    try:
        result = await asyncio.to_thread(fn, book, data)
    except (BookError, FormatError, ValueError, KeyError, RuntimeError) as exc:
        return fail(str(exc))
    if isinstance(result, dict) and result.get("fragment") in page.fragments:
        return await _render_fragment(ui, request, ui.book(), page, result["fragment"], dict(result.get("query") or {}),
                                      result.get("meldung", ""))
    message = result.get("meldung", "Gespeichert") if isinstance(result, dict) else "Gespeichert"
    return done(message, result.get("weiter") if isinstance(result, dict) and result.get("weiter") else back)


# ---------- Kreditoren ----------

# Status tabs of Einkauf and Verkauf: tab → the document statuses it lists (None = every status).
EINKAUF_TABS = {"offen": ("offen", "angewiesen"), "bezahlt": ("bezahlt",), "alle": None,
                "angewiesen": ("angewiesen",), "storniert": ("storniert",)}
VERKAUF_TABS = {"offen": ("offen", "teilbezahlt"), "bezahlt": ("bezahlt",), "alle": None,
                "teilbezahlt": ("teilbezahlt",), "storniert": ("storniert",)}
DRAFT_GROUPS = {"einkauf": ("kreditor", "quittung"), "verkauf": ("debitor",)}
DRAFT_REVIEW = {"kreditor": "/kreditoren/neu?entwurf=", "quittung": "/eingang/quittung?entwurf=",
                "debitor": "/debitoren/extern?entwurf="}
DRAFT_TAB = {"einkauf": "/kreditoren?status=entwurf", "verkauf": "/debitoren?status=entwurf"}


def group_drafts(book: Book, group: str) -> list[dict]:
    from .. import erfassung
    return sorted((d for d in erfassung.drafts(book).values() if d.get("art", "kreditor") in DRAFT_GROUPS[group]),
                  key=lambda d: d["id"])


def _group_of(art: str) -> str:
    return "verkauf" if art == "debitor" else "einkauf"


def draft_nav(book: Book, meta: dict) -> dict:
    """«Entwurf 2 von 5» with the previous/next draft of the same list, for the review pages."""
    items = group_drafts(book, _group_of(meta.get("art", "kreditor")))
    ids = [d["id"] for d in items]
    i = ids.index(meta["id"]) if meta["id"] in ids else 0
    url = lambda d: DRAFT_REVIEW[d.get("art", "kreditor")] + d["id"]
    return {"pos": i + 1, "total": len(items), "tab": DRAFT_TAB[_group_of(meta.get("art", "kreditor"))],
            "prev": url(items[i - 1]) if i > 0 else "", "next": url(items[i + 1]) if i + 1 < len(items) else ""}


def _beleg_of(result: dict) -> str:
    """The Beleg number a booked bill or receipt got (api results of bill_add / receipt_book)."""
    if isinstance(result, dict):
        if result.get("kreditor"):
            return str(result["kreditor"].get("nummer") or "")
        rows = result.get("buchungen") or []
        if rows:
            return str(rows[0].get("beleg") or "")
    return ""


def then_plugin(book: Book, to):
    """Wrap an `act` target: a plugin may continue elsewhere after the Beleg (e.g. Anlagen for a purchase)."""
    from .. import plugins

    def target(result):
        default = to(result) if callable(to) else to
        beleg = _beleg_of(result)
        return (plugins.after_beleg(book, beleg) if beleg else None) or default
    return target


def after_draft(book: Book, art: str, draft_id: str, weiter: bool) -> str:
    """Where to go once a draft is approved: with «Freigeben & nächster» the next draft of the list that
    is not with the agent, else back to the list."""
    group = _group_of(art)
    if weiter:
        for d in group_drafts(book, group):
            if d["id"] != draft_id and d.get("status") != "agent":
                return DRAFT_REVIEW[d.get("art", "kreditor")] + d["id"]
    return DRAFT_TAB[group]


async def kreditoren_page(ui: UI, request: Request):
    from .. import kreditoren as kred
    book = ui.book()
    drafts = group_drafts(book, "einkauf")
    status = request.query_params.get("status") or ("entwurf" if drafts else "offen")
    if status != "entwurf" and status not in EINKAUF_TABS:
        status = "alle"
    paid = kred.payments(book)
    rows = [kred.state(book, m, paid.get(k, [])) for k, m in kred.bills(book).items()]
    wanted = EINKAUF_TABS.get(status)
    rows = [r for r in rows if wanted is None or r["status"] in wanted] if status != "entwurf" else []
    rows.sort(key=lambda r: (r["status"] not in ("offen", "angewiesen"), r["faellig"], r["nummer"]),
              reverse=status == "bezahlt")
    from .. import erfassung, jev
    return ui.render(request, "kreditoren.html", book=book, rows=rows, status=status, op=kred.open_payables(book),
                     default_date=_next_workday().isoformat(), drafts=drafts,
                     auto_agent=erfassung.agent_auto(book), jev=jev.config(book),
                     ocr=bool(erfassung.ocr_languages()), dv=erfassung.value, vat_of=draft_vat,
                     hold_of=lambda d: api.bill_draft_hold(book, d))


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


EINGANG_BACK = {"kreditor": "/kreditoren?status=entwurf", "quittung": "/kreditoren?status=entwurf",
                "debitor": "/debitoren?status=entwurf", "": "/kreditoren?status=entwurf"}
STATEMENT_SUFFIXES = (".xml", ".camt", ".053")


async def eingang_einlesen(ui: UI, request: Request):
    """Upload (or inbox files from the Übersicht) → drafts. PDFs and images are read; camt statements
    are imported at the bank; anything else (CSV, Excel) lands in the inbox. Without a given kind the
    answer leads to where the drafts landed: Einkauf or Verkauf › Entwürfe."""
    from .. import erfassung
    form = await request.form()
    art = (form.get("art") or ("kreditor" if request.url.path.startswith("/kreditoren") else "")).strip()
    uploads = [u for u in form.getlist("dateien") if getattr(u, "filename", "")]
    targets = [d for d in form.getlist("datei") if d]
    if not uploads and not targets:
        return fail("Keine Datei gewählt.")
    created, errors_, stored, statements = [], [], 0, []
    for upload in uploads:
        if not art and Path(upload.filename).suffix.lower() in STATEMENT_SUFFIXES:
            tmpdir = Path(tempfile.mkdtemp(prefix="aeradex-"))
            tmp = tmpdir / Path(upload.filename).name
            tmp.write_bytes(await upload.read())
            try:
                statements.append((await asyncio.to_thread(api.bank_import, ui.book(), str(tmp)))["meldung"])
            except (BookError, FormatError) as exc:
                errors_.append(f"{upload.filename}: {exc}")
            finally:
                tmp.unlink(missing_ok=True)
            continue
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
    if errors_ and not created and not stored and not statements:
        return fail(" · ".join(errors_))
    kinds = defaultdict(int)
    for d in created:
        kinds[erfassung.ARTEN[d["art"]]] += 1
    parts = [f"{n}× {k}" for k, n in kinds.items()] + statements
    if stored:
        parts.append(f"{stored} Datei(en) in der Inbox (Kontoauszug als CSV/Excel: unter Bank importieren)")
    msg = ", ".join(parts)
    if errors_:
        msg += f" · Probleme: {' · '.join(errors_)}"
    if art:
        to = EINGANG_BACK.get(art, "/kreditoren?status=entwurf")
    elif created:
        to = DRAFT_TAB["verkauf" if all(d["art"] == "debitor" for d in created) else "einkauf"]
    else:
        to = "/bank" if statements else "/#inbox"
    return done(msg, to, "warn" if errors_ else "ok")


async def eingang_entwurf(ui: UI, request: Request):
    draft_id, aktion = request.path_params["id"], request.path_params["aktion"]
    back = request.headers.get("hx-current-url") or "/kreditoren?status=entwurf"
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


async def eingang_freigeben(ui: UI, request: Request):
    """«Freigeben» for the ticked drafts in the list: each is booked as it stands; one that still needs
    a person (warning, new supplier) stays and says why."""
    f = await request.form()
    ids = f.getlist("entwurf")
    back = request.headers.get("hx-current-url") or "/kreditoren?status=entwurf"
    if not ids:
        return fail("Keinen Entwurf gewählt.")
    from .. import plugins
    booked, problems, follow = [], [], None
    for draft_id in ids:
        try:
            res = await asyncio.to_thread(api.bill_draft_approve, ui.book(), draft_id)
            booked.append(draft_id)
            follow = follow or (plugins.after_beleg(ui.book(), _beleg_of(res)) if _beleg_of(res) else None)
        except (BookError, FormatError, ValueError) as exc:
            problems.append(str(exc))
    if not booked:
        return fail(" · ".join(problems))
    msg = f"{len(booked)} freigegeben ({', '.join(booked)})" + (f" · offen: {' · '.join(problems)}" if problems else "")
    return done(msg, follow or back, "warn" if problems else "ok")


def draft_vat(meta: dict) -> list[dict]:
    """The VAT a draft would book, per code: what the list shows before «Freigeben»."""
    from .. import erfassung, mwst as m
    v = erfassung.form_values(meta)
    lines = [(p.get("mwst") or "", p.get("betrag")) for p in v["positionen"]] or [(v["mwst"], v["betrag"])]
    out: dict[str, Decimal] = {}
    for code, gross in lines:
        if not code:
            continue
        c = m.CODES.get(code)
        try:
            tax = m.tax_from_gross(parse_amount(str(gross)), c.rate) if c and c.kind != "bezug" else Decimal(0)
        except (FormatError, InvalidOperation, TypeError):
            tax = Decimal(0)
        out[code] = out.get(code, Decimal(0)) + tax
    return [{"code": k, "betrag": t} for k, t in out.items()]


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
                     employees={nr: e for nr, e in payroll.employees(book).items() if e.get("aktiv", True)},
                     liquid=liquid, kind=file_kind(book.root / meta["datei"]), quellen_felder=meta.get("felder") or {},
                     dnav=draft_nav(book, meta),
                     accounts=account_options(book), currencies=currencies(book),
                     today=date.today().isoformat())


async def quittung_buchen(ui: UI, request: Request):
    f = await request.form()
    draft_id = f.get("entwurf", "")
    how = f.get("zahlung") or "konto"
    if how.startswith("bank:"):
        zahlung = {"art": "bank", "bank": how[5:]}
    elif how.startswith("buchung:"):
        zahlung = {"art": "buchung", "beleg": how[8:]}
    elif how == "spesen":
        zahlung = {"art": "spesen", "mitarbeiter": f.get("mitarbeiter") or "", "spesenart": f.get("spesenart") or "uebrige"}
    else:
        zahlung = {"art": "konto", "konto": acct(f.get("zahlkonto")) or ui.book().settings.konto("bank")}
    codes = f.getlist("p_mwst") or [""] * len(f.getlist("p_konto"))
    lines = [{"konto": acct(k), "betrag": b, "text": t, "mwst": m}
             for k, b, t, m in zip(f.getlist("p_konto"), f.getlist("p_betrag"), f.getlist("p_text"), codes) if k or b]
    cur = (f.get("waehrung") or "CHF").upper()
    return await act(request, api.receipt_book,
                     then_plugin(ui.book(), after_draft(ui.book(), "quittung", draft_id, f.get("weiter") == "1")),
                     ui.book(), draft_id, f.get("datum"),
                     (f.get("text") or "").strip() or "Quittung", f.get("betrag"), acct(f.get("konto")),
                     f.get("mwst") or "", lines or None, "" if cur == "CHF" else cur, f.get("kurs") or None, zahlung,
                     f.get("leistung_von") or None, f.get("leistung_bis") or None)


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
                     dnav=draft_nav(book, meta) if meta else None,
                     chosen=v.get("kunde") if v.get("kunde") in custs else "",
                     kind=file_kind(book.root / meta["datei"]) if meta else None,
                     quellen_felder=(meta or {}).get("felder") or {}, accounts=account_options(book),
                     today=date.today().isoformat())


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
    fields = {k: (f.get(k) or "").strip() for k in ("datum", "faellig", "rechnungsnr", "referenz", "mwst", "entwurf",
                                                     "kurs", "leistung_von", "leistung_bis")}
    if (f.get("waehrung") or "CHF").upper() != "CHF":
        fields["waehrung"] = f.get("waehrung").upper()
    fields = {k: v for k, v in fields.items() if v}
    if f.get("konto"):
        fields["konto"] = acct(f.get("konto"))
    if f.get("datei") and not fields.get("entwurf"):
        fields["datei"] = f.get("datei")
    to = (after_draft(book, "debitor", fields["entwurf"], f.get("weiter") == "1") if fields.get("entwurf")
          else lambda r: f"/debitoren/rechnung/{r['rechnung']['nummer']}")
    return await act(request, api.invoice_external, to, book, kunde, f.get("betrag"), **fields)


async def kreditoren_einstellung(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.settings_update, "/kreditoren?status=entwurf", ui.book(),
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
                         accounts=account_options(book), today=date.today().isoformat(),
                         entwurf=meta, ev=v, quellen_felder=meta.get("felder") or {}, currencies=currencies(book),
                         warnungen=erfassung.warnings(book, meta), dnav=draft_nav(book, meta),
                         iban_pruefen=any("IBAN" in w for w in erfassung.warnings(book, meta)))
    if datei:
        try:
            scan = kred.scan(book, datei)
        except Exception as exc:
            scan_error = str(exc)
    sups = kred.suppliers(book)
    chosen = (scan or {}).get("lieferant") or request.query_params.get("lieferant") or ""
    kind = file_kind(book.root / datei) if datei else None
    return ui.render(request, "kreditor_neu.html", book=book, datei=datei, kind=kind, scan=scan, scan_error=scan_error,
                     suppliers=sups, chosen=chosen, accounts=account_options(book),
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
                                                     "mitteilung", "iban", "datei", "entwurf", "leistung_von",
                                                     "leistung_bis")}
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
    to = (after_draft(book, "kreditor", fields["entwurf"], f.get("weiter") == "1") if fields.get("entwurf")
          else lambda r: f"/kreditoren/rechnung/{r['kreditor']['nummer']}")
    return await act(request, api.bill_add, then_plugin(book, to), book, lieferant, f.get("betrag"), **fields)


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
                     today=date.today().isoformat())


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
    return ui.render(request, "zahlungen.html", book=book, runs=kred.runs(book),
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
                     accounts=account_options(book), edit=request.query_params.get("edit"))


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
    herkunft = mwst.herkunft(book, periode) if rep else None
    overview = [mwst.report(book, p) for p in periods] if cfg["methode"] != "keine" else []
    if cfg["methode"] == "saldo":
        lines = ZIFFERN + [("322", f"Leistungen zum Saldosteuersatz {cfg['saldosteuersatz']} %", "steuer"),
                           ("399", "Total geschuldete Steuer", "zw")]
    else:
        lines = ZIFFERN + ZIFFERN_EFFEKTIV
    return ui.render(request, "mwst.html", book=book, cfg=cfg, rep=rep, year=year, periods=periods,
                     overview=overview, lines=lines, codes=mwst.CODES, herkunft=herkunft,
                     period_label=mwst.period_label)


async def mwst_buchen(ui: UI, request: Request):
    f = await request.form()
    periode = f.get("periode")
    return await act(request, api.mwst_book, f"/mwst?periode={periode}&jahr={periode[:4]}", ui.book(), periode)


async def mwst_abstimmung(ui: UI, request: Request):
    from .. import mwst
    book = ui.book()
    cfg = mwst.config(book)
    year = year_param(request, book)
    rep = mwst.abstimmung(book, year) if cfg["methode"] != "keine" else None
    return ui.render(request, "mwst_abstimmung.html", book=book, cfg=cfg, rep=rep, year=year)


async def mwst_abgrenzung(ui: UI, request: Request):
    f = await request.form()
    jahr = int(f.get("jahr") or 0)
    return await act(request, api.mwst_abgrenzung, f"/mwst/abstimmung?jahr={jahr}", ui.book(), jahr,
                     f.get("neu") == "1")


async def mwst_abstimmung_pdf(ui: UI, request: Request):
    from .. import mwst, pdf as pdfmod
    book = ui.book()
    year = year_param(request, book)
    try:
        data = pdfmod.mwst_abstimmung_pdf(book, mwst.abstimmung(book, year))
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="MWST-Umsatzabstimmung {year}.pdf"'})


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
    data = pdfmod.mwst_pdf(book, rep, mwst.herkunft(book, rep["periode"]))
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="MWST {rep["periode"]}.pdf"'})


# ---------- Verlauf ----------

async def verlauf(ui: UI, request: Request):
    book = ui.book()
    qp = request.query_params
    beleg, von, suche = qp.get("beleg", "").strip(), qp.get("von", "").strip(), qp.get("q", "").strip()
    users = ui.users.list() if ui.users is not None else []
    return ui.render(request, "verlauf.html", book=book, log=api.history(book, 200, beleg, von, suche),
                     agent_mark="Agent", beleg=beleg, von=von, q=suche, benutzer=users,
                     belegverlauf=api.booking_history(book, beleg) if beleg else None)


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
    from .. import jev
    return ui.render(request, "einstellungen.html", book=book, cfg=payroll.config(book),
                     iban_problem=iban_problem(iban) if iban else "Keine IBAN hinterlegt",
                     qr_iban=is_qr_iban(iban) if iban else False, issues=checks.run(book),
                     accounts=account_options(book), cred=credentials_status(book.settings.get("agent_backend")),
                     jev=jev.config(book), jev_datei=str(jev.key_file()).replace(str(Path.home()), "~", 1),
                     ki=__import__("aeradex.web.chat", fromlist=["openai_status"]).openai_status(),
                     backends=__import__("aeradex.web.chat", fromlist=["BACKENDS"]).BACKENDS,
                     plugin_rows=api.plugin_list(book), katalog=_katalog(book), server_mode=ui.auth_dir is not None,
                     neustart=bool(getattr(ui, "neustart_noetig", False)),
                     kantone=__import__("aeradex.qst_estv", fromlist=["CANTONS"]).CANTONS,
                     qst_geladen=sorted(p.stem.replace("-", " ") for p in (book.root / "lohn" / "qst_tarife").glob("??-????.json")),
                     benutzer_zahl=len(ui.users.list()) if ui.users is not None else 0,
                     jahre=jahreswechsel.overview(book), eb=__import__("aeradex.eroeffnung", fromlist=["view"]).view(book))


async def jahre_einstellung(ui: UI, request: Request):
    """Settings › Geschäftsjahre: open the next year, move the first one."""
    f = await request.form()
    book = ui.book()
    if f.get("aktion") == "erstes":
        try:
            jahr = int(f.get("erstes_jahr") or 0)
        except ValueError:
            return fail("Jahr als Zahl angeben, z.B. 2027.")
        return await act(request, api.first_year_set, "/einstellungen#jahre", book, jahr)
    return await act(request, api.year_open, "/einstellungen#jahre", book, jahreswechsel.next_year(book))


async def eroeffnung_seite(ui: UI, request: Request):
    from .. import eroeffnung
    book = ui.book()
    v = eroeffnung.view(book)
    proposal = eroeffnung.staged(book) or {}
    werte = proposal.get("werte") or {}
    for r in v["zeilen"]:
        r["vorschlag"] = werte.get(r["konto"])
    used = [r for r in v["zeilen"] if r["wert"] or r["eroeffnung"] or r["vorschlag"] is not None]
    alle = request.query_params.get("alle") == "1" or not used

    def shown(r):
        return alle or r in used
    return ui.render(request, "eroeffnung.html", v=v, vorschlag=proposal, alle=alle, accounts=account_options(book),
                     neu=[dict(n, betrag=werte.get(n["nr"])) for n in proposal.get("neu") or []],
                     bilanz=[r for r in v["zeilen"] if r["bilanz"] and shown(r)],
                     erfolg=[r for r in v["zeilen"] if not r["bilanz"] and shown(r)])


async def eroeffnung_speichern(ui: UI, request: Request):
    f = await request.form()
    book = ui.book()
    werte = {k[2:]: (v or "").strip() for k, v in f.multi_items() if k.startswith("w_")}
    neu = []
    try:
        for nr in f.getlist("neu_nr"):
            amount = parse_amount(f.get(f"n_{nr}") or "0", f"Konto {nr}")
            ziel = acct(f.get(f"neu_ziel_{nr}") or "")
            if ziel:                                      # booked onto an existing account instead
                werte[ziel] = str(parse_amount(werte.get(ziel) or "0", f"Konto {ziel}") + amount)
            else:
                neu.append({"nr": nr, "name": (f.get(f"neu_name_{nr}") or "").strip()})
                werte[nr] = str(amount)
        result = await asyncio.to_thread(api.opening_set, book, werte, neu)
    except (BookError, FormatError, ValueError) as exc:
        return fail(str(exc))
    from .. import eroeffnung
    eroeffnung.discard(book)
    return done(result.get("meldung", "Gespeichert"), "/eroeffnung")


async def eroeffnung_einlesen(ui: UI, request: Request):
    f = await request.form()
    book = ui.book()
    if f.get("verwerfen") == "1":
        from .. import eroeffnung
        eroeffnung.discard(book)
        return done("Vorschlag verworfen", "/eroeffnung")
    upload = f.get("datei")
    if upload is not None and getattr(upload, "filename", ""):
        name, data = upload.filename, await upload.read()
    elif (f.get("text") or "").strip():
        name, data = "eingefuegt.txt", f.get("text").encode()
    else:
        return fail("Datei wählen oder die Saldenliste ins Textfeld einfügen.")
    try:
        p = await asyncio.to_thread(api.opening_read, book, name, data)
    except (BookError, FormatError, ValueError) as exc:
        return fail(str(exc))
    return done(f"{len(p['werte'])} Konten eingelesen — prüfen und speichern", "/eroeffnung#salden")


SETTINGS_TABS = ("firma", "buchhaltung", "jahre", "lohn", "agent", "funktionen", "buch")


async def einstellungen_speichern(ui: UI, request: Request):
    """One handler for the forms of several tabs: only what a form sends is changed."""
    f = await request.form()
    fields = {k: (f.get(k) or "").strip() for k in ("firma", "rechtsform", "uid", "telefon", "email", "iban",
                                                     "qr_referenz_praefix", "zahlungsfrist_tage", "agent_modus", "co",
                                                     "zahlungs_iban") if k in f}
    vat = {"methode": f.get("mwst_methode") or "keine", "periode": f.get("mwst_periode") or "",
           "saldosteuersatz": (f.get("mwst_saldosteuersatz") or "").strip(),
           "taetigkeit": (f.get("mwst_taetigkeit") or "").strip(), "abrechnungsart": f.get("mwst_abrechnungsart") or ""}
    if "mwst_konto" in f:
        vat["konto"] = acct(f.get("mwst_konto"))
    if "mwst_abrechnungskonto" in f:
        vat["konten"] = {"abrechnung": acct(f.get("mwst_abrechnungskonto"))}
    adresse = {k: (f.get(f"a_{k}") or "").strip() for k in ("strasse", "nr", "plz", "ort", "land") if f"a_{k}" in f}
    konten = {k: acct(f.get(f"k_{k}")) for k in ("bank", "debitoren", "ertrag", "gutschrift", "gewinnvortrag",
                                                  "jahresergebnis", "dividende", "reserve") if f"k_{k}" in f}
    if "firma" in fields and not fields["firma"]:
        return fail("Firmenname ist nötig.")
    tab = f.get("_tab") if f.get("_tab") in SETTINGS_TABS else ""
    return await act(request, api.settings_update, "/einstellungen" + (f"#{tab}" if tab else ""), ui.book(),
                     adresse=adresse or None, konten=konten or None,
                     mwst=vat if f.get("mwst_methode") is not None else None, **fields)


async def logo_einstellung(ui: UI, request: Request):
    f = await request.form()
    if f.get("entfernen") == "1":
        return await act(request, api.logo_remove, "/einstellungen#firma", ui.book())
    upload = f.get("logo")
    if not upload or not getattr(upload, "filename", ""):
        return fail("Keine Datei gewählt.")
    return await act(request, api.logo_set, "/einstellungen#firma", ui.book(), upload.filename, await upload.read())


async def logo_bild(ui: UI, request: Request):
    """The stored logo, for the preview in Einstellungen."""
    book = ui.book()
    rel = str(book.settings.get("logo") or "")
    path = (book.root / rel).resolve() if rel else None
    if not path or not path.is_file() or not path.is_relative_to(book.root.resolve()):
        return Response(status_code=404)
    return Response(path.read_bytes(), media_type="image/png" if path.suffix == ".png" else "image/jpeg",
                    headers={"Cache-Control": "no-store"})


async def agent_einstellung(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.settings_update, "/einstellungen#agent", ui.book(),
                     agent_backend=f.get("agent_backend") or "auto")


async def jev_schluessel(ui: UI, request: Request):
    """The TypeSafe key goes to <config>/typesafe.env (chmod 600), never into the book."""
    from .. import jev
    f = await request.form()
    try:
        if f.get("entfernen") == "1":
            jev.delete_key()
            return done("API-Schlüssel entfernt", "/einstellungen#jev")
        jev.save_key(f.get("schluessel") or "")
    except (BookError, OSError) as exc:
        return fail(str(exc))
    return done("API-Schlüssel gespeichert", "/einstellungen#jev")


async def ki_zugang(ui: UI, request: Request):
    """Access to an OpenAI-compatible model (Google Gemini …) goes to <config>/ki.env (chmod 600), never into the book."""
    from . import chat
    f = await request.form()
    try:
        if f.get("entfernen") == "1":
            chat.delete_openai()
            return done("Zugang entfernt", "/einstellungen#ki")
        chat.save_openai(f.get("adresse") or "", f.get("modell") or "", f.get("schluessel") or "")
    except (ValueError, OSError) as exc:
        return fail(str(exc))
    return done("Zugang gespeichert", "/einstellungen#ki")


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
        data = pdfmod.statement_pdf(book, st, statements.anhang_blocks(book, year))
        name = f"Jahresrechnung {year}.pdf"
    elif kind == "journal":
        data = pdfmod.journal_pdf(book, [r for r in book.rows if r.datum.year == year], f"Journal {year}")
        name = f"Journal {year}.pdf"
    elif kind == "kontoblatt":
        nr = request.query_params.get("konto", "")
        data = pdfmod.ledger_pdf(book, account_ledger(book, nr, year))
        name = f"Konto {nr} {year}.pdf"
    elif kind in ("debitoren", "kreditoren"):
        try:
            raw = request.query_params.get("stichtag")
            stichtag = parse_date(raw, "stichtag") if raw else None
        except FormatError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        if kind == "debitoren":
            ar = invoices.aged_receivables(book, stichtag)
            data = pdfmod.receivables_pdf(book, ar)
        else:
            from .. import kreditoren as kred
            ar = kred.open_payables(book, stichtag)
            data = pdfmod.payables_pdf(book, ar)
        name = f"Offene {kind.capitalize()} {ar['stichtag']:%Y-%m-%d}.pdf"
    else:
        return PlainTextResponse("Unbekannter Bericht", status_code=404)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})


# ---------- Abschlussunterlagen and the .aeradex file ----------

MIME = {".pdf": "application/pdf", ".csv": "text/csv; charset=utf-8", ".zip": "application/zip", ".xml": "application/xml",
        ".yaml": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
        ".aeradex": "application/vnd.aeradex+zip"}


def download(data: bytes, name: str, inline: bool = False) -> Response:
    from urllib.parse import quote
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    disposition = f'{"inline" if inline else "attachment"}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'
    return Response(data, media_type=MIME.get(Path(name).suffix.lower(), "application/octet-stream"),
                    headers={"Content-Disposition": disposition})


async def unterlagen_teil(ui: UI, request: Request):
    from .. import dossier
    book = ui.book()
    year = year_param(request, book)
    fmt = request.query_params.get("format", "pdf")
    try:
        name, data = await asyncio.to_thread(dossier.build_part, book, year, request.path_params["teil"], fmt)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return download(data, name, inline=name.endswith(".pdf"))


async def unterlagen_zip(ui: UI, request: Request):
    from .. import dossier
    book = ui.book()
    year = year_param(request, book)
    teile = request.query_params.getlist("teil") or None
    formate = request.query_params.getlist("format") or None
    try:
        name, data, _ = await asyncio.to_thread(dossier.build_zip, book, year, teile, formate)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return download(data, name)


async def buch_export(ui: UI, request: Request):
    from .. import austausch
    f = await request.form()
    book = ui.book()
    password = f.get("passwort") or None
    if password and password != f.get("passwort2"):
        return PlainTextResponse("Passwörter stimmen nicht überein.", status_code=400)
    name = f"{book.settings.firma} {date.today().isoformat()}.aeradex"
    with tempfile.TemporaryDirectory(prefix="aeradex-export-") as tmp:
        target = Path(tmp) / name
        try:
            await asyncio.to_thread(austausch.export_book, book, target, password, f.get("inbox") == "1",
                                    f.get("historie") == "1")
        except BookError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return download(target.read_bytes(), name)


# ---------- Datensicherung ----------

async def datensicherung_seite(ui: UI, request: Request):
    book = ui.book()
    raw = request.query_params.get("jahr", "")
    jahr = int(raw) if raw.isdigit() and int(raw) in book.years() else None
    ov = await asyncio.to_thread(api.backup_overview, book, jahr)
    gruppen: dict[str, list] = {}
    for d in ov["datensaetze"]:
        gruppen.setdefault(d["gruppe"], []).append(d)
    return ui.render(request, "datensicherung.html", book=book, ov=ov, gruppen=gruppen, jahr=jahr)


def _log_backup(ui: UI, request: Request, was: str) -> None:
    user = getattr(request.state, "user", None)
    if user is not None and ui.userlog is not None:
        ui.userlog.add(user.name, "datensicherung", notiz=was)


async def datensicherung_datei(ui: UI, request: Request):
    book = ui.book()
    qp = request.query_params
    jahr = int(qp["jahr"]) if qp.get("jahr", "").isdigit() else None
    try:
        name, data = await asyncio.to_thread(api.backup_part, book, qp.get("key", ""), qp.get("format", "csv"), jahr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    _log_backup(ui, request, name)
    return download(data, name, inline=name.endswith(".pdf"))


async def datensicherung_zip(ui: UI, request: Request):
    book = ui.book()
    qp = request.query_params
    jahr = int(qp["jahr"]) if qp.get("jahr", "").isdigit() else None
    keys = qp.getlist("key") or None
    formate = qp.getlist("format") or None
    try:
        name, data, _ = await asyncio.to_thread(api.backup_zip, book, keys, jahr, formate, qp.get("originale") == "1")
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    _log_backup(ui, request, name)
    return download(data, name)


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
        Route("/journal/aendern", h(journal_aendern), methods=["POST"]),
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
        Route("/debitoren/akonto", h(rechnung_akonto)),
        Route("/debitoren/zuordnen", h(zuordnen), methods=["POST"]),
        Route("/debitoren/offene-posten", h(offene_posten)),
        Route("/kreditoren/offene-posten", h(kreditoren_offene_posten)),
        Route("/debitoren/mahnungen", h(mahnungen_page)),
        Route("/debitoren/mahnungen", h(mahnungen_erstellen), methods=["POST"]),
        Route("/debitoren/kunden", h(kunden)),
        Route("/debitoren/kunden/neu", h(kunde_speichern), methods=["POST"]),
        Route("/debitoren/kunden/{nr:str}", h(kunde)),
        Route("/debitoren/kunden/{nr:str}", h(kunde_speichern), methods=["POST"]),
        Route("/debitoren/kunden/{nr:str}/log", h(kunde_log), methods=["POST"]),
        Route("/debitoren/kunden/{nr:str}/log/{index:int}", h(kunde_log_erledigt), methods=["POST"]),
        Route("/debitoren/kunden/{nr:str}/infos", h(kunde_infos), methods=["POST"]),
        Route("/debitoren/rechnung/{nr:str}", h(rechnung)),
        Route("/debitoren/rechnung/{nr:str}/{aktion:str}", h(rechnung_aktion), methods=["POST"]),
        Route("/bank/bewegungen", h(bank_bewegungen), methods=["GET"]),
        Route("/bank/regeln", h(bank_regeln), methods=["GET"]),
        Route("/bank/abstimmung", h(bank_abstimmung), methods=["GET"]),
        Route("/bank/{id:str}/suchen", h(bank_suchen), methods=["GET"]),
        Route("/vorschlaege", h(vorschlaege_page), methods=["GET"]),
        Route("/bank", h(bank_page)),
        Route("/bank/import", h(bank_upload), methods=["POST"]),
        Route("/bank/jev", h(bank_jev), methods=["POST"]),
        Route("/bank/alle-abgleichen", h(bank_alle), methods=["POST"]),
        Route("/bank/regel", h(bank_regel), methods=["POST"]),
        Route("/bank/format/{name:str}/bestaetigen", h(bank_format_ok), methods=["POST"]),
        Route("/bank/karte/importieren", h(bank_karte_import), methods=["POST"]),
        Route("/bank/regel/{id:str}/entfernen", h(bank_regel), methods=["POST"]),
        Route("/bank/{id:str}/{aktion:str}", h(bank_aktion), methods=["POST"]),
        Route("/p/{plugin:str}/{slug:str}", h(plugin_seite)),
        Route("/p/{plugin:str}/{slug:str}/{aktion:str}", h(plugin_aktion), methods=["POST"]),
        Route("/p/{plugin:str}/{slug:str}/{teil:str}", h(plugin_teil)),
        Route("/lohn/spesen", h(spesen_aktion), methods=["POST"]),
        Route("/lohn/spesen/{nr:str}/entfernen", h(spesen_aktion), methods=["POST"]),
        Route("/journal/raster", h(journal_raster), methods=["POST"]),
        Route("/journal/vorlage.xlsx", h(journal_vorlage)),
        Route("/journal/umbuchen/vorschau", h(journal_umbuchen_vorschau), methods=["POST"]),
        Route("/journal/umbuchen", h(journal_umbuchen), methods=["POST"]),
        Route("/journal/import", h(journal_import), methods=["POST"]),
        Route("/journal/auslesen", h(journal_auslesen), methods=["POST"]),
        Route("/kreditoren", h(kreditoren_page)),
        Route("/kreditoren/neu", h(kreditor_neu)),
        Route("/kreditoren/einlesen", h(eingang_einlesen), methods=["POST"]),
        Route("/kreditoren/entwurf/{id:str}/{aktion:str}", h(eingang_entwurf), methods=["POST"]),
        Route("/eingang/einlesen", h(eingang_einlesen), methods=["POST"]),
        Route("/eingang/freigeben", h(eingang_freigeben), methods=["POST"]),
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
        Route("/lohn/zahlung/{aktion:str}", h(lohnzahlung), methods=["POST"]),
        Route("/lohn/qst-import", h(qst_importieren), methods=["POST"]),
        Route("/lohn/lauf", h(lohnlauf), methods=["POST"]),
        Route("/lohn/abschliessen", h(lohnlauf_abschliessen), methods=["POST"]),
        Route("/lohn/abrechnung/{monat:str}/{nr:str}", h(abrechnung)),
        Route("/lohn/abrechnung/{monat:str}/{nr:str}/{aktion:str}", h(abrechnung_aktion), methods=["POST"]),
        Route("/lohn/mitarbeiter", h(mitarbeiter)),
        Route("/lohn/qst-status", h(qst_status)),
        Route("/lohn/mitarbeiter/neu", h(mitarbeiter_speichern), methods=["POST"]),
        Route("/lohn/mitarbeiter/{nr:str}", h(mitarbeiter_speichern), methods=["POST"]),
        Route("/lohn/lohnkonto", h(lohnkonto_firma)),
        Route("/lohn/lohnmeldungen", h(lohnmeldungen)),
        Route("/lohn/{art:str}/datei.{fmt:str}", h(lohn_datei)),
        Route("/lohn/lohnkonto/{jahr:int}/{nr:str}", h(lohnkonto)),
        Route("/lohn/lohnausweis/{jahr:int}/{nr:str}", h(lohnausweis_erstellen), methods=["POST"]),
        Route("/abschluss", h(abschluss)),
        Route("/abschluss/unterlagen.zip", h(unterlagen_zip)),
        Route("/abschluss/unterlagen/{teil:str}", h(unterlagen_teil)),
        Route("/abschluss/vorschau", h(abschluss_vorschau)),
        Route("/abschluss/gruppe", h(abschluss_gruppe), methods=["POST"]),
        Route("/abschluss/buchung/{art:str}", h(abschluss_buchung)),
        Route("/abschluss/{tab:str}", h(abschluss)),
        Route("/abschluss/buchung/{art:str}", h(abschluss_buchung_post), methods=["POST"]),
        Route("/abschluss/buchung/{art:str}/entfernen", h(abschluss_buchung_post), methods=["POST"]),
        Route("/abschluss/{aktion:str}", h(abschluss_aktion), methods=["POST"]),
        Route("/mwst", h(mwst_page)),
        Route("/mwst/buchen", h(mwst_buchen), methods=["POST"]),
        Route("/mwst/abstimmung", h(mwst_abstimmung)),
        Route("/mwst/abstimmung/pdf", h(mwst_abstimmung_pdf)),
        Route("/mwst/abgrenzung", h(mwst_abgrenzung), methods=["POST"]),
        Route("/mwst/pdf", h(mwst_pdf)),
        Route("/mwst/xml", h(mwst_xml)),
        Route("/verlauf", h(verlauf)),
        Route("/verlauf/{hash:str}", h(commit)),
        Route("/einstellungen", h(einstellungen)),
        Route("/einstellungen", h(einstellungen_speichern), methods=["POST"]),
        Route("/einstellungen/lohn", h(lohn_einstellungen), methods=["POST"]),
        Route("/einstellungen/jahre", h(jahre_einstellung), methods=["POST"]),
        Route("/eroeffnung", h(eroeffnung_seite)),
        Route("/eroeffnung", h(eroeffnung_speichern), methods=["POST"]),
        Route("/eroeffnung/einlesen", h(eroeffnung_einlesen), methods=["POST"]),
        Route("/einstellungen/buch-export", h(buch_export), methods=["POST"]),
        Route("/datensicherung", h(datensicherung_seite)),
        Route("/datensicherung/datei", h(datensicherung_datei)),
        Route("/datensicherung/zip", h(datensicherung_zip)),
        Route("/einstellungen/logo", h(logo_einstellung), methods=["POST"]),
        Route("/einstellungen/logo.bild", h(logo_bild)),
        Route("/einstellungen/agent", h(agent_einstellung), methods=["POST"]),
        Route("/einstellungen/jev", h(jev_einstellung), methods=["POST"]),
        Route("/einstellungen/jev/schluessel", h(jev_schluessel), methods=["POST"]),
        Route("/einstellungen/ki", h(ki_zugang), methods=["POST"]),
        Route("/einstellungen/plugins/installieren", h(plugin_installieren), methods=["POST"]),
        Route("/einstellungen/plugins/neustart", h(plugin_neustart), methods=["POST"]),
        Route("/einstellungen/plugins/{aktion:str}", h(plugin_einstellung), methods=["POST"]),
        Route("/pdf/{kind:str}", h(pdf_report)),
    ]

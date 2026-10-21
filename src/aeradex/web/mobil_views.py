"""The phone view (`/m`): what is needed on the road, in a slim layout with a tab bar. Heute, Kunden (everything of
the dossier, call/mail/navigate), Rechnungen (Entwürfe … Bezahlt), Beleg scannen (the browser finds and flattens the
sheet, see static/scan.js), Belege prüfen/buchen and the agent with voice input. Same data and the same api writes
as the desktop pages; phones are sent here from `/` unless the person chose the desktop view (cookie `ansicht`)."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import api, invoices
from ..book import Book, BookError
from ..files import FormatError
from ..ledger import BalanceEngine
from .app import UI, acct, act, done, fail

ZERO = Decimal("0")
VIEW_COOKIE = "ansicht"
PHONE = re.compile(r"iPhone|iPod|Android.+Mobile|Mobile.+Firefox|Windows Phone|BlackBerry|Opera Mini", re.I)
APPLE = re.compile(r"iPhone|iPad|iPod", re.I)

READABLE = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".txt")
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
INVOICE_FILTERS = [("offen", "Offen"), ("ueberfaellig", "Überfällig"), ("gemahnt", "Gemahnt"), ("entwurf", "Entwürfe"),
                   ("bezahlt", "Bezahlt"), ("alle", "Alle")]
DRAFT_KINDS = {"kreditor": "Lieferantenrechnung", "quittung": "Quittung", "debitor": "Eigene Rechnung"}
DESKTOP_REVIEW = {"kreditor": "/kreditoren/neu?entwurf=", "quittung": "/eingang/quittung?entwurf=",
                  "debitor": "/debitoren/extern?entwurf="}


# ---------- which view ----------

def is_phone(request: Request) -> bool:
    if request.headers.get("sec-ch-ua-mobile") == "?1":
        return True
    return bool(PHONE.search(request.headers.get("user-agent", "")))


def wants_phone(request: Request) -> bool:
    """A phone gets the phone view, unless the person switched to the desktop view (and the other way round)."""
    choice = request.cookies.get(VIEW_COOKIE, "")
    if choice in ("desktop", "handy"):
        return choice == "handy"
    return is_phone(request)


def maps_url(address: str, apple: bool) -> str:
    """Apple Maps on an iPhone, Google Maps elsewhere (on Android the link opens the Maps app)."""
    if apple:
        return f"https://maps.apple.com/?q={quote(address)}"
    return f"https://www.google.com/maps/search/?api=1&query={quote(address)}"


def address_line(c: dict) -> str:
    a = c.get("adresse") or {}
    street = f"{a.get('strasse') or ''} {a.get('nr') or ''}".strip()
    town = f"{a.get('plz') or ''} {a.get('ort') or ''}".strip()
    return ", ".join(x for x in (street, town) if x)


def render(ui: UI, request: Request, template: str, **ctx):
    ctx.setdefault("apple", bool(APPLE.search(request.headers.get("user-agent", ""))))
    ctx.setdefault("today", date.today().isoformat())
    return ui.render(request, f"m/{template}", **ctx)


def can_write(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return user is None or user.can_write


# ---------- shared data ----------

def invoice_rows(book: Book, today: str | None = None) -> list[dict]:
    """Every invoice with its state, the reminder level and whether it is overdue."""
    from .. import mahnungen
    today = today or date.today().isoformat()
    paid = invoices.settlements(book)
    rows = []
    for nr, meta in invoices.invoices(book).items():
        st = invoices.invoice_state(book, meta, paid.get(nr, []))
        is_open = st["status"] in ("offen", "teilbezahlt")
        rem = mahnungen.history(book, nr) if is_open else []
        st["mahnstufe"] = max((int(m.get("stufe") or 0) for m in rem), default=0)
        st["ueberfaellig"] = is_open and st["faellig"] < today
        rows.append(st)
    rows.sort(key=lambda r: (r["datum"], r["nummer"]), reverse=True)
    return rows


def draft_state(book: Book, d: dict) -> dict:
    """How a draft stands, in one pill: label, colour, and why it needs the desktop (hold)."""
    status = d.get("status")
    hold = api.bill_draft_hold(book, d) if status == "bereit" else ""
    if status == "agent":
        return {"label": "Agent arbeitet …", "cls": "info", "hold": ""}
    if status == "bereit" and not hold:
        return {"label": "bereit", "cls": "ok", "hold": ""}
    if status == "bereit":
        return {"label": "prüfen", "cls": "warn", "hold": hold}
    if status == "konflikt":
        return {"label": "Name ≠ IBAN", "cls": "red", "hold": d.get("konflikt") or ""}
    if status == "unvollstaendig":
        return {"label": "unvollständig", "cls": "warn", "hold": ""}
    return {"label": "Konto offen", "cls": "warn", "hold": ""}


def draft_list(book: Book) -> list[dict]:
    from .. import erfassung
    out = []
    for d in sorted(erfassung.drafts(book).values(), key=lambda d: d["id"], reverse=True):
        out.append({"id": d["id"], "art": d.get("art", "kreditor"), "kind": DRAFT_KINDS.get(d.get("art", "kreditor"), ""),
                    "name": erfassung.value(d, "name"), "betrag": erfassung.value(d, "betrag"),
                    "waehrung": erfassung.value(d, "waehrung"), "datum": erfassung.value(d, "datum"),
                    "datei": d.get("datei", ""), **draft_state(book, d)})
    return out


def liquid(book: Book) -> list[dict]:
    year = date.today().year
    if year not in book.years():
        year = max(book.years())
    eng = BalanceEngine(book)
    return [{"nr": nr, "name": a.name, "saldo": eng.balance(nr, year)}
            for nr, a in sorted(book.accounts.items()) if a.gruppe == "fluessige" and a.aktiv_]


# ---------- pages ----------

async def heute(ui: UI, request: Request):
    from .. import kunden as crm
    from .views import todo_items, unread_inbox
    book = ui.book()
    today = date.today()
    rows = await asyncio.to_thread(invoice_rows, book, today.isoformat())
    overdue = sorted((r for r in rows if r["ueberfaellig"]), key=lambda r: r["faellig"])
    drafts = draft_list(book)
    shown = {"Wiedervorlage", "Entwürfe", "Inbox"}
    todos = [t for t in await asyncio.to_thread(todo_items, book) if t["tag"] not in shown]
    return render(ui, request, "heute.html", book=book, tab="heute", wochentag=WEEKDAYS[today.weekday()],
                  wiedervorlagen=crm.wiedervorlagen(book, today + timedelta(days=7)), overdue=overdue,
                  overdue_total=sum((r["offen_chf"] for r in overdue), ZERO), drafts=drafts,
                  unread=len(unread_inbox(book)), liquid=liquid(book), todos=todos)


async def kunden(ui: UI, request: Request):
    from .. import kunden as crm
    from .views import customer_search_text, customer_states
    book = ui.book()
    custs = invoices.customers(book)
    kat = request.query_params.get("kategorie", "")
    shown = sorted(((nr, c) for nr, c in custs.items() if not kat or c.get("kategorie") == kat),
                   key=lambda kv: (kv[1].get("firma") or kv[1].get("name") or kv[0]).lower())
    return render(ui, request, "kunden.html", book=book, tab="kunden", customers=shown, states=customer_states(book),
                  suche={nr: customer_search_text(nr, c) for nr, c in shown}, adresse=address_line,
                  kategorien=sorted({c.get("kategorie") for c in custs.values() if c.get("kategorie")}),
                  kategorie=kat, KATEGORIEN=crm.KATEGORIEN, wv={e["kunde"] for e in crm.wiedervorlagen(book, date.today())})


async def kunde(ui: UI, request: Request):
    from .. import kunden as crm
    from .views import kunde_teile_html
    book = ui.book()
    nr = request.path_params["nr"]
    try:
        d = await asyncio.to_thread(crm.dossier, book, nr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    c = d["kunde"]
    adresse = address_line(c)
    apple = bool(APPLE.search(request.headers.get("user-agent", "")))
    return render(ui, request, "kunde.html", book=book, tab="kunden", d=d, c=c, nr=nr, adresse=adresse,
                  karte=maps_url(adresse, apple) if adresse else "", teile=kunde_teile_html(ui, book, nr, d),
                  arten=crm.LOG_ARTEN, KATEGORIEN=crm.KATEGORIEN, apple=apple)


async def kunde_neu(ui: UI, request: Request):
    from .. import kunden as crm
    return render(ui, request, "kunde_neu.html", tab="kunden", KATEGORIEN=crm.KATEGORIEN)


async def kunde_speichern(ui: UI, request: Request):
    from .views import kunde_speichern as save
    return await save(ui, request, base="/m/kunden")


async def kunde_nummer(ui: UI, request: Request):
    from .views import kunde_nummer as renumber
    return await renumber(ui, request, base="/m/kunden")


async def inbox_einlesen(ui: UI, request: Request):
    """A file lying in the inbox → a draft, then its page."""
    from .. import erfassung
    from .views import _agent_in_background
    f = await request.form()
    try:
        res = await asyncio.to_thread(api.bill_draft_create, ui.book(), f"inbox/{Path(f.get('datei') or '').name}", "")
    except (BookError, FormatError, ValueError) as exc:
        return fail(str(exc))
    draft = res["entwurf"]
    if erfassung.agent_auto(ui.book()) and erfassung.needs_agent(draft):
        _agent_in_background(ui.root, [draft["id"]])
    return done(f"{draft['id']} eingelesen", f"/m/belege/{draft['id']}")


async def inbox_loeschen(ui: UI, request: Request):
    f = await request.form()
    return await act(request, api.inbox_remove, "/m/belege", ui.book(), f.get("datei") or "")


async def kunde_log(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    return await act(request, api.customer_log_add, f"/m/kunden/{nr}", ui.book(), nr, f.get("text") or "",
                     f.get("art") or "notiz", f.get("datum") or None, f.get("nachfassen") or None)


async def kunde_log_erledigt(ui: UI, request: Request):
    nr = request.path_params["nr"]
    return await act(request, api.customer_log_done, request.headers.get("hx-current-url") or f"/m/kunden/{nr}",
                     ui.book(), nr, int(request.path_params["index"]), True)


async def kunde_infos(ui: UI, request: Request):
    f = await request.form()
    nr = request.path_params["nr"]
    return await act(request, api.customer_update, f"/m/kunden/{nr}", ui.book(), nr, notizen=f.get("notizen") or "")


async def rechnungen(ui: UI, request: Request):
    from .views import group_drafts
    book = ui.book()
    status = request.query_params.get("status") or "offen"
    if status not in dict(INVOICE_FILTERS):
        status = "offen"
    rows = await asyncio.to_thread(invoice_rows, book)
    years = sorted({int(r["datum"][:4]) for r in rows if r["datum"][:4].isdigit()} | {date.today().year}, reverse=True)
    try:
        jahr = int(request.query_params.get("jahr") or 0) or None
    except ValueError:
        jahr = None
    counts = {"offen": sum(1 for r in rows if r["status"] in ("offen", "teilbezahlt")),
              "ueberfaellig": sum(1 for r in rows if r["ueberfaellig"]),
              "gemahnt": sum(1 for r in rows if r["mahnstufe"] and r["status"] in ("offen", "teilbezahlt")),
              "entwurf": len(group_drafts(book, "verkauf"))}
    pick = {"offen": lambda r: r["status"] in ("offen", "teilbezahlt"), "ueberfaellig": lambda r: r["ueberfaellig"],
            "gemahnt": lambda r: r["mahnstufe"] and r["status"] in ("offen", "teilbezahlt"),
            "bezahlt": lambda r: r["status"] == "bezahlt", "alle": lambda r: True, "entwurf": lambda r: False}[status]
    shown = [r for r in rows if pick(r) and (jahr is None or r["datum"][:4] == str(jahr))]
    if status in ("offen", "ueberfaellig", "gemahnt"):
        shown.sort(key=lambda r: (r["faellig"], r["nummer"]))
    drafts = [d for d in draft_list(book) if d["art"] == "debitor"] if status == "entwurf" else []
    open_rows = [r for r in rows if r["status"] in ("offen", "teilbezahlt")]
    return render(ui, request, "rechnungen.html", book=book, tab="rechnungen", rows=shown, status=status,
                  filters=INVOICE_FILTERS, counts=counts, drafts=drafts, jahr=jahr, years=years,
                  total_offen=sum((r["offen_chf"] for r in open_rows), ZERO),
                  total_ueberfaellig=sum((r["offen_chf"] for r in open_rows if r["ueberfaellig"]), ZERO))


async def rechnung(ui: UI, request: Request):
    from .. import mahnungen
    book = ui.book()
    nr = request.path_params["nr"]
    try:
        meta = invoices.invoice(book, nr)
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    state = invoices.invoice_state(book, meta)
    pdf = meta["_pfad"].with_suffix(".pdf")
    if meta.get("extern") and meta.get("datei"):
        pdf = book.root / meta["datei"]
    reminders = mahnungen.history(book, nr)
    return render(ui, request, "rechnung.html", book=book, tab="rechnungen", meta=meta, state=state,
                  paid=invoices.settlements(book).get(nr, []), reminders=reminders,
                  mahnstufe=max((int(m.get("stufe") or 0) for m in reminders), default=0),
                  pdf=book.rel(pdf) if pdf.exists() else None)


async def scannen(ui: UI, request: Request):
    from .. import erfassung
    book = ui.book()
    return render(ui, request, "scannen.html", book=book, tab="scannen", ocr=bool(erfassung.ocr_languages()),
                  art=request.query_params.get("art", ""))


async def scannen_hochladen(ui: UI, request: Request):
    """The flattened pages (JPEG) → one PDF in the inbox → a draft, as the desktop upload does; or a file picked as
    it is (a PDF from the phone). Answers JSON: where the draft can be looked at."""
    from .. import erfassung, scan
    from .views import _agent_in_background
    form = await request.form()
    art = (form.get("art") or "").strip()
    if art and art not in DRAFT_KINDS:
        return JSONResponse({"ok": False, "fehler": "Unbekannte Belegart"}, status_code=400)
    stamp = date.today().strftime("%Y-%m-%d")
    pages = [p for p in form.getlist("seite") if hasattr(p, "read")]
    whole = form.get("datei")
    try:
        if pages:
            data = [await p.read() for p in pages]
            pdf = await asyncio.to_thread(scan.pages_to_pdf, data, f"Scan {stamp}")
            name = f"scan-{stamp}.pdf"
        elif whole is not None and hasattr(whole, "read") and getattr(whole, "filename", ""):
            pdf, name = await whole.read(), whole.filename
        else:
            return JSONResponse({"ok": False, "fehler": "Nichts gescannt"}, status_code=400)
        stored = await asyncio.to_thread(api.inbox_add, ui.book(), name, pdf)
        res = await asyncio.to_thread(api.bill_draft_create, ui.book(), stored["datei"], art)
    except (BookError, FormatError, ValueError) as exc:
        return JSONResponse({"ok": False, "fehler": str(exc)}, status_code=400)
    draft = res["entwurf"]
    if erfassung.agent_auto(ui.book()) and erfassung.needs_agent(draft):
        _agent_in_background(ui.root, [draft["id"]])
    return JSONResponse({"ok": True, "entwurf": draft["id"], "url": f"/m/belege/{draft['id']}",
                         "meldung": f"{DRAFT_KINDS.get(draft.get('art', ''), 'Beleg')} {draft['id']} eingelesen"})


async def belege(ui: UI, request: Request):
    from .views import unread_inbox
    book = ui.book()
    return render(ui, request, "belege.html", book=book, tab="scannen", drafts=draft_list(book),
                  inbox=unread_inbox(book), lesbar=READABLE)


async def beleg(ui: UI, request: Request):
    from .. import erfassung
    book = ui.book()
    try:
        d = erfassung.draft(book, request.path_params["id"])
    except BookError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    v = erfassung.form_values(d)
    konto = v["konto"]
    st = draft_state(book, d)
    art = d.get("art", "kreditor")
    datei = d.get("datei", "")
    accounts = [{"nr": a.nr, "name": a.name} for a in book.accounts.values()
                if a.aktiv_ and (a.klasse == "aufwand" or (a.klasse == "aktiv" and a.gruppe != "fluessige"))]
    return render(ui, request, "beleg.html", book=book, tab="scannen", d=d, v=v, st=st, art=art,
                  kind=DRAFT_KINDS.get(art, ""), konto=konto,
                  konto_name=book.accounts[konto].name if konto in book.accounts else "",
                  warnings=erfassung.warnings(book, d), accounts=accounts,
                  bild=datei.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")), datei=datei,
                  buchbar=art in ("kreditor", "quittung") and d.get("status") == "bereit" and not st["hold"],
                  desktop=DESKTOP_REVIEW.get(art, "/kreditoren?status=entwurf") + d["id"],
                  hinweise=[h for h in d.get("hinweise") or [] if h != d.get("konflikt")][-3:])


async def beleg_aktion(ui: UI, request: Request):
    from .views import _agent_in_background
    draft_id, aktion = request.path_params["id"], request.path_params["aktion"]
    here = f"/m/belege/{draft_id}"
    f = await request.form()
    if aktion == "buchen":
        try:
            res = await asyncio.to_thread(api.bill_draft_approve, ui.book(), draft_id)
        except (BookError, FormatError, ValueError) as exc:
            return fail(str(exc))
        return done(res.get("meldung", f"{draft_id} gebucht") if isinstance(res, dict) else f"{draft_id} gebucht",
                    "/m/belege")
    if aktion == "konto":
        konto = acct(f.get("konto") or "")
        if not konto:
            return fail("Kein Konto gewählt.")
        return await act(request, api.bill_draft_update, here, ui.book(), draft_id, "Hand", konto=konto)
    if aktion == "art":
        return await act(request, api.bill_draft_update, here, ui.book(), draft_id, "Hand", art=f.get("art") or "")
    if aktion == "agent":
        try:
            await asyncio.to_thread(api.bill_draft_mark, ui.book(), draft_id, "agent", "An den Agenten übergeben")
        except BookError as exc:
            return fail(str(exc))
        _agent_in_background(ui.root, [draft_id])
        return done(f"{draft_id} ist beim Agenten — die Seite aktualisiert sich, sobald er fertig ist", here)
    if aktion == "verwerfen":
        return await act(request, api.bill_draft_discard, "/m/belege", ui.book(), draft_id)
    return fail("Unbekannte Aktion")


async def agent_page(ui: UI, request: Request):
    from .chat import credentials_status
    book = ui.book()
    kunde_nr = request.query_params.get("kunde", "")
    ctx, page, preset = "", "/m/agent", ""
    if kunde_nr:
        c = invoices.customers(book).get(kunde_nr)
        if c:
            ctx = f"Kunde {c.get('firma') or c.get('name')} ({kunde_nr})"
            page = f"/debitoren/kunden/{kunde_nr}"
            preset = f"Notiz zu {c.get('firma') or c.get('name')} ({kunde_nr}): "
    cred = credentials_status(book.settings.get("agent_backend"))
    return render(ui, request, "agent.html", book=book, tab="agent", ctx=ctx, page=page, preset=preset,
                  bereit=cred["ok"], hinweis=cred.get("hinweis", ""), schreiben=can_write(request))


async def ansicht(ui: UI, request: Request):
    """Switch between the phone and the desktop view; the choice is remembered on this device."""
    zu = request.query_params.get("zu", "")
    to = "/m" if zu == "handy" else "/"
    response = RedirectResponse(to, status_code=303)
    if zu in ("handy", "desktop"):
        response.set_cookie(VIEW_COOKIE, zu, max_age=365 * 86400, samesite="lax", httponly=True)
    return response


# ---------- installable app ----------

async def manifest(ui: UI, request: Request):
    firma = ui.book().settings.firma or "aeradex"
    data = {"name": f"{firma} · aeradex", "short_name": firma if len(firma) <= 14 else "aeradex",
            "start_url": "/m", "scope": "/", "display": "standalone", "orientation": "portrait",
            "background_color": "#08090a", "theme_color": "#08090a", "lang": "de-CH",
            "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                      {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


# The service worker only makes the app installable: it caches nothing (no book data on the phone) and lets every
# request through to the network.
SERVICE_WORKER = """self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
"""


async def service_worker(ui: UI, request: Request):
    return Response(SERVICE_WORKER, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


PUBLIC = ("/manifest.webmanifest", "/sw.js")


def routes(ui: UI) -> list[Route]:
    def h(fn):
        async def handler(request):
            return await fn(ui, request)
        handler.__name__ = fn.__name__
        return handler

    return [
        Route("/m", h(heute)),
        Route("/m/kunden", h(kunden)),
        Route("/m/kunden/neu", h(kunde_neu)),
        Route("/m/kunden/neu", h(kunde_speichern), methods=["POST"]),
        Route("/m/kunden/{nr:str}", h(kunde)),
        Route("/m/kunden/{nr:str}/log", h(kunde_log), methods=["POST"]),
        Route("/m/kunden/{nr:str}/log/{index:int}", h(kunde_log_erledigt), methods=["POST"]),
        Route("/m/kunden/{nr:str}/infos", h(kunde_infos), methods=["POST"]),
        Route("/m/kunden/{nr:str}/nummer", h(kunde_nummer), methods=["POST"]),
        Route("/m/inbox/einlesen", h(inbox_einlesen), methods=["POST"]),
        Route("/m/inbox/loeschen", h(inbox_loeschen), methods=["POST"]),
        Route("/m/kunden/{nr:str}", h(kunde_speichern), methods=["POST"]),
        Route("/m/rechnungen", h(rechnungen)),
        Route("/m/rechnungen/{nr:str}", h(rechnung)),
        Route("/m/scannen", h(scannen)),
        Route("/m/scannen", h(scannen_hochladen), methods=["POST"]),
        Route("/m/belege", h(belege)),
        Route("/m/belege/{id:str}", h(beleg)),
        Route("/m/belege/{id:str}/{aktion:str}", h(beleg_aktion), methods=["POST"]),
        Route("/m/agent", h(agent_page)),
        Route("/m/ansicht", h(ansicht)),
        Route("/manifest.webmanifest", h(manifest)),
        Route("/sw.js", h(service_worker)),
    ]

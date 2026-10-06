"""allkvitt web UI: a local Starlette app over the same `api` the CLI and MCP use.

Every write goes through `api` (guard → validate → write → check → git commit),
so the UI can never put the book into a state the CLI would not. Pages are
server-rendered with Jinja; HTMX posts forms and swaps in errors; a small SSE
stream tells open pages when files changed underneath them (an agent, git, an
editor) so they refresh themselves.
"""
from __future__ import annotations

import asyncio
import os
import contextlib
import hashlib
import hmac
import json
import mimetypes
import secrets
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from jinja2 import BaseLoader, ChoiceLoader, Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse,
                                 Response, StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import api
from ..book import MONTHS_DE, Book, BookError
from ..files import FormatError, parse_date
from .auth import token_matches

HERE = Path(__file__).parent
COOKIE = "allkvitt_session"
FLASH = "allkvitt_flash"


# ---------- formatting ----------

def chf(value, blank_zero: bool = False, sign: bool = False) -> str:
    try:
        d = Decimal(str(value if value not in (None, "") else 0))
    except InvalidOperation:
        return str(value)
    if not d:
        return "" if blank_zero else "0.00"
    text = f"{abs(d):,.2f}".replace(",", "'")
    if d < 0:
        return "−" + text
    return ("+" + text) if sign else text


def minus(value) -> str:
    """A deduction shown with its sign: 108.16 → −108.16, but 0 stays 0.00."""
    try:
        return chf(-Decimal(str(value if value not in (None, "") else 0)))
    except InvalidOperation:
        return str(value)


def qty(value) -> str:
    """40.0 → 40, 1.50 → 1.5."""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation:
        return str(value)


def datum(value, fmt: str = "%d.%m.%Y") -> str:
    if not value:
        return ""
    try:
        return parse_date(value).strftime(fmt)
    except (FormatError, ValueError):
        return str(value)


def pct(value, digits: int = 2) -> str:
    try:
        return f"{Decimal(str(value or 0)) * 100:.{digits}f} %".replace(".", ".")
    except InvalidOperation:
        return str(value)


def neg(value) -> bool:
    try:
        return Decimal(str(value or 0)) < 0
    except InvalidOperation:
        return False


class _PluginTemplates(BaseLoader):
    """Templates of plugins as "plugins/<name>/<file>", from the plugin package's templates/ folder."""

    def get_source(self, environment, template):
        parts = template.split("/", 2)
        if len(parts) != 3 or parts[0] != "plugins" or ".." in parts[2]:
            raise TemplateNotFound(template)
        from .. import plugins
        folder = plugins.template_dir(parts[1])
        path = folder / parts[2] if folder else None
        if path is None or not path.is_file():
            raise TemplateNotFound(template)
        mtime = path.stat().st_mtime
        return path.read_text(encoding="utf-8"), str(path), lambda: path.exists() and path.stat().st_mtime == mtime


def make_env() -> Environment:
    env = Environment(loader=ChoiceLoader([FileSystemLoader(HERE / "templates"), _PluginTemplates()]),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters.update(chf=chf, minus=minus, qty=qty, datum=datum, pct=pct, neg=neg, urlq=lambda v: quote(str(v)))
    static = HERE / "static"
    stamp = hashlib.sha1("".join(f"{p.name}{p.stat().st_mtime_ns}" for p in sorted(static.glob("*.*"))).encode()).hexdigest()[:8]
    env.globals.update(MONTHS=MONTHS_DE, today=date.today, asset=stamp)
    return env


# ---------- app ----------

class UI:
    """Holds the book location, the session token and the template env."""

    def __init__(self, root: Path, token: str | None = None, auth_dir: Path | None = None,
                 https: bool = False):
        self.root = Path(root).resolve()
        self.token = token or secrets.token_urlsafe(24)
        self.csrf = hmac.new(self.token.encode(), b"csrf", hashlib.sha256).hexdigest()[:32]
        self.env = make_env()
        self.version = 0          # bumped by the file watcher
        self.chat = None          # set lazily by chat.py
        self.auth_dir = auth_dir  # set = server mode (login, no plugin installs from the browser)
        self.neustart_noetig = False
        # Server mode: users and signed sessions instead of the one-time token.
        self.users = self.sessions = self.throttle = None
        self.https = https
        if auth_dir is not None:
            from .auth import Sessions, Throttle, UserStore
            self.users, self.sessions, self.throttle = UserStore(auth_dir), Sessions(auth_dir), Throttle()

    def book(self) -> Book:
        return Book(self.root)

    # -- rendering --
    def render(self, request: Request, template: str, http_status: int = 200, **ctx) -> HTMLResponse:
        book = ctx.pop("book", None) or self.book()
        flash = None
        raw = request.cookies.get(FLASH)
        if raw:
            try:
                flash = json.loads(raw)
            except ValueError:
                flash = None
        ctx.setdefault("nav", nav_counts(book))
        from .. import mwst
        ctx.setdefault("vat", mwst.config(book))
        ctx.setdefault("vat_codes", mwst.CODES)
        try:
            from .. import plugins
            ctx.setdefault("plugin_pages", [{"url": f"/p/{name}/{pg.slug}", "label": pg.label}
                                            for name, pg in plugins.pages(book)])
        except Exception:
            ctx.setdefault("plugin_pages", [])
        ctx.setdefault("navigation", navigation(request.url.path, ctx["nav"], ctx["plugin_pages"],
                                                ctx["vat"].get("methode", "") if isinstance(ctx["vat"], dict) else "",
                                                str(ctx.get("status") or request.query_params.get("status") or "")))
        html = self.env.get_template(template).render(
            request=request, book=book, settings=book.settings, csrf=getattr(request.state, "csrf", self.csrf),
            flash=flash, path=request.url.path, user=getattr(request.state, "user", None), **ctx)
        response = HTMLResponse(html, status_code=http_status)
        if raw:
            response.delete_cookie(FLASH)
        return response

    def partial(self, template: str, **ctx) -> HTMLResponse:
        return HTMLResponse(self.env.get_template(template).render(csrf=self.csrf, **ctx))


# The top navigation: (key, label, url, badge key, shortcut, [tabs]); a tab is (key, label, url, extra prefixes)
# or (key, label, url, extra prefixes, badge key). A tab url with `?status=…` is a status tab: it is on when the
# page shows that status. Order follows the work: buying, selling, the bank, wages, then the books themselves.
SECTIONS = [
    ("uebersicht", "Übersicht", "/", "todo", "u", []),
    ("einkauf", "Einkauf", "/kreditoren", "einkauf", "k", [
        ("entwurf", "Entwürfe", "/kreditoren?status=entwurf", ("/kreditoren/neu", "/eingang/quittung"), "einkauf"),
        ("offen", "Offen", "/kreditoren?status=offen", ("/kreditoren/rechnung",)),
        ("bezahlt", "Bezahlt", "/kreditoren?status=bezahlt", ()),
        ("alle", "Alle", "/kreditoren?status=alle", ()),
        ("zahlungen", "Zahlungsläufe", "/kreditoren/zahlungen", ("/kreditoren/zahlungslauf",)),
        ("lieferanten", "Lieferanten", "/kreditoren/lieferanten", ()),
        ("offene", "Offene Posten", "/kreditoren/offene-posten", ())]),
    ("verkauf", "Verkauf", "/debitoren", "verkauf", "d", [
        ("entwurf", "Entwürfe", "/debitoren?status=entwurf", ("/debitoren/extern",), "verkauf"),
        ("offen", "Offen", "/debitoren?status=offen", ("/debitoren/rechnung", "/debitoren/neu")),
        ("bezahlt", "Bezahlt", "/debitoren?status=bezahlt", ()),
        ("alle", "Alle", "/debitoren?status=alle", ()),
        ("kunden", "Kunden", "/debitoren/kunden", ()),
        ("mahnungen", "Mahnungen", "/debitoren/mahnungen", ()),
        ("offene", "Offene Posten", "/debitoren/offene-posten", ())]),
    ("bank", "Bank", "/bank", "bank", "b", [
        ("abgleichen", "Abgleichen", "/bank", (), "bank"), ("bewegungen", "Bewegungen", "/bank/bewegungen", ()),
        ("regeln", "Regeln", "/bank/regeln", ()), ("abstimmung", "Abstimmung", "/bank/abstimmung", ())]),
    ("lohn", "Lohn", "/lohn", "entwuerfe", "l", [
        ("lauf", "Lohnlauf", "/lohn", ()),
        ("mitarbeiter", "Mitarbeitende", "/lohn/mitarbeiter", ("/lohn/lohnkonto", "/lohn/lohnausweis"))]),
    ("buchhaltung", "Buchhaltung", "/journal", "vorschlaege", "j", [
        ("journal", "Journal", "/journal", ()), ("vorschlaege", "Vorschläge", "/vorschlaege", (), "vorschlaege"),
        ("kontenplan", "Kontenplan", "/konten", ()), ("saldenliste", "Saldenliste", "/konten/saldenliste", ()),
        ("mwst", "MWST", "/mwst", ()), ("berichte", "Berichte", "/berichte", ()),
        ("budget", "Budget", "/berichte/budget", ()), ("abschluss", "Abschluss", "/abschluss", ())]),
]

def _under(path: str, prefix: str) -> bool:
    return path == prefix or (prefix != "/" and path.startswith(prefix.rstrip("/") + "/"))


def navigation(path: str, counts: dict, plugin_pages: list[dict], vat_method: str = "", status: str = "") -> dict:
    """The two navigation rows: sections (with badge and active flag), and the tabs of the active
    section. `status` is the status the page shows (for status tabs). All enabled plugin pages appear
    in a shared «Plugins» section."""
    sections, active = [], None
    plugin_section = [("plugins", "Plugins", plugin_pages[0]["url"], "", "", [
        (pg["url"], pg["label"], pg["url"], ()) for pg in plugin_pages])] if plugin_pages else []
    for item in [*SECTIONS, *([None, *plugin_section] if plugin_section else [])]:
        if item is None:
            sections.append(None)
            continue
        key, label, url, badge, keyc, tabs = item
        hidden = [t[2] for t in tabs if t[0] == "mwst" and vat_method in ("", "keine")]   # page still belongs here
        tabs = [{"key": t[0], "label": t[1], "url": t[2], "base": t[2].split("?")[0],
                 "status": t[2].split("?status=")[1] if "?status=" in t[2] else None,
                 "prefixes": (t[2].split("?")[0], *t[3]), "badge": counts.get(t[4]) if len(t) > 4 else 0}
                for t in tabs if t[2] not in hidden]
        prefixes = [url, *hidden] + [p for t in tabs for p in t["prefixes"]]
        on = any(_under(path, p) for p in prefixes)
        entry = {"key": key, "label": label, "url": url, "badge": counts.get(badge) if badge else 0,
                 "kuerzel": keyc, "on": on,
                 "tabs": tabs if len(tabs) > 1 or key == "plugins" else []}
        if on and active is None:
            active = entry
        sections.append(entry)
    if active:
        def score(t: dict) -> int:
            # a status tab is on for its own list (by status) and for its extra prefixes (draft review, detail)
            if t["status"] is not None:
                if path == t["base"]:      # a status without a tab of its own (storniert …) → «Alle»
                    return 1000 if t["status"] == status else (1 if t["status"] == "alle" else 0)
                return max((len(p) for p in t["prefixes"][1:] if _under(path, p)), default=0)
            return max((len(p) for p in t["prefixes"] if _under(path, p)), default=0)
        top, best = max(((score(t), t) for t in active["tabs"]), key=lambda x: x[0], default=(0, None))
        # a page below the section without a tab of its own → the first tab
        best = best if top else (active["tabs"][0] if active["tabs"] else None)
        for t in active["tabs"]:
            t["on"] = t is best
    return {"bereiche": sections, "aktiv": active}


def nav_counts(book: Book) -> dict:
    """Badge numbers for the navigation, cheap enough to compute per page."""
    from .. import journal, payroll
    inbox = book.root / "inbox"
    files = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")] if inbox.exists() else []
    proposals = journal.list_proposals(book)
    drafts = [p for p in payroll.payslips(book) if p.get("status") != "abgeschlossen"]
    from .. import check
    from .. import bank
    from .. import erfassung
    errors = sum(1 for i in check.run(book) if i.level == "fehler")
    bank_open = sum(1 for t in bank.transactions(book) if t["Status"] == "offen")
    eingang = list(erfassung.drafts(book).values())
    drafted = {d.get("datei") for d in eingang}
    unread = sum(1 for p in files if f"inbox/{p.name}" not in drafted)
    buying = sum(1 for d in eingang if d.get("art", "kreditor") in ("kreditor", "quittung"))
    selling = sum(1 for d in eingang if d.get("art") == "debitor")
    return {"inbox": unread, "vorschlaege": len(proposals), "entwuerfe": len(drafts), "fehler": errors,
            "bank": bank_open, "einkauf": buying, "verkauf": selling,
            "todo": unread + buying + selling + bank_open + len(proposals) + len(drafts)}


def done(message: str, to: str, kind: str = "ok") -> Response:
    """Answer to a successful HTMX form post: flash + navigate."""
    response = Response(status_code=204, headers={"HX-Redirect": to})
    response.set_cookie(FLASH, json.dumps({"kind": kind, "msg": message}), max_age=30, samesite="strict",
                        httponly=True)
    return response


def fail(message: str) -> HTMLResponse:
    """Answer to a refused form post: the message lands in the form's error box."""
    return HTMLResponse(f'<div class="alert error" role="alert">{_escape(message)}</div>', status_code=200,
                        headers={"HX-Reswap": "innerHTML"})


def _escape(text: str) -> str:
    from markupsafe import escape
    return str(escape(text)).replace("\n", "<br>")


def acct(value: str) -> str:
    """'6500  Büromaterial' (datalist choice) → '6500'."""
    return (value or "").strip().split(" ", 1)[0]


async def act(request: Request, fn, to: str, *args, **kwargs) -> Response:
    """Run one api write, turn its outcome into an HTMX answer."""
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except (BookError, FormatError, ValueError, KeyError, RuntimeError) as exc:
        return fail(str(exc))
    message = result.get("meldung", "Gespeichert") if isinstance(result, dict) else "Gespeichert"
    if callable(to):
        to = to(result)
    return done(message, to)


# ---------- security ----------

class SessionMiddleware(BaseHTTPMiddleware):
    """Local-only access: a random token from the launch URL becomes a cookie;
    every POST must also carry the CSRF token and come from our own origin."""

    def __init__(self, app, ui: UI):
        super().__init__(app)
        self.ui = ui

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/"):
            return await call_next(request)
        if self.ui.users is not None:
            return await self.server_mode(request, call_next)
        token = request.query_params.get("t")
        if token is not None:
            if not token_matches(token, self.ui.token):
                return PlainTextResponse("Ungültiger Zugangslink.", status_code=403)
            rest = "&".join(f"{k}={quote(v)}" for k, v in request.query_params.multi_items() if k != "t")
            response = RedirectResponse(path + (f"?{rest}" if rest else ""), status_code=303)
            response.set_cookie(COOKIE, self.ui.token, httponly=True, samesite="strict")
            return response
        if not token_matches(request.cookies.get(COOKIE, ""), self.ui.token):
            return HTMLResponse(LOCKED_PAGE, status_code=401)
        if request.method == "POST":
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin and origin.split("://", 1)[-1] != host:
                return PlainTextResponse("Fremde Herkunft abgelehnt.", status_code=403)
            sent = request.headers.get("x-csrf", "")
            if not token_matches(sent, self.ui.csrf):
                return PlainTextResponse("CSRF-Token fehlt oder ist falsch.", status_code=403)
        return await call_next(request)


    async def server_mode(self, request: Request, call_next):
        from .. import gitlog
        from .auth import ADMIN_ONLY, SESSION_COOKIE
        ui, path = self.ui, request.url.path
        if path in ("/login", "/healthz"):
            if request.method == "POST" and not self._same_origin(request):
                return PlainTextResponse("Fremde Herkunft abgelehnt.", status_code=403)
            return await call_next(request)
        session = ui.sessions.read(request.cookies.get(SESSION_COOKIE, ""))
        user = ui.users.get(session["u"]) if session else None
        if user is None or ui.users.version(user.name) != session.get("v", 0):
            target = "/login" + (f"?weiter={quote(path)}" if path not in ("/", "/logout") else "")
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": target})
            return RedirectResponse(target, status_code=303)
        request.state.user = user
        request.state.csrf = ui.sessions.csrf(session)
        if request.method == "POST":
            if not self._same_origin(request):
                return PlainTextResponse("Fremde Herkunft abgelehnt.", status_code=403)
            if not token_matches(request.headers.get("x-csrf", ""), request.state.csrf):
                return PlainTextResponse("CSRF-Token fehlt oder ist falsch.", status_code=403)
            if path != "/logout":
                refusal = None
                if not user.can_write:
                    refusal = "Nur Leserecht — Änderungen sind deiner Rolle nicht erlaubt."
                elif any(path == p or path.startswith(p + "/") for p in ADMIN_ONLY) and not user.is_admin:
                    refusal = "Nur Admins dürfen Einstellungen ändern und Perioden sperren."
                if refusal:
                    # HTMX forms show it in their error box; anything else gets a plain 403.
                    return fail(refusal) if request.headers.get("hx-request") else PlainTextResponse(refusal, status_code=403)
        token = gitlog.AUTHOR.set(user.git_author)
        try:
            return await call_next(request)
        finally:
            gitlog.AUTHOR.reset(token)

    @staticmethod
    def _same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return not origin or origin.split("://", 1)[-1] == host


LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>allkvitt</title>
<body style="font-family:system-ui;padding:3rem;max-width:36rem;margin:auto;line-height:1.5">
<h1>allkvitt ist gesperrt</h1><p>Öffne allkvitt über den Link, den <code>allkvitt ui</code> im Terminal ausgibt.
Er enthält einen Zugangsschlüssel, der nur für diese Sitzung gilt.</p></body>"""


# ---------- shared routes ----------

def serve_file(ui: UI):
    async def handler(request: Request):
        rel = request.path_params["path"]
        target = (ui.root / rel).resolve()
        if not target.is_relative_to(ui.root) or ".git" in target.parts or not target.is_file():
            return PlainTextResponse("Nicht gefunden", status_code=404)
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if media.startswith("text/") or target.suffix in (".md", ".yaml", ".yml"):
            media = "text/plain; charset=utf-8"
        return FileResponse(target, media_type=media,
                            headers={"Content-Disposition": f'inline; filename="{quote(target.name)}"'})
    return handler


def events(ui: UI):
    async def handler(request: Request):
        async def stream():
            seen = ui.version
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if ui.version != seen:
                    seen = ui.version
                    yield f"event: changed\ndata: {seen}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return handler


async def watch(ui: UI) -> None:
    """Bump ui.version whenever a file in the book changes (except git internals)."""
    try:
        from watchfiles import awatch
    except ImportError:
        return

    def relevant(_change, path: str) -> bool:
        from ..storage import internal
        try:
            return not internal(Path(path).relative_to(ui.root).as_posix())
        except ValueError:
            return True

    async for _changes in awatch(ui.root, watch_filter=relevant, debounce=400):
        ui.version += 1


def login_routes(ui: UI) -> list[Route]:
    from .auth import SESSION_COOKIE, SESSION_DAYS

    async def login(request: Request):
        weiter = request.query_params.get("weiter", "/")
        if not weiter.startswith("/") or weiter.startswith("//"):
            weiter = "/"
        error = None
        if request.method == "POST":
            form = await request.form()
            name = (form.get("name") or "").strip().lower()
            address = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
            if ui.throttle.blocked(f"u:{name}", f"a:{address}"):
                error = "Zu viele Fehlversuche. Bitte in 5 Minuten erneut versuchen."
            else:
                user = await asyncio.to_thread(ui.users.check, name, form.get("passwort") or "")
                if user:
                    ui.throttle.clear(f"u:{name}", f"a:{address}")
                    response = RedirectResponse(weiter, status_code=303)
                    response.set_cookie(SESSION_COOKIE, ui.sessions.issue(user.name, ui.users.version(user.name)),
                                        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                                        secure=ui.https or request.headers.get("x-forwarded-proto") == "https")
                    return response
                ui.throttle.fail(f"u:{name}", f"a:{address}")
                error = "Benutzername oder Passwort falsch."
        book = ui.book()
        html = ui.env.get_template("login.html").render(firma=book.settings.firma, error=error, weiter=weiter)
        return HTMLResponse(html, status_code=401 if error else 200)

    async def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        if request.headers.get("hx-request"):
            response = Response(status_code=204, headers={"HX-Redirect": "/login"})
            response.delete_cookie(SESSION_COOKIE)
        return response

    async def healthz(request: Request):
        return PlainTextResponse("ok")

    return [Route("/login", login, methods=["GET", "POST"]), Route("/logout", logout, methods=["POST"]),
            Route("/healthz", healthz)]


def create_app(root: Path, token: str | None = None, auth_dir: Path | None = None, https: bool = False) -> Starlette:
    from . import berichte_views, views, chat

    ui = UI(root, token, auth_dir, https)
    routes = views.routes(ui) + berichte_views.routes(ui) + chat.routes(ui) + (login_routes(ui) if auth_dir is not None else []) + [
        Route("/datei/{path:path}", serve_file(ui)),
        Route("/events", events(ui)),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(watch(ui))
        try:
            yield
        finally:
            task.cancel()

    app = Starlette(routes=routes, middleware=[Middleware(SessionMiddleware, ui=ui)], lifespan=lifespan)
    app.state.ui = ui
    return app


def restart_later(ui: "UI", delay: float = 0.8) -> None:
    """Replace this process with a fresh `allkvitt ui` (same token), so newly installed plugins load."""
    import sys
    import threading

    def go():
        os.environ["ALLKVITT_UI_TOKEN"] = ui.token
        os.environ["ALLKVITT_UI_RESTART"] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Timer(delay, go).start()


def run(root: Path, port: int = 5151, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    # After installing a plugin the UI restarts itself (os.execv) and keeps its access token.
    restarted = bool(os.environ.pop("ALLKVITT_UI_RESTART", ""))
    app = create_app(root, os.environ.pop("ALLKVITT_UI_TOKEN", None) or None)
    ui: UI = app.state.ui
    url = f"http://127.0.0.1:{port}/?t={ui.token}"
    print(f"allkvitt läuft für {ui.book().settings.firma}: {url}\n(Beenden mit Ctrl+C)")
    if open_browser and not restarted:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def run_server(root: Path, host: str = "127.0.0.1", port: int = 8080, config: Path | None = None,
               https: bool = False) -> None:
    """Multi-user mode with login (behind a TLS reverse proxy such as Caddy or nginx)."""
    import uvicorn

    from .auth import UserStore, config_dir
    directory = config_dir(config)
    if not UserStore(directory).has_admin():
        raise SystemExit(f"Noch kein Admin in {directory}/users.yaml — zuerst: allkvitt user add NAME --rolle admin")
    app = create_app(root, auth_dir=directory, https=https)
    print(f"allkvitt läuft für {app.state.ui.book().settings.firma} auf http://{host}:{port} (Login, Benutzer in {directory})")
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")

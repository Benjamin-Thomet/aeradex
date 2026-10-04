"""batzen web UI: a local Starlette app over the same `api` the CLI and MCP use.

Every write goes through `api` (guard → validate → write → check → git commit),
so the UI can never put the book into a state the CLI would not. Pages are
server-rendered with Jinja; HTMX posts forms and swaps in errors; a small SSE
stream tells open pages when files changed underneath them (an agent, git, an
editor) so they refresh themselves.
"""
from __future__ import annotations

import asyncio
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

from jinja2 import Environment, FileSystemLoader, select_autoescape
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

HERE = Path(__file__).parent
COOKIE = "batzen_session"
FLASH = "batzen_flash"


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


def make_env() -> Environment:
    env = Environment(loader=FileSystemLoader(HERE / "templates"),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters.update(chf=chf, minus=minus, qty=qty, datum=datum, pct=pct, neg=neg, urlq=lambda v: quote(str(v)))
    static = HERE / "static"
    stamp = hashlib.sha1("".join(f"{p.name}{p.stat().st_mtime_ns}" for p in sorted(static.glob("*.*"))).encode()).hexdigest()[:8]
    env.globals.update(MONTHS=MONTHS_DE, today=date.today, asset=stamp)
    return env


# ---------- app ----------

class UI:
    """Holds the book location, the session token and the template env."""

    def __init__(self, root: Path, token: str | None = None):
        self.root = Path(root).resolve()
        self.token = token or secrets.token_urlsafe(24)
        self.csrf = hmac.new(self.token.encode(), b"csrf", hashlib.sha256).hexdigest()[:32]
        self.env = make_env()
        self.version = 0          # bumped by the file watcher
        self.chat = None          # set lazily by chat.py

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
        html = self.env.get_template(template).render(
            request=request, book=book, settings=book.settings, csrf=self.csrf, flash=flash,
            path=request.url.path, **ctx)
        response = HTMLResponse(html, status_code=http_status)
        if raw:
            response.delete_cookie(FLASH)
        return response

    def partial(self, template: str, **ctx) -> HTMLResponse:
        return HTMLResponse(self.env.get_template(template).render(csrf=self.csrf, **ctx))


def nav_counts(book: Book) -> dict:
    """Badge numbers for the navigation, cheap enough to compute per page."""
    from .. import journal, payroll
    inbox = book.root / "inbox"
    files = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")] if inbox.exists() else []
    proposals = journal.list_proposals(book)
    drafts = [p for p in payroll.payslips(book) if p.get("status") != "abgeschlossen"]
    from .. import check
    errors = sum(1 for i in check.run(book) if i.level == "fehler")
    return {"inbox": len(files), "vorschlaege": len(proposals), "entwuerfe": len(drafts), "fehler": errors,
            "pruefen": len(files) + len(proposals) + len(drafts)}


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
        token = request.query_params.get("t")
        if token is not None:
            if not hmac.compare_digest(token, self.ui.token):
                return PlainTextResponse("Ungültiger Zugangslink.", status_code=403)
            rest = "&".join(f"{k}={quote(v)}" for k, v in request.query_params.multi_items() if k != "t")
            response = RedirectResponse(path + (f"?{rest}" if rest else ""), status_code=303)
            response.set_cookie(COOKIE, self.ui.token, httponly=True, samesite="strict")
            return response
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), self.ui.token):
            return HTMLResponse(LOCKED_PAGE, status_code=401)
        if request.method == "POST":
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin and origin.split("://", 1)[-1] != host:
                return PlainTextResponse("Fremde Herkunft abgelehnt.", status_code=403)
            sent = request.headers.get("x-csrf", "")
            if not hmac.compare_digest(sent, self.ui.csrf):
                return PlainTextResponse("CSRF-Token fehlt oder ist falsch.", status_code=403)
        return await call_next(request)


LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>batzen</title>
<body style="font-family:system-ui;padding:3rem;max-width:36rem;margin:auto;line-height:1.5">
<h1>batzen ist gesperrt</h1><p>Öffne batzen über den Link, den <code>batzen ui</code> im Terminal ausgibt.
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
        parts = Path(path).parts
        return ".git" not in parts and not path.endswith("write.lock") and "__pycache__" not in parts

    async for _changes in awatch(ui.root, watch_filter=relevant, debounce=400):
        ui.version += 1


def create_app(root: Path, token: str | None = None) -> Starlette:
    from . import views, chat

    ui = UI(root, token)
    routes = views.routes(ui) + chat.routes(ui) + [
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


def run(root: Path, port: int = 5151, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    app = create_app(root)
    ui: UI = app.state.ui
    url = f"http://127.0.0.1:{port}/?t={ui.token}"
    print(f"batzen läuft für {ui.book().settings.firma}: {url}\n(Beenden mit Ctrl+C)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

"""The portal: a phone app for employees (static PWA + JSON) and the sync API for the office's book.

Everything lives under /app so Caddy can put it next to the company's website.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from . import auth, db

PREFIX = "/app"
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("aeradex_team_portal")
KENNUNG = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
BRANDING_FILES = ("logo.png", "icon-192.png", "icon-512.png", "branding.css")

ANTRAG = ("Ferien", "Kompensation", "Unbezahlt", "Weiterbildung")
DIREKT = ("Krank", "Unfall")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PERSON = re.compile(r"^[MX]\d{2,4}$")
DOC_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}$")


@dataclass
class Config:
    data: Path
    secret: bytes
    sync_token_sha256: str
    secure_cookie: bool = True
    # One word that names this installation in the protocol: cookie, CSRF header, the phone's storage, the salts of
    # the login key and of the documents. Changing it logs everyone out and makes stored documents unreadable.
    kennung: str = "aeradex"
    titel: str = "Team"                 # name of the app on the phone, e.g. «Team · Muster AG»
    branding: Path | None = None        # folder with logo.png, icon-192.png, icon-512.png, branding.css (optional)
    farbe: str = "#1b1f24"              # theme colour (status bar, header)
    kurz: str = ""                      # name under the icon on the home screen (default: start of the title)

    def __post_init__(self):
        if not KENNUNG.match(self.kennung):
            raise SystemExit("Kennung: 2–31 Kleinbuchstaben, Ziffern oder «-», beginnt mit einem Buchstaben")

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        data = Path(env("AERADEX_PORTAL_DATA", "/var/lib/aeradex-team-portal"))
        secret = env("AERADEX_PORTAL_SECRET", "")
        if len(secret) < 32:
            raise SystemExit("AERADEX_PORTAL_SECRET fehlt (mindestens 32 Zeichen)")
        branding = env("AERADEX_PORTAL_BRANDING", "").strip()
        return cls(data, secret.encode(), env("AERADEX_PORTAL_SYNC_TOKEN_SHA256", "").strip().lower(),
                   env("AERADEX_PORTAL_INSECURE_COOKIE") != "1", kennung=env("AERADEX_PORTAL_KENNUNG", "aeradex"),
                   titel=env("AERADEX_PORTAL_TITEL", "Team"), branding=Path(branding) if branding else None,
                   farbe=env("AERADEX_PORTAL_FARBE", "#1b1f24"), kurz=env("AERADEX_PORTAL_KURZ", "").strip())

    @property
    def cookie(self) -> str:
        return self.kennung.replace("-", "_") + "_s"

    @property
    def header(self) -> str:
        return "x-" + self.kennung

    def asset(self, name: str) -> Path | None:
        """A branding file if the installation has one, else the neutral one shipped with the portal."""
        if self.branding and name in BRANDING_FILES and (self.branding / name).is_file():
            return self.branding / name
        return STATIC / name if (STATIC / name).is_file() else None


class Problem(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "fehler": message}, status_code=status)


# ---------- validation of what a phone sends ----------

def _day(value, name: str) -> str:
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        raise Problem(f"{name}: Datum fehlt") from None
    today = date.today()
    if not today - timedelta(days=400) <= d <= today + timedelta(days=400):
        raise Problem(f"{name}: Datum ausserhalb des erlaubten Bereichs")
    return d.isoformat()


def _number(value, name: str, low: Decimal, high: Decimal) -> str:
    try:
        n = Decimal(str(value).replace(",", ".").strip())
    except InvalidOperation:
        raise Problem(f"{name}: keine Zahl") from None
    if not low < n <= high:
        raise Problem(f"{name}: zwischen {low} und {high}")
    return f"{n.normalize():f}"


def _text(value, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _object_check(out: dict, shared: dict) -> None:
    """The book's rule (projekt_pflicht): a customer with open objects needs one of them on every entry."""
    if shared.get("projekt_pflicht") and out["kunde"] and not out["projekt"]:
        if any(p.get("kunde_nr") == out["kunde"] for p in shared.get("projekte") or []):
            raise Problem(f"Zuerst das {shared.get('projekt_wort') or 'Projekt'} wählen")


def clean_entry(kind: str, data: dict, shared: dict | None = None) -> dict:
    shared = shared or {}
    if kind == "zeit":
        out = {"datum": _day(data.get("datum"), "Datum"),
               "stunden": _number(data.get("stunden"), "Stunden", Decimal(0), Decimal(24)),
               "kunde": _text(data.get("kunde"), 10).upper(), "projekt": _text(data.get("projekt"), 10).upper(),
               "leistung": _text(data.get("leistung"), 10).upper(),
               "kategorie": _text(data.get("kategorie"), 40), "text": _text(data.get("text"))}
        billable = bool(out["kunde"] or out["projekt"])
        if not billable and not out["kategorie"]:
            raise Problem("Kunde oder interne Kategorie wählen")
        if billable:
            out["kategorie"] = ""
            _object_check(out, shared)
            # with the days on the invoice one person describes the day for everyone on the job
            if not (out["text"] or out["leistung"] or (shared.get("rechnung_tage") and out["projekt"])):
                raise Problem("Was wurde gemacht? Text oder Leistungsart angeben")
        return out
    if kind == "material":
        out = {"datum": _day(data.get("datum"), "Datum"), "kunde": _text(data.get("kunde"), 10).upper(),
               "projekt": _text(data.get("projekt"), 10).upper(), "produkt": _text(data.get("produkt"), 10).upper(),
               "menge": _number(data.get("menge"), "Menge", Decimal(0), Decimal(100000)), "text": _text(data.get("text"))}
        if not (out["kunde"] or out["projekt"]) or not out["produkt"]:
            raise Problem("Material braucht Kunde und Produkt")
        _object_check(out, shared)
        if out["produkt"] == "TEXT" and not out["text"]:          # «Anderes»: the text says what it was
            raise Problem("Beschreiben, was verwendet wurde")
        return out
    if kind == "abwesenheit":
        art = _text(data.get("art"), 40)
        if art not in ANTRAG + DIREKT:
            raise Problem("Art der Abwesenheit wählen")
        von = _day(data.get("von"), "Von")
        bis = _day(data.get("bis") or von, "Bis")
        if bis < von:
            raise Problem("«Bis» liegt vor «Von»")
        return {"art": art, "von": von, "bis": bis, "halb": bool(data.get("halb")), "notiz": _text(data.get("notiz"))}
    raise Problem("Unbekannte Art")


# ---------- app ----------

def create_app(config: Config | None = None) -> Starlette:
    cfg = config or Config.from_env()
    db_path = cfg.data / "portal.db"
    throttle = auth.Throttle()

    def con():
        return db.connect(db_path)

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        fwd = request.headers.get("x-forwarded-for", "")
        if peer in ("127.0.0.1", "::1") and fwd:
            return fwd.split(",")[-1].strip()
        return peer

    def me(request: Request, c) -> dict:
        s = auth.read_session(cfg.secret, request.cookies.get(cfg.cookie))
        acc = db.account(c, s["p"]) if s else None
        if not acc or not acc["aktiv"] or acc["session_gen"] != s["g"] or not acc["pw_hash"]:
            raise Problem("Nicht angemeldet", 401)
        return acc

    def booked(c, person: str, uuid: str) -> dict | None:
        """What the office's last snapshot says about an app entry that is already in the book."""
        mine = db.snapshot(c, person)
        for group in ("eintraege", "abwesenheiten", "antraege"):
            for item in mine.get(group) or []:
                if item.get("uuid") == uuid:
                    return item
        return None

    def guard_write(request: Request) -> None:
        # a custom header cannot be sent cross-site without a CORS preflight, which we never allow
        if request.headers.get(cfg.header) != "1":
            raise Problem("Ungültige Anfrage", 403)

    def with_cookie(resp: Response, person: str, gen: int) -> Response:
        resp.set_cookie(cfg.cookie, auth.sign_session(cfg.secret, person, gen), max_age=auth.SESSION_DAYS * 86400,
                        path=PREFIX, httponly=True, secure=cfg.secure_cookie, samesite="strict")
        return resp

    def handler(fn):
        async def run(request: Request):
            try:
                return await fn(request)
            except Problem as exc:
                return _fail(str(exc), exc.status)
            except (ValueError, KeyError, TypeError) as exc:
                return _fail(str(exc) or "Ungültige Anfrage")
            except PermissionError as exc:
                return _fail(str(exc), 403)
        return run

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise Problem("Ungültige Anfrage") from None
        if not isinstance(data, dict):
            raise Problem("Ungültige Anfrage")
        return data

    # ----- pages -----

    logo = bool(cfg.branding and (cfg.branding / "logo.png").is_file())
    shell = ["/app/", "/app/static/app.js", "/app/static/app.css", "/app/static/icon-192.png",
             "/app/static/branding.css", "/app/manifest.webmanifest"] + (["/app/static/logo.png"] if logo else [])
    if cfg.branding and cfg.branding.is_dir():   # fonts and other files branding.css refers to work offline too
        shell += sorted(f"/app/branding/{p.name}" for p in cfg.branding.iterdir()
                        if p.is_file() and p.suffix in (".woff2", ".png", ".svg") and p.name not in BRANDING_FILES)
    titel = html.escape(cfg.titel)
    fill = {"__KENNUNG__": cfg.kennung, "__TITEL__": titel, "__FARBE__": html.escape(cfg.farbe),
            "__LOGO__": "1" if logo else "",
            "__KOPF__": f'<img src="/app/static/logo.png" alt="{titel}" height="30">' if logo
                        else f'<strong class="titel">{titel}</strong>'}

    def filled(name: str) -> str:
        text = (STATIC / name).read_text(encoding="utf-8")
        for key, value in fill.items():
            text = text.replace(key, value)
        return text

    async def index(request: Request):
        return Response(filled("index.html"), media_type="text/html", headers={"cache-control": "no-cache"})

    async def service_worker(request: Request):
        text = filled("sw.js").replace("__FILES__", json.dumps(shell))
        return Response(text, media_type="text/javascript",
                        headers={"cache-control": "no-cache", "service-worker-allowed": PREFIX + "/"})

    async def manifest(request: Request):
        return JSONResponse({
            "name": cfg.titel, "short_name": (cfg.kurz or cfg.titel.split(" · ")[0])[:12], "start_url": "/app/#/erfassen",
            "scope": "/app/", "display": "standalone", "background_color": cfg.farbe, "theme_color": cfg.farbe, "lang": "de-CH",
            "icons": [{"src": "/app/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/app/static/icon-512.png", "sizes": "512x512", "type": "image/png",
                       "purpose": "any maskable"}]}, media_type="application/manifest+json")

    async def static(request: Request):
        name = request.path_params["name"]
        path = cfg.asset(name) if "/" not in name else None
        if not path:
            return Response("Nicht gefunden", status_code=404, media_type="text/plain")
        return FileResponse(path)

    async def branding(request: Request):
        name = request.path_params["name"]
        if not cfg.branding or "/" in name or name.startswith(".") or not (cfg.branding / name).is_file():
            return Response("Nicht gefunden", status_code=404, media_type="text/plain")
        return FileResponse(cfg.branding / name)

    async def robots(request: Request):
        return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

    # ----- employees -----

    @handler
    async def login(request: Request):
        guard_write(request)
        data = await body(request)
        benutzer = _text(data.get("benutzer"), 40).lower()
        ip = client_ip(request)
        if throttle.locked(f"u:{benutzer}", f"ip:{ip}"):
            log.warning("login locked user=%s ip=%s", benutzer, ip)
            raise Problem("Zu viele Versuche. Bitte in 5 Minuten nochmals.", 429)
        c = con()
        acc = db.account(c, benutzer=benutzer)
        if not auth.verify_secret(str(data.get("auth") or ""), acc and acc["pw_hash"]) or not acc["aktiv"]:
            throttle.fail(f"u:{benutzer}", f"ip:{ip}")
            log.warning("login failed user=%s ip=%s", benutzer, ip)
            raise Problem("Benutzername oder Passwort falsch", 401)
        throttle.clear(f"u:{benutzer}")
        return with_cookie(JSONResponse({"ok": True}), acc["person"], acc["session_gen"])

    @handler
    async def logout(request: Request):
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(cfg.cookie, path=PREFIX)
        return resp

    @handler
    async def activate(request: Request):
        guard_write(request)
        data = await body(request)
        benutzer = _text(data.get("benutzer"), 40).lower()
        ip = client_ip(request)
        if throttle.locked(f"a:{benutzer}", f"ip:{ip}"):
            raise Problem("Zu viele Versuche. Bitte in 5 Minuten nochmals.", 429)
        c = con()
        acc = db.account(c, benutzer=benutzer)
        code = auth.sha256(auth.normalize_code(data.get("code") or ""))
        valid = (acc and acc["aktiv"] and acc["invite_sha256"] and acc["invite_sha256"] == code
                 and (acc["invite_expires"] or "") >= date.today().isoformat())
        if not valid:
            throttle.fail(f"a:{benutzer}", f"ip:{ip}")
            log.warning("activation failed user=%s ip=%s", benutzer, ip)
            raise Problem("Code ungültig oder abgelaufen. Bitte im Büro einen neuen verlangen.", 401)
        secret = str(data.get("auth") or "")
        wrapped = data.get("wrapped") or {}
        pubkey = str(data.get("pubkey") or "")
        if len(secret) != 64 or not pubkey or not all(k in wrapped for k in ("salt", "iv", "ct", "iter")):
            raise Problem("Aktivierung unvollständig")
        gen = db.activate(c, acc["person"], auth.hash_secret(secret), pubkey, wrapped)
        throttle.clear(f"a:{benutzer}")
        return with_cookie(JSONResponse({"ok": True}), acc["person"], gen)

    @handler
    async def whoami(request: Request):
        c = con()
        acc = me(request, c)
        since = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
        return JSONResponse({
            "ok": True, "person": acc["person"], "name": acc["name"], "benutzer": acc["benutzer"],
            "key_version": acc["key_version"], "wrapped": json.loads(acc["wrapped_key"] or "{}"),
            "shared": db.snapshot(c, "_shared"), "mein": db.snapshot(c, acc["person"]),
            "eintraege": db.entries_of(c, acc["person"], since), "letzter_abgleich": db.last_sync(c)},
            headers={"cache-control": "no-store"})

    @handler
    async def put_entry(request: Request):
        guard_write(request)
        c = con()
        acc = me(request, c)
        data = await body(request)
        uuid = str(data.get("uuid") or "").lower()
        if not UUID.match(uuid):
            raise Problem("Ungültige ID")
        kind = str(data.get("kind") or "")
        entry = db.put_entry(c, acc["person"], uuid, kind, clean_entry(kind, data.get("data") or {}, db.snapshot(c, "_shared")),
                             booked(c, acc["person"], uuid))
        return JSONResponse({"ok": True, "eintrag": entry})

    @handler
    async def remove_entry(request: Request):
        guard_write(request)
        c = con()
        acc = me(request, c)
        uuid = request.path_params["uuid"].lower()
        db.delete_entry(c, acc["person"], uuid, booked(c, acc["person"], uuid))
        return JSONResponse({"ok": True})

    @handler
    async def password(request: Request):
        guard_write(request)
        c = con()
        acc = me(request, c)
        data = await body(request)
        if not auth.verify_secret(str(data.get("alt") or ""), acc["pw_hash"]):
            raise Problem("Bisheriges Passwort falsch", 401)
        neu, wrapped = str(data.get("neu") or ""), data.get("wrapped") or {}
        if len(neu) != 64 or not all(k in wrapped for k in ("salt", "iv", "ct", "iter")):
            raise Problem("Unvollständig")
        gen = db.change_password(c, acc["person"], auth.hash_secret(neu), wrapped)
        return with_cookie(JSONResponse({"ok": True}), acc["person"], gen)

    @handler
    async def my_docs(request: Request):
        c = con()
        acc = me(request, c)
        return JSONResponse({"ok": True, "docs": db.docs_of(c, acc["person"])})

    @handler
    async def my_doc(request: Request):
        c = con()
        acc = me(request, c)
        found = db.doc(c, acc["person"], request.path_params["doc_id"])
        if not found:
            raise Problem("Nicht gefunden", 404)
        return JSONResponse({"ok": True, "doc": {k: found[k] for k in ("doc_id", "title", "kind", "period",
                                                                        "key_version", "envelope")}},
                            headers={"cache-control": "no-store"})

    # ----- sync (the book) -----

    def sync_guard(request: Request) -> None:
        if not auth.bearer_ok(request.headers.get("authorization"), cfg.sync_token_sha256):
            log.warning("sync auth failed ip=%s", client_ip(request))
            raise Problem("Nicht berechtigt", 401)

    @handler
    async def sync_pending(request: Request):
        sync_guard(request)
        c = con()
        rows = db.claim_pending(c)
        db.touch_sync(c)
        return JSONResponse({"entries": [{k: r[k] for k in ("uuid", "person", "kind", "version", "data", "created")}
                                         for r in rows],
                             "keys": db.public_keys(c), "server_time": db.stamp()})

    @handler
    async def sync_status(request: Request):
        sync_guard(request)
        return JSONResponse({"offen": db.waiting(con())})

    @handler
    async def sync_ack(request: Request):
        sync_guard(request)
        data = await body(request)
        c = con()
        n = db.acknowledge(c, list(data.get("results") or []))
        db.purge(c)
        return JSONResponse({"ok": True, "bestaetigt": n})

    @handler
    async def sync_snapshot(request: Request):
        sync_guard(request)
        data = await body(request)
        db.put_snapshot(con(), data)
        return JSONResponse({"ok": True})

    @handler
    async def sync_accounts(request: Request):
        sync_guard(request)
        data = await body(request)
        for a in data.get("accounts") or []:
            if not PERSON.match(str(a.get("person"))) or not re.match(r"^[a-z0-9._-]{2,40}$", str(a.get("benutzer"))):
                raise Problem(f"Ungültiges Konto {a.get('person')}")
        return JSONResponse({"ok": True, **db.sync_accounts(con(), data.get("accounts") or [],
                                                            data.get("invites") or [])})

    @handler
    async def sync_docs(request: Request):
        sync_guard(request)
        # the book encrypts with the same Kennung as the phones derive their keys with
        return JSONResponse({"docs": db.doc_index(con()), "kennung": cfg.kennung})

    @handler
    async def sync_doc(request: Request):
        sync_guard(request)
        person, doc_id = request.path_params["person"], request.path_params["doc_id"]
        if not PERSON.match(person) or not DOC_ID.match(doc_id):
            raise Problem("Ungültige Dokument-ID")
        c = con()
        if request.method == "DELETE":
            db.delete_doc(c, person, doc_id)
        else:
            db.put_doc(c, person, doc_id, await body(request))
        return JSONResponse({"ok": True})

    api = [
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/aktivieren", activate, methods=["POST"]),
        Route("/api/ich", whoami),
        Route("/api/eintraege", put_entry, methods=["POST"]),
        Route("/api/eintraege/{uuid}", remove_entry, methods=["DELETE"]),
        Route("/api/passwort", password, methods=["POST"]),
        Route("/api/dokumente", my_docs),
        Route("/api/dokumente/{doc_id}", my_doc),
        Route("/api/sync/pending", sync_pending),
        Route("/api/sync/status", sync_status),
        Route("/api/sync/ack", sync_ack, methods=["POST"]),
        Route("/api/sync/snapshot", sync_snapshot, methods=["PUT"]),
        Route("/api/sync/accounts", sync_accounts, methods=["PUT"]),
        Route("/api/sync/docs", sync_docs),
        Route("/api/sync/docs/{person}/{doc_id}", sync_doc, methods=["PUT", "DELETE"]),
    ]
    routes = [
        Mount(PREFIX, routes=[
            Route("/", index), Route("/sw.js", service_worker), Route("/manifest.webmanifest", manifest),
            Route("/robots.txt", robots), *api,
            Route("/static/{name}", static), Route("/branding/{name}", branding),
        ]),
        Route(PREFIX, lambda r: Response(status_code=308, headers={"location": PREFIX + "/"})),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])


HEADERS = [
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                                 b"script-src 'self'; connect-src 'self'; frame-src 'self' blob:; object-src 'self' blob:; "
                                 b"base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
]


class SecurityHeaders:
    """Every answer: not for search engines, not framable, scripts only from here."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(k, v) for k, v in HEADERS if k not in have]
            await send(message)
        return await self.app(scope, receive, wrapped)

"""Users in the browser (server mode): own account, the user list, invitations and password links.

Users live outside the book (`web/auth.py`), so these pages write `<config>/` files, not the book, and keep their
own log (`UserLog`). Without an e-mail service an invitation or a password link is shown once to the person who
made it, to pass on by hand.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from markupsafe import escape
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .app import UI, done, fail
from .auth import ROLE_LABEL, ROLES, SESSION_COOKIE, SESSION_DAYS, check_name, may_grant


def _me(request: Request):
    return getattr(request.state, "user", None)


def public_base(ui: UI, request: Request) -> str:
    """The address people open from outside (behind a proxy: the forwarded host and scheme)."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or ("https" if ui.https else request.url.scheme)
    return f"{scheme}://{host.split(',')[0].strip()}"


def _set_session(ui: UI, request: Request, response: Response, name: str) -> Response:
    response.set_cookie(SESSION_COOKIE, ui.sessions.issue(name, ui.users.version(name)), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax",
                        secure=ui.https or request.headers.get("x-forwarded-proto") == "https")
    return response


def _until(entry: dict) -> str:
    return datetime.fromtimestamp(float(entry.get("bis", 0))).strftime("%d.%m.%Y")


def _link_box(titel: str, url: str, entry: dict, text: str) -> HTMLResponse:
    """The freshly made link, shown once, with a copy button."""
    return HTMLResponse(
        f'<div class="alert ok linkbox"><strong>{escape(titel)}</strong>'
        f'<p class="small">{escape(text)} Gültig bis {_until(entry)}, nur einmal verwendbar. Der Link wird nicht '
        f'nochmals angezeigt.</p>'
        f'<div class="actions"><input class="input" readonly value="{escape(url)}" onclick="this.select()" '
        f'aria-label="Link"><button class="btn small" type="button" '
        f'onclick="navigator.clipboard.writeText(this.previousElementSibling.value);this.textContent=\'Kopiert\'">'
        f'Kopieren</button></div></div>')


def routes(ui: UI) -> list[Route]:
    def server_only(request: Request):
        if ui.users is None:
            return ui.render(request, "benutzer.html", lokal=True)
        return None

    async def konto(request: Request):
        return server_only(request) or ui.render(request, "konto.html", ich=ui.users.get(_me(request).name))

    async def konto_post(request: Request):
        me, f = _me(request), await request.form()
        if f.get("aktion") == "anzeige":
            try:
                await asyncio.to_thread(ui.users.set_display, me.name, f.get("anzeige", ""))
            except ValueError as exc:
                return fail(str(exc))
            ui.userlog.add(me.name, "anzeigename", benutzer=me.name)
            return done("Anzeigename gespeichert — gilt für neue Buchungen", "/konto")
        if not await asyncio.to_thread(ui.users.check, me.name, f.get("aktuell") or ""):
            return fail("Das aktuelle Passwort stimmt nicht.")
        neu = f.get("neu") or ""
        if neu != (f.get("neu2") or ""):
            return fail("Die neuen Passwörter sind nicht gleich.")
        try:
            await asyncio.to_thread(ui.users.set_password, me.name, neu)
        except ValueError as exc:
            return fail(str(exc))
        ui.userlog.add(me.name, "passwort", benutzer=me.name)
        return _set_session(ui, request, done("Passwort geändert — andere Geräte sind abgemeldet", "/konto"), me.name)

    async def benutzer(request: Request):
        if ui.users is None:
            return ui.render(request, "benutzer.html", lokal=True)
        me = _me(request)
        return ui.render(request, "benutzer.html", lokal=False, benutzer=ui.users.list(), ich=me,
                         rollen=[(r, ROLE_LABEL[r]) for r in ROLES], einladbar=[(r, ROLE_LABEL[r]) for r in ROLES
                                                                              if may_grant(me.rolle, r)],
                         offen=[dict(e, bis_text=_until(e)) for e in ui.invites.list()],
                         protokoll=ui.userlog.recent(50), namen={u.name: u.anzeige for u in ui.users.list()})

    async def einladen(request: Request):
        me, f = _me(request), await request.form()
        rolle = f.get("rolle") or "lesen"
        if not may_grant(me.rolle, rolle):
            return fail(f"Du kannst höchstens mit der Rolle «{ROLE_LABEL[me.rolle]}» einladen.")
        token, entry = await asyncio.to_thread(ui.invites.create, me.name, rolle, f.get("notiz", ""))
        ui.userlog.add(me.name, "eingeladen", rolle=rolle, notiz=entry["notiz"])
        url = f"{public_base(ui, request)}/einladung/{token}"
        return _link_box(f"Einladung als {ROLE_LABEL[rolle]}{' für ' + entry['notiz'] if entry['notiz'] else ''}", url,
                         entry, "Diesen Link der Person schicken (WhatsApp, SMS, persönlich). Damit wählt sie "
                                "Benutzername und Passwort.")

    async def zurueckziehen(request: Request):
        me, f = _me(request), await request.form()
        entry = next((e for e in ui.invites.list() if e["id"] == f.get("id")), None)
        if entry is None:
            return fail("Link nicht mehr offen.")
        if entry.get("von") != me.name and not me.is_admin:
            return fail("Nur wer den Link erstellt hat oder ein Admin kann ihn zurückziehen.")
        ui.invites.revoke(entry["id"])
        ui.userlog.add(me.name, "zurueckgezogen", art=entry.get("art"), rolle=entry.get("rolle", ""),
                       benutzer=entry.get("benutzer", ""))
        return done("Link zurückgezogen", "/benutzer")

    async def rolle(request: Request):
        me, f = _me(request), await request.form()
        name, neu = f.get("name", ""), f.get("rolle", "")
        if name == me.name:
            return fail("Die eigene Rolle ändert ein anderer Admin.")
        try:
            await asyncio.to_thread(ui.users.set_role, name, neu)
        except ValueError as exc:
            return fail(str(exc))
        ui.userlog.add(me.name, "rolle", benutzer=name, rolle=neu)
        return done(f"{name}: {ROLE_LABEL[neu]} — gilt ab der nächsten Anmeldung", "/benutzer")

    async def entfernen(request: Request):
        me, f = _me(request), await request.form()
        name = f.get("name", "")
        if name == me.name:
            return fail("Sich selbst kann man nicht entfernen.")
        try:
            await asyncio.to_thread(ui.users.remove, name)
        except ValueError as exc:
            return fail(str(exc))
        ui.userlog.add(me.name, "entfernt", benutzer=name)
        return done(f"{name} entfernt — die Buchungen bleiben mit dem Namen im Verlauf", "/benutzer")

    async def passwort_link(request: Request):
        me, f = _me(request), await request.form()
        name = f.get("name", "")
        if ui.users.get(name) is None:
            return fail(f"Benutzer {name} unbekannt.")
        token, entry = await asyncio.to_thread(ui.invites.create_reset, me.name, name)
        ui.userlog.add(me.name, "passwort-link", benutzer=name)
        return _link_box(f"Passwort-Link für {name}", f"{public_base(ui, request)}/einladung/{token}", entry,
                         "Diesen Link der Person schicken; damit setzt sie ein neues Passwort. Das alte gilt, bis sie es tut.")

    async def einladung(request: Request):
        token = request.path_params["token"]
        address = request.client.host if request.client else ""
        entry = ui.invites.find(token)
        book = ui.book()
        ctx = {"firma": book.settings.firma, "entry": entry, "error": None, "werte": {},
               "rolle_text": ROLE_LABEL.get((entry or {}).get("rolle", ""), ""),
               "von": ui.users.get((entry or {}).get("von", "")) if entry else None}
        if entry is None:
            return HTMLResponse(ui.env.get_template("einladung.html").render(**ctx), status_code=404)
        if request.method == "POST":
            f = await request.form()
            ctx["werte"] = {"name": (f.get("name") or "").strip().lower(), "anzeige": (f.get("anzeige") or "").strip()}
            if ui.throttle.blocked(f"e:{address}"):
                ctx["error"] = "Zu viele Versuche. Bitte in 5 Minuten erneut versuchen."
            elif (f.get("passwort") or "") != (f.get("passwort2") or ""):
                ctx["error"] = "Die Passwörter sind nicht gleich."
            else:
                try:
                    if entry["art"] == "reset":
                        name = entry["benutzer"]
                        await asyncio.to_thread(ui.users.set_password, name, f.get("passwort") or "")
                        ui.userlog.add(name, "passwort-zurueckgesetzt", benutzer=name, link_von=entry.get("von", ""))
                    else:
                        name = check_name(ctx["werte"]["name"])
                        await asyncio.to_thread(ui.users.add, name, f.get("passwort") or "", entry["rolle"],
                                                ctx["werte"]["anzeige"], entry.get("von", ""))
                        ui.userlog.add(name, "angenommen", benutzer=name, rolle=entry["rolle"], von=entry.get("von", ""))
                except ValueError as exc:
                    ui.throttle.fail(f"e:{address}")
                    ctx["error"] = str(exc)
                else:
                    ui.invites.consume(token)
                    await asyncio.to_thread(ui.users.touch_login, name)
                    return _set_session(ui, request, RedirectResponse("/", status_code=303), name)
        if entry["art"] != "reset" and not ctx["werte"]:
            ctx["werte"] = {"name": _suggest(entry.get("notiz", "")), "anzeige": entry.get("notiz", "")}
        return HTMLResponse(ui.env.get_template("einladung.html").render(**ctx), status_code=400 if ctx["error"] else 200)

    def server(fn):
        async def wrapped(request: Request):
            if ui.users is None:
                return fail("Nur mit Login (aeradex serve).")
            return await fn(request)
        return wrapped

    return [
        Route("/konto", konto), Route("/konto", server(konto_post), methods=["POST"]),
        Route("/benutzer", benutzer), Route("/benutzer/einladen", server(einladen), methods=["POST"]),
        Route("/benutzer/zurueckziehen", server(zurueckziehen), methods=["POST"]),
        Route("/benutzer/rolle", server(rolle), methods=["POST"]),
        Route("/benutzer/entfernen", server(entfernen), methods=["POST"]),
        Route("/benutzer/passwort-link", server(passwort_link), methods=["POST"]),
    ] + ([Route("/einladung/{token:str}", einladung, methods=["GET", "POST"])] if ui.users is not None else [])


def _suggest(notiz: str) -> str:
    """'Lea Muster' → 'lea' (first word, letters only)."""
    word = (notiz or "").strip().split(" ")[0].lower()
    return "".join(ch for ch in word if ch.isalnum() or ch in ".-_")[:40]


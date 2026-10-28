"""The company list of the local UI (`/firmen`): open, create, add and import books, and switch between them.

Local mode only (`aeradex ui`); `aeradex serve` has one book and does not mount these routes. The list lives
outside the books (`firmen.py`), so nothing here writes to a book except creating or importing a new one.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from .. import api, firmen
from ..book import BookError
from .app import FLASH, UI, done, fail

RECHTSFORMEN = ("GmbH", "AG", "Einzelfirma", "Verein", "Genossenschaft")


def routes(ui: UI) -> list[Route]:
    async def liste(request: Request):
        flash = None
        raw = request.cookies.get(FLASH)
        if raw:
            try:
                flash = json.loads(raw)
            except ValueError:
                flash = None
        aktuell = None
        if ui.root is not None:
            try:
                aktuell = ui.book().settings.firma
            except BookError:
                aktuell = None
        home = str(Path.home())

        def kurz(path: str) -> str:          # paths as people write them: ~/aeradex/…
            return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
        liste = [f | {"anzeige": kurz(f["ordner"])} for f in firmen.liste()]
        html = ui.env.get_template("firmen.html").render(
            firmen=liste, root=str(ui.root) if ui.root else "", aktuell=aktuell, csrf=ui.csrf,
            flash=flash, rechtsformen=RECHTSFORMEN, jahr=date.today().year,
            standard=kurz(str(firmen.base_folder())))
        response = HTMLResponse(html)
        if raw:
            response.delete_cookie(FLASH)
        return response

    async def oeffnen(request: Request):
        ordner = (await request.form()).get("ordner", "")
        try:
            await asyncio.to_thread(firmen.merken, Path(ordner))
            ui.switch(Path(ordner))
        except BookError as exc:
            return fail(str(exc))
        return done(f"{ui.book().settings.firma} geöffnet", "/")

    async def neu(request: Request):
        f = await request.form()
        jahr = (f.get("jahr") or "").strip()
        try:
            result = await asyncio.to_thread(
                api.company_create, f.get("firma", ""), f.get("rechtsform") or "GmbH",
                int(jahr) if jahr.isdigit() else None, f.get("ordner", ""),
                **{k: (f.get(k) or "").strip() for k in ("strasse", "nr", "plz", "ort", "uid")})
            ui.switch(Path(result["buch"]))
        except BookError as exc:
            return fail(str(exc))
        return done(f"Buch {result['firma']} eröffnet — Bank, Logo und MWST unter Einstellungen ergänzen",
                    "/einstellungen")

    async def hinzufuegen(request: Request):
        ordner = (await request.form()).get("ordner", "")
        try:
            result = await asyncio.to_thread(api.company_add, ordner)
        except BookError as exc:
            return fail(str(exc))
        return done(result["meldung"], "/firmen")

    async def importieren(request: Request):
        f = await request.form()
        upload = f.get("datei")
        if not getattr(upload, "filename", ""):
            return fail("Keine .aeradex-Datei gewählt")
        with tempfile.TemporaryDirectory(prefix="aeradex-firma-") as tmp:
            path = Path(tmp) / "buch.aeradex"
            path.write_bytes(await upload.read())
            try:
                result = await asyncio.to_thread(api.company_import, str(path), f.get("ordner", ""),
                                                 f.get("passwort") or None)
                ui.switch(Path(result["ordner"]))
            except (BookError, ValueError) as exc:
                return fail(str(exc))
        return done(result.get("meldung") or "Buch importiert", "/")

    async def entfernen(request: Request):
        ordner = (await request.form()).get("ordner", "")
        if ui.root is not None and Path(ordner).expanduser().resolve() == ui.root:
            return fail("Die geöffnete Firma lässt sich nicht entfernen — zuerst eine andere öffnen.")
        try:
            result = await asyncio.to_thread(api.company_remove, ordner)
        except BookError as exc:
            return fail(str(exc))
        return done(result["meldung"], "/firmen")

    return [Route("/firmen", liste),
            Route("/firmen/oeffnen", oeffnen, methods=["POST"]),
            Route("/firmen/neu", neu, methods=["POST"]),
            Route("/firmen/hinzufuegen", hinzufuegen, methods=["POST"]),
            Route("/firmen/importieren", importieren, methods=["POST"]),
            Route("/firmen/entfernen", entfernen, methods=["POST"])]

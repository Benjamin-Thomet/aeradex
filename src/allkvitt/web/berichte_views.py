"""Berichte in the UI: any report for any period, export, templates, comments, monthly package, budget grid."""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from urllib.parse import quote, urlencode

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .. import api, berichte, budget, reports
from ..book import BookError
from .app import UI, act, done, fail

PARAMS = ("jahr", "periode", "von", "bis", "spalten", "vergleich", "stichtag", "nach")
SERIES = ["var(--info)", "var(--warn)", "var(--ok)"]


def _params(source) -> dict:
    return {k: source.get(k) for k in PARAMS if source.get(k)}


def chart(spec: dict | None) -> dict | None:
    """Geometry of a bar chart (positive and negative values) for an inline SVG."""
    if not spec or not spec.get("labels"):
        return None
    values = [float(v or 0) for s in spec["reihen"] for v in s["werte"]]
    top, bottom = max(values + [0.0]), min(values + [0.0])
    span = (top - bottom) or 1.0
    plot_top, plot_h, left, width = 14, 130, 70, 810
    zero = plot_top + plot_h * top / span
    n, k = len(spec["labels"]), len(spec["reihen"])
    slot = width / max(n, 1)
    bar = min(34.0, slot * 0.7 / k)
    bars = []
    for i, label in enumerate(spec["labels"]):
        for j, s in enumerate(spec["reihen"]):
            v = float(s["werte"][i] or 0)
            h = plot_h * abs(v) / span
            x = left + slot * i + slot / 2 - bar * k / 2 + bar * j
            bars.append({"x": round(x, 1), "y": round(zero - h if v >= 0 else zero, 1), "w": round(bar - 2, 1),
                         "h": round(h, 1), "fill": SERIES[j % len(SERIES)], "title": f"{s['name']} {label}",
                         "wert": s["werte"][i]})
    ticks = [{"y": round(plot_top + plot_h * (top - t) / span, 1), "wert": Decimal(str(round(t)))}
             for t in (top, 0.0, bottom) if t or t == 0.0]
    labels = [{"x": round(left + slot * i + slot / 2, 1), "text": str(l)[:10]} for i, l in enumerate(spec["labels"])]
    return {"bars": bars, "zero": round(zero, 1), "ticks": {t["y"]: t for t in ticks}.values(), "labels": labels,
            "legend": [{"name": s["name"], "fill": SERIES[j % len(SERIES)]} for j, s in enumerate(spec["reihen"])],
            "left": left, "right": left + width}


def _drill(report: dict, col: dict, konto: str) -> str | None:
    """Link from a figure to the account sheet of exactly that period."""
    if not col.get("von") or not col.get("bis") or not konto[:1].isdigit():
        return None
    return f"/konten/{quote(konto)}?" + urlencode({"jahr": col["bis"].year, "von": col["von"].isoformat(),
                                                    "bis": col["bis"].isoformat()})


async def berichte_page(ui: UI, request: Request):
    book = ui.book()
    q = request.query_params
    registry = reports.registry(book)
    vorlage = q.get("vorlage") or ""
    typ = q.get("typ") or "erfolgsrechnung"
    params = _params(q)
    params.setdefault("jahr", str(date.today().year))
    detail = q.get("detail") == "1"
    error, rep, comment = "", None, None
    try:
        rep = await asyncio.to_thread(
            lambda: berichte.run_template(book, vorlage, **params) if vorlage else reports.run(book, typ, **params))
        comment = berichte.comment(book, rep)
        typ = rep["typ"]
    except (BookError, ValueError) as exc:
        error = str(exc)
    groups: dict[str, list] = {}
    for r in registry.values():
        groups.setdefault(r.gruppe, []).append(r)
    links = {}
    if rep:
        for r in rep["zeilen"]:
            for k in r.get("konten") or []:
                links[k["konto"]] = {c["key"]: _drill(rep, c, k["konto"]) for c in rep["spalten"]}
    query = urlencode({**params, "typ": typ, **({"vorlage": vorlage} if vorlage else {}),
                       **({"detail": "1"} if detail else {})})
    return ui.render(request, "berichte.html", book=book, tab="berichte", groups=groups, registry=registry,
                     typ=typ, vorlage=vorlage, vorlagen=berichte.templates(book), p=params, detail=detail,
                     rep=rep, kommentar=comment, error=error, chart=chart(rep.get("diagramm") if rep else None),
                     links=links, query=query, params_of=registry.get(typ).params if typ in registry else (),
                     fmt=berichte.fmt, stand=berichte.stand_text(berichte.stand(book)) if rep else "",
                     monat=_previous_month())


def _previous_month() -> str:
    first = date.today().replace(day=1)
    prev = date.fromordinal(first.toordinal() - 1)
    return f"{prev.year}-{prev.month:02d}"


async def berichte_export(ui: UI, request: Request):
    book = ui.book()
    q = request.query_params
    fmt = request.path_params["fmt"]
    try:
        data, name = await asyncio.to_thread(api.report_export, book, q.get("typ") or "", fmt, q.get("vorlage") or "",
                                             q.get("detail") == "1", **_params(q))
    except (BookError, ValueError) as exc:
        return PlainTextResponse(str(exc), status_code=400)
    media = {"pdf": "application/pdf", "csv": "text/csv; charset=utf-8",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    disposition = "inline" if fmt == "pdf" else "attachment"
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}"})


async def berichte_aktion(ui: UI, request: Request):
    book = ui.book()
    f = await request.form()
    aktion = request.path_params["aktion"]
    params = _params(f)
    typ, vorlage = f.get("typ") or "", f.get("vorlage") or ""
    back = request.headers.get("hx-current-url") or "/berichte"
    if aktion == "vorlage":
        return await act(request, api.report_template_save, lambda r: "/berichte?" + urlencode(
            {"vorlage": f.get("name") or "", "jahr": params.get("jahr", "")}), book, f.get("name") or "", typ,
            f.get("beschreibung") or "", **params)
    if aktion == "vorlage-loeschen":
        return await act(request, api.report_template_delete, "/berichte", book, f.get("name") or "")
    if aktion == "kommentar":
        author = "Mensch"
        user = getattr(request.state, "user", None)
        if user is not None:
            author = getattr(user, "anzeige", "") or getattr(user, "name", "") or author
        return await act(request, api.report_comment_save, back, book, f.get("text") or "", typ, vorlage, author,
                         **params)
    if aktion == "agent":
        try:
            if vorlage:
                t = berichte.templates(book)[vorlage]
                typ, params = t["typ"], {**t.get("parameter", {}), **params}
            await asyncio.to_thread(berichte.run_agent_comment, book.root, typ, **params)
        except (BookError, KeyError, RuntimeError) as exc:
            return fail(str(exc))
        return done("Kommentar vom Agenten gespeichert", back)
    if aktion == "monat":
        monat = f.get("monat") or ""
        try:
            out = await asyncio.to_thread(api.monthly_report, book, monat, f.get("mail") == "1")
        except (BookError, ValueError) as exc:
            return fail(str(exc))
        return done(out["meldung"], "/datei/" + quote(out["dateien"][0]))
    return fail("Unbekannte Aktion")


async def budget_page(ui: UI, request: Request):
    book = ui.book()
    try:
        jahr = int(request.query_params.get("jahr") or date.today().year)
    except ValueError:
        jahr = date.today().year
    rows = budget.grid(book, jahr)
    return ui.render(request, "budget.html", book=book, tab="budget", jahr=jahr, rows=rows,
                     vorhanden=budget.exists(book, jahr),
                     total={k: sum((r["jahr"] for r in rows if r["klasse"] == k), Decimal(0))
                            for k in ("ertrag", "aufwand")})


async def budget_aktion(ui: UI, request: Request):
    book = ui.book()
    f = await request.form()
    jahr = int(f.get("jahr") or date.today().year)
    to = f"/berichte/budget?jahr={jahr}"
    if request.path_params["aktion"] == "vorjahr":
        return await act(request, api.budget_from_prior, to, book, jahr, f.get("prozent") or "0")
    grid: dict[str, list] = {}
    for key, value in f.items():
        if key.startswith("b_"):
            _, konto, month = key.split("_")
            grid.setdefault(konto, [""] * 12)[int(month) - 1] = value
    return await act(request, api.budget_grid_save, to, book, jahr, grid)


def routes(ui: UI) -> list[Route]:
    def h(fn):
        async def handler(request):
            return await fn(ui, request)
        handler.__name__ = fn.__name__
        return handler

    return [
        Route("/berichte", h(berichte_page)),
        Route("/berichte/export.{fmt:str}", h(berichte_export)),
        Route("/berichte/budget", h(budget_page)),
        Route("/berichte/budget/{aktion:str}", h(budget_aktion), methods=["POST"]),
        Route("/berichte/{aktion:str}", h(berichte_aktion), methods=["POST"]),
    ]

"""Reports out of the house: text, PDF, Excel, CSV; saved templates; comments; the monthly package.

    auswertungen/vorlagen.yaml              saved reports (name → typ + parameters)
    auswertungen/kommentare/<schlüssel>.md  a comment on one report, with the fingerprint of its figures
    berichte/<JJJJ-MM>/                     the monthly package (PDF + Excel) — generated output, like the
                                            Abschluss-ZIP: berichte/ is not in git and can be rebuilt any time

Every output carries its Stand: the git commit the figures come from and the
check status, so a printed report can always be traced back to the book.
"""
from __future__ import annotations

import io
import re
import subprocess
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import reports
from .book import Book, BookError
from .files import read_frontmatter, read_yaml, slug, write_frontmatter, write_yaml

ZERO = Decimal("0")


# ---------- Stand ----------

def stand(book: Book) -> dict:
    """The git commit the figures come from, whether the files differ from it, and the check status."""
    from . import check
    commit, dirty = "", False
    try:
        out = subprocess.run(["git", "-C", str(book.root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        commit = out.stdout.strip() if out.returncode == 0 else ""
        if commit:
            st = subprocess.run(["git", "-C", str(book.root), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10)
            dirty = bool(st.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    issues = check.run(Book(book.root))
    errors = sum(1 for i in issues if i.level == "fehler")
    warnings = sum(1 for i in issues if i.level == "warnung")
    return {"commit": commit, "geaendert": dirty, "fehler": errors, "warnungen": warnings,
            "erstellt": datetime.now().replace(microsecond=0)}


def stand_text(s: dict) -> str:
    parts = [f"Stand {s['erstellt']:%d.%m.%Y %H:%M}"]
    if s["commit"]:
        parts.append(f"git {s['commit']}" + (" + nicht festgehaltene Änderungen" if s["geaendert"] else ""))
    parts.append("Buch geprüft: " + ("ohne Fehler" if not s["fehler"] else f"{s['fehler']} Fehler")
                 + (f", {s['warnungen']} Warnungen" if s["warnungen"] else ""))
    return " · ".join(parts)


# ---------- values as text ----------

def fmt(value, einheit: str = "", art: str = "ist") -> str:
    if value is None:
        return "–"
    if isinstance(value, str) or art == "text":
        return str(value)
    if art == "pct" or einheit == "%":
        return f"{Decimal(value):.1f} %"
    if einheit == "Tage":
        return f"{Decimal(value):.0f} Tage"
    return f"{Decimal(value):,.2f}".replace(",", "'")


def value_columns(report: dict) -> list[dict]:
    return report["spalten"]


def table(report: dict, detail: bool = False) -> tuple[list[str], list[list], list[str]]:
    """Header, rows (label + raw values) and a style per row — the common form for all outputs."""
    cols = value_columns(report)
    header = [""] + [c["label"] for c in cols]
    rows, styles = [], []
    for r in report["zeilen"]:
        rows.append([r["label"]] + [r["werte"].get(c["key"]) for c in cols])
        styles.append(r["stil"])
        if detail:
            for k in r.get("konten") or []:
                rows.append([f"    {k['konto']} {k['name']}"] + [k["werte"].get(c["key"]) for c in cols])
                styles.append("konto")
    return header, rows, styles


def _units(report: dict, detail: bool) -> list[str]:
    """The unit of every row `table` returns (detail rows inherit their line's unit)."""
    out = []
    for r in report["zeilen"]:
        out.append(r.get("einheit", ""))
        if detail:
            out += [r.get("einheit", "")] * len(r.get("konten") or [])
    return out


def text(report: dict, detail: bool = False) -> str:
    header, rows, styles = table(report, detail)
    cols = value_columns(report)
    unit_rows = _units(report, detail)
    cells = [[row[0]] + [fmt(v, unit_rows[n], c["art"]) for v, c in zip(row[1:], cols)]
             for n, row in enumerate(rows)]
    width0 = max([len(header[0])] + [len(c[0]) for c in cells] + [10])
    widths = [max([len(header[j])] + [len(c[j]) for c in cells]) for j in range(1, len(header))]
    lines = [f"{report['titel']} · {report['untertitel']}", ""]
    lines.append(header[0].ljust(width0) + "  " + "  ".join(h.rjust(w) for h, w in zip(header[1:], widths)))
    for c, style in zip(cells, styles):
        if style == "kopf":
            lines.append("")
        line = c[0].ljust(width0) + "  " + "  ".join(v.rjust(w) for v, w in zip(c[1:], widths))
        if style == "kopf":
            line = c[0]
        lines.append(line)
        if style == "total":
            lines.append("")
    for h in report.get("hinweise") or []:
        lines.append(f"Hinweis: {h}")
    return "\n".join(lines).rstrip() + "\n"


# ---------- PDF ----------

def _chart(spec: dict, width: float):
    """A bar chart (reportlab) for the PDF, or None."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors
    if not spec or not spec.get("labels"):
        return None
    height = 150
    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 40, 30, width - 60, height - 50
    chart.data = [[float(v or 0) for v in s["werte"]] for s in spec["reihen"]]
    chart.categoryAxis.categoryNames = [str(l)[:12] for l in spec["labels"]]
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labelTextFormat = lambda v: f"{v:,.0f}".replace(",", "'")
    palette = [colors.HexColor("#2f6f8f"), colors.HexColor("#c47a2c"), colors.HexColor("#6b8f3a")]
    for i in range(len(chart.data)):
        chart.bars[i].fillColor = palette[i % len(palette)]
        chart.bars[i].strokeColor = None
    chart.barSpacing = 1
    d.add(chart)
    x = 40
    for i, s in enumerate(spec["reihen"]):
        d.add(String(x, height - 10, f"■ {s['name']}", fontSize=8.5, fillColor=palette[i % len(palette)]))
        x += 110
    return d


def pdf(book: Book, items: list[dict], titel: str = "", detail: bool = False) -> bytes:
    """One PDF with one or more reports. items: {"bericht": report, "kommentar": text|None}."""
    from reportlab.lib.pagesizes import A4, landscape

    from xml.sax.saxutils import escape

    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph

    from . import pdf as core
    head_style = ParagraphStyle("kopf", fontName="Helvetica-Bold", fontSize=7.5, leading=9, alignment=TA_RIGHT,
                                textColor=core.MUTED)
    s = stand(book)
    wide = any(len(i["bericht"]["spalten"]) > 5 for i in items)
    width = (landscape(A4) if wide else A4)[0] - 2 * core.SIDE
    blocks: list = []
    for n, item in enumerate(items):
        rep = item["bericht"]
        if n:
            blocks.append(("page",))
        if len(items) > 1 or titel:
            blocks.append(("h2", f"{rep['titel']} · {rep['untertitel']}"))
        header, rows, styles = table(rep, detail)
        cols = value_columns(rep)
        units = _units(rep, detail)
        body, totals = [], []
        for i, (row, style) in enumerate(zip(rows, styles)):
            if style == "kopf":
                body.append([row[0]] + [""] * len(cols))
            else:
                body.append([row[0]] + [fmt(v, units[i], c["art"]) for v, c in zip(row[1:], cols)])
            if style in ("zwischentotal", "total"):
                totals.append(i)
        n_cols = len(cols)
        first = 0.55 if n_cols == 1 else max(0.2, min(0.42, 1 - 0.135 * n_cols))
        rest = (1 - first) / max(n_cols, 1)
        head = [header[0]] + [Paragraph(escape(h), head_style) for h in header[1:]]   # long labels wrap
        blocks.append(("table", head, body, [first] + [rest] * n_cols,
                       {"right": tuple(range(1, n_cols + 1)), "totals": tuple(totals), "wrap": (0,)}))
        chart = _chart(rep.get("diagramm"), width)
        if chart is not None:
            blocks.append(("flowable", chart))
        for h in rep.get("hinweise") or []:
            blocks.append(("small", f"Hinweis: {h}"))
        if item.get("kommentar"):
            k = item["kommentar"]
            blocks.append(("h2", "Kommentar" + (" (veraltet — die Zahlen haben sich seither geändert)"
                                                if k.get("veraltet") else "")))
            for para in [p for p in k["text"].split("\n\n") if p.strip()]:
                blocks.append(("text", para))
            blocks.append(("small", f"{k.get('autor') or ''} · {k.get('datum') or ''}"))
    blocks.append(("small", stand_text(s)))
    first = items[0]["bericht"]
    return core.report_pdf(book, titel or first["titel"], blocks,
                           subtitle="" if (len(items) > 1 or titel) else first["untertitel"], wide=wide)


# ---------- Excel and CSV ----------

def xlsx(book: Book, items: list[dict], detail: bool = True) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise BookError("Für Excel fehlt openpyxl: pip install 'allkvitt[excel]' — oder als CSV exportieren") from None
    s = stand(book)
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    bold, top = Font(bold=True), Border(top=Side(style="thin"))
    for item in items:
        rep = item["bericht"]
        name = re.sub(r"[\[\]:*?/\\]", "", rep["titel"])[:28] or "Bericht"
        base, n = name, 2
        while name in used:
            name, n = f"{base[:26]} {n}", n + 1
        used.add(name)
        ws = wb.create_sheet(name)
        ws.append([f"{book.settings.firma} · {rep['titel']} · {rep['untertitel']}"])
        ws["A1"].font = Font(bold=True, size=12)
        ws.append([])
        cols = value_columns(rep)
        ws.append([""] + [c["label"] for c in cols])
        for cell in ws[3]:
            cell.font = bold
        for r in rep["zeilen"]:
            unit = r.get("einheit", "")
            ws.append([r["label"]] + [None if r["stil"] == "kopf" else r["werte"].get(c["key"]) for c in cols])
            row = ws.max_row
            for j, c in enumerate(cols, start=2):
                if c["art"] == "text":
                    continue
                ws.cell(row, j).number_format = ("0.0" if c["art"] == "pct" or unit == "%" else
                                                 "0" if unit == "Tage" else "#,##0.00")
            if r["stil"] in ("zwischentotal", "total", "kopf"):
                for cell in ws[row]:
                    cell.font = bold
                    if r["stil"] != "kopf":
                        cell.border = top
            if detail:
                for k in r.get("konten") or []:
                    ws.append([f"{k['konto']} {k['name']}"] + [k["werte"].get(c["key"]) for c in cols])
                    ws.cell(ws.max_row, 1).alignment = Alignment(indent=2)
                    for j in range(2, len(cols) + 2):
                        ws.cell(ws.max_row, j).number_format = "#,##0.00"
                    ws.row_dimensions[ws.max_row].outlineLevel = 1
        ws.append([])
        for h in rep.get("hinweise") or []:
            ws.append([f"Hinweis: {h}"])
        if item.get("kommentar"):
            ws.append(["Kommentar" + (" (veraltet)" if item["kommentar"].get("veraltet") else "")])
            ws.append([item["kommentar"]["text"]])
        ws.append([stand_text(s)])
        ws.column_dimensions["A"].width = 58
        for j in range(2, len(cols) + 2):
            ws.column_dimensions[get_column_letter(j)].width = 15
        ws.freeze_panes = "B4"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv(report: dict, detail: bool = True) -> bytes:
    from .dossier import csv_bytes
    header, rows, _ = table(report, detail)
    return csv_bytes(["Position"] + header[1:], [[r[0].strip()] + r[1:] for r in rows])


# ---------- templates ----------

PARAMS = ("jahr", "periode", "von", "bis", "spalten", "vergleich", "stichtag", "nach", "detail")


def templates_path(book: Book) -> Path:
    return book.root / "auswertungen" / "vorlagen.yaml"


def templates(book: Book) -> dict[str, dict]:
    p = templates_path(book)
    return {str(k): dict(v or {}) for k, v in ((read_yaml(p) or {}) if p.exists() else {}).items()}


def save_template(book: Book, name: str, typ: str, params: dict, beschreibung: str = "") -> tuple[dict, list[Path]]:
    name = name.strip()
    if not name:
        raise BookError("Vorlage: Name fehlt")
    if typ not in reports.registry(book):
        raise BookError(f"Bericht '{typ}' unbekannt")
    clean = {k: v for k, v in params.items() if k in PARAMS and v not in (None, "")}
    clean.pop("jahr", None)                        # a template is reused for every year
    all_ = templates(book)
    all_[name] = {"typ": typ, "parameter": clean, **({"beschreibung": beschreibung} if beschreibung else {})}
    p = templates_path(book)
    write_yaml(p, {k: all_[k] for k in sorted(all_)})
    return {"name": name, **all_[name]}, [p]


def delete_template(book: Book, name: str) -> tuple[dict, list[Path]]:
    all_ = templates(book)
    if name not in all_:
        raise BookError(f"Vorlage '{name}' gibt es nicht")
    del all_[name]
    p = templates_path(book)
    write_yaml(p, all_)
    return {"name": name}, [p]


def run_template(book: Book, name: str, **override) -> dict:
    t = templates(book).get(name)
    if t is None:
        raise BookError(f"Vorlage '{name}' gibt es nicht: {', '.join(templates(book)) or 'keine'}")
    rep = reports.run(book, t["typ"], **{**t.get("parameter", {}), **{k: v for k, v in override.items() if v}})
    rep["vorlage"] = name
    return rep


# ---------- comments ----------

def comment_key(report: dict) -> str:
    p = report.get("parameter") or {}
    # by report and parameters, not by template: the same figures share one comment however they were opened
    parts = [report["typ"]] + [f"{k}-{p[k]}" for k in sorted(p) if k in PARAMS and k != "detail"]
    return slug("-".join(str(x) for x in parts))[:120]


def comment_path(book: Book, report: dict) -> Path:
    return book.root / "auswertungen" / "kommentare" / f"{comment_key(report)}.md"


def comment(book: Book, report: dict) -> dict | None:
    p = comment_path(book, report)
    if not p.exists():
        return None
    meta, body = read_frontmatter(p)
    return {"text": body.strip(), "autor": meta.get("autor", ""), "datum": meta.get("datum", ""),
            "veraltet": meta.get("fingerprint") != report["fingerprint"], "pfad": p}


def save_comment(book: Book, report: dict, text_: str, autor: str = "") -> tuple[dict, list[Path]]:
    text_ = (text_ or "").strip()
    if not text_:
        raise BookError("Kommentar ist leer")
    p = comment_path(book, report)
    meta = {"bericht": report["typ"], "titel": f"{report['titel']} · {report['untertitel']}",
            "parameter": report.get("parameter") or {}, "fingerprint": report["fingerprint"],
            "autor": autor or "Mensch", "datum": date.today().isoformat()}
    write_frontmatter(p, meta, text_)
    return {"pfad": book.rel(p), "schluessel": comment_key(report)}, [p]


def agent_prompt(report: dict) -> str:
    p = report.get("parameter") or {}
    args = ", ".join(f"{k}={v}" if k == "jahr" else f'{k}="{v}"'
                     for k, v in sorted(p.items()) if k in PARAMS and k != "detail")
    return (f"Kommentiere den Bericht «{report['titel']} · {report['untertitel']}» für die Geschäftsleitung.\n"
            f"1. Lies ihn mit period_report(typ=\"{report['typ']}\"{', ' + args if args else ''}) — rechne nichts selbst, "
            "zitiere nur Zahlen aus dem Bericht.\n"
            "2. Schreibe 3–6 kurze Sätze auf Deutsch (Schweiz, ohne ß): Was ist auffällig, wo sind grosse Abweichungen "
            "(Vorperiode/Vorjahr/Budget), was sollte man prüfen? Keine Empfehlungen ohne Grundlage in den Zahlen.\n"
            f"3. Speichere ihn mit save_report_comment(typ=\"{report['typ']}\"{', ' + args if args else ''}, text=…).")


def run_agent_comment(root: Path, typ: str, **params) -> str:
    """Let the book's agent write the comment (it reads the report through the tools and saves it)."""
    import queue
    try:
        from .web import chat
    except ImportError as exc:
        raise BookError(f"Für den Agenten fehlen Pakete ({exc.name}): pip install 'allkvitt[ui]'") from None
    book = Book(root)
    kind = chat.backend(book.settings.get("agent_backend"))
    if not kind:
        raise BookError("Kein Agent verfügbar (Claude Code, Codex, opencode oder ANTHROPIC_API_KEY)")
    rep = reports.run(book, typ, **params)
    runner = chat.AGENTS[kind](root)
    events: queue.Queue = queue.Queue()
    runner.run(events, agent_prompt(rep))
    said = []
    while not events.empty():
        ev = events.get()
        if ev.get("type") == "text":
            said.append(ev.get("text", ""))
    if comment(Book(root), reports.run(Book(root), typ, **params)) is None:
        raise BookError("Der Agent hat keinen Kommentar gespeichert" + (f": {said[-1][:200]}" if said else ""))
    return said[-1] if said else ""


# ---------- monthly package ----------

def monthly_items(book: Book, monat: str) -> list[dict]:
    from . import budget
    from .ledger import month_end
    y, m = (int(x) for x in monat.split("-"))
    last = month_end(y, m)
    cmp = "budget" if budget.exists(book, y) else "vorjahr"
    specs = [
        ("erfolgsrechnung", {"jahr": y, "periode": f"{m:02d}", "vergleich": cmp}),
        ("erfolgsrechnung", {"jahr": y, "von": f"{y}-01-01", "bis": last.isoformat(), "vergleich": cmp}),
        ("bilanz", {"jahr": y, "periode": f"{m:02d}", "vergleich": "vorperiode"}),
        ("geldfluss", {"jahr": y, "von": f"{y}-01-01", "bis": last.isoformat()}),
        ("kennzahlen", {"jahr": y, "von": f"{y}-01-01", "bis": last.isoformat(), "stichtag": last.isoformat()}),
        ("debitoren", {"stichtag": last.isoformat()}),
        ("kreditoren", {"stichtag": last.isoformat()}),
    ]
    items = []
    for typ, params in specs:
        rep = reports.run(book, typ, **params)
        if typ == "erfolgsrechnung" and "von" in params:
            rep["titel"] = "Erfolgsrechnung kumuliert"
        items.append({"bericht": rep, "kommentar": comment(book, rep)})
    return items


def monthly_package(book: Book, monat: str, mail: bool = False) -> tuple[dict, list[Path]]:
    """Write berichte/<JJJJ-MM>/Monatsbericht <JJJJ-MM>.pdf and .xlsx (and mail them, if asked)."""
    if not re.fullmatch(r"\d{4}-\d{2}", monat or ""):
        raise BookError("Monat als JJJJ-MM")
    items = monthly_items(book, monat)
    folder = book.root / "berichte" / monat
    folder.mkdir(parents=True, exist_ok=True)
    pdf_path = folder / f"Monatsbericht {monat}.pdf"
    pdf_path.write_bytes(pdf(book, items, f"Monatsbericht {monat}"))
    paths = [pdf_path]
    try:
        x = folder / f"Monatsbericht {monat}.xlsx"
        x.write_bytes(xlsx(book, items))
        paths.append(x)
    except BookError:
        pass                                   # no openpyxl: the PDF is enough
    sent = ""
    if mail:
        from . import mail as mailer
        sent = mailer.send(book, f"{book.settings.firma}: Monatsbericht {monat}",
                           f"Guten Tag\n\nIm Anhang der Monatsbericht {monat}.\n\n{stand_text(stand(book))}\n",
                           paths)
    return {"monat": monat, "dateien": [book.rel(p) for p in paths], "gesendet_an": sent}, paths

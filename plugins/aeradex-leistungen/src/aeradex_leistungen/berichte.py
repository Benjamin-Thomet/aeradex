"""Reports of the plugin for Berichte (`aeradex bericht …`): utilisation per person, products, projects."""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from aeradex import invoices
from aeradex.reports import Column, Report, _report, _window, window_label

from . import daten, kontrolle

ZERO = Decimal("0")


def _pct(a: Decimal, b: Decimal) -> Decimal | None:
    return (a / b * 100).quantize(Decimal("0.1"), ROUND_HALF_UP) if b else None


def _entries(book, p: dict) -> tuple[list[dict], str]:
    start, end = _window(p)
    return [e for e in daten.with_state(book) if start <= e["datum"] <= end], window_label(start, end)


def personen(book, p: dict) -> dict:
    """Hours per person: total, billable, utilisation, value at billing rates, cost at cost rates."""
    items, label = _entries(book, p)
    people = daten.people(book)
    cols = [Column("ist", "Stunden"), Column("abr", "abrechenbar"), Column("quote", "Quote %", "pct"),
            Column("wert", "Wert CHF"), Column("kosten", "Kosten CHF"), Column("offen", "noch offen CHF")]
    per: dict[str, dict] = defaultdict(lambda: {c.key: ZERO for c in cols})
    for e in items:
        if e["art"] != "Zeit":
            continue
        w = per[e["wer"]]
        w["ist"] += e["menge"]
        if e["abrechenbar"]:
            w["abr"] += e["menge"]
            w["wert"] += e["betrag"]
            if e["status"] == "offen":
                w["offen"] += e["betrag"]
        w["kosten"] += daten.money(e["menge"] * (people.get(e["wer"], {}).get("kostensatz") or ZERO))
    rows = []
    for nr in sorted(per):
        w = per[nr]
        w["quote"] = _pct(w["abr"], w["ist"])
        rows.append({"label": people.get(nr, {}).get("name", nr), "stil": "line", "werte": w, "konten": []})
    total = {c.key: sum((r["werte"][c.key] or ZERO for r in rows), ZERO) for c in cols}
    total["quote"] = _pct(total["abr"], total["ist"])
    rows.append({"label": "Total", "stil": "total", "werte": total, "konten": []})
    return _report("leistungen_personen", "Auslastung", label, cols, rows,
                   diagramm={"art": "balken", "labels": [r["label"] for r in rows[:-1]],
                             "reihen": [{"name": "abrechenbar", "werte": [r["werte"]["abr"] for r in rows[:-1]]},
                                        {"name": "intern", "werte": [r["werte"]["ist"] - r["werte"]["abr"]
                                                                      for r in rows[:-1]]}]})


def produkte(book, p: dict) -> dict:
    """Products sold: quantity and value, billed and open."""
    items, label = _entries(book, p)
    prods = daten.products(book)
    cols = [Column("menge", "Menge"), Column("wert", "Wert CHF"), Column("verrechnet", "verrechnet CHF"),
            Column("offen", "offen CHF")]
    per: dict[str, dict] = defaultdict(lambda: {c.key: ZERO for c in cols})
    for e in items:
        if e["art"] != "Produkt" or not e["abrechenbar"]:
            continue
        w = per[e["produkt"]]
        w["menge"] += e["menge"]
        w["wert"] += e["betrag"]
        w["verrechnet" if e["status"] == "abgerechnet" else "offen"] += e["betrag"]
    rows = [{"label": f"{nr} {prods.get(nr, {}).get('text', '')}", "stil": "line", "werte": per[nr], "konten": [],
             "einheit": ""} for nr in sorted(per, key=lambda k: -per[k]["wert"])]
    rows.append({"label": "Total", "stil": "total", "konten": [],
                 "werte": {c.key: sum((r["werte"][c.key] for r in rows), ZERO) for c in cols}})
    return _report("leistungen_produkte", "Umsatz nach Produkt", label, cols, rows)


def projekte(book, p: dict) -> dict:
    """Open projects: budget against actual, billed, contribution margin."""
    cols = [Column("budget_h", "Budget h"), Column("ist_h", "Ist h"), Column("auslastung", "Budget %", "pct"),
            Column("budget_chf", "Budget CHF"), Column("wert", "Wert CHF"), Column("verrechnet", "verrechnet CHF"),
            Column("db", "DB CHF")]
    names = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    rows = []
    for nr, proj in sorted(daten.projects(book).items()):
        if proj.get("status") != "offen" and p.get("alle") != "1":
            continue
        st = kontrolle.project(book, nr)
        rows.append({"label": f"{nr} {st['name']} ({names.get(st['kunde'], st['kunde'])}, {st['abrechnung']})",
                     "stil": "line", "konten": [],
                     "werte": {"budget_h": st["budget_stunden"], "ist_h": st["stunden"], "auslastung": st["auslastung"],
                               "budget_chf": st["budget_chf"], "wert": st["wert"], "verrechnet": st["abgerechnet"],
                               "db": st["deckungsbeitrag"]}})
    return _report("leistungen_projekte", "Projekte", "offene Projekte", cols, rows)


REPORTS = [
    Report("leistungen_personen", "Auslastung (Stunden je Person)", personen, ("periode",), gruppe="Leistungen",
           beschreibung="Stunden, abrechenbar-Quote, Wert und Kosten je Person"),
    Report("leistungen_produkte", "Umsatz nach Produkt", produkte, ("periode",), gruppe="Leistungen",
           beschreibung="erfasste Produkte, verrechnet und offen"),
    Report("leistungen_projekte", "Projekte", projekte, (), gruppe="Leistungen",
           beschreibung="Budget gegen Ist, verrechnet, Deckungsbeitrag"),
]

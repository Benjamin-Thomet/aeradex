"""Pages in the UI: «Leistungen» (Erfassen, Abrechnen, Projekte, Kontrolle, Stammdaten) and «Offerten»."""
from __future__ import annotations

from datetime import date

from aeradex import api, invoices
from aeradex.book import BookError
from aeradex.plugins import Page

from . import abrechnung, daten, kontrolle, offerten

TABS = [("erfassen", "Erfassen"), ("abrechnen", "Abrechnen"), ("projekte", "Projekte"),
        ("kontrolle", "Stundenkontrolle"), ("stammdaten", "Stammdaten")]


def _list(form: dict, key: str) -> list:
    v = form.get(key)
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _month(query: dict) -> tuple[int, int]:
    raw = query.get("monat") or date.today().strftime("%Y-%m")
    y, m = raw.split("-")
    return int(y), int(m)


def _common(book) -> dict:
    custs = invoices.customers(book)
    return {"tabs": TABS, "kunden": {k: invoices.qr.invoice_name(c) for k, c in custs.items()},
            "personen": daten.people(book), "produkte": daten.products(book), "projekte": daten.projects(book),
            "cfg": daten.settings(book)}


def _context(book, query):
    tab = query.get("tab") or "erfassen"
    ctx = {"tab": tab, **_common(book)}
    y, m = _month(query)
    ctx["monat"] = f"{y}-{m:02d}"
    if tab == "erfassen":
        start, end = kontrolle.month_range(y, m)
        ctx["eintraege"] = abrechnung.select(book, query.get("kunde") or "", von=start, bis=end, status=query.get("status") or "")
        ctx["filter_kunde"] = query.get("kunde") or ""
        ctx["filter_status"] = query.get("status") or ""
    elif tab == "abrechnen":
        ctx["uebersicht"] = abrechnung.summary(book)
        kunde = query.get("kunde") or ""
        ctx.update(kunde=kunde, projekt=query.get("projekt") or "", von=query.get("von") or "",
                   bis=query.get("bis") or "", detail=query.get("detail") == "1")
        if kunde:
            items = abrechnung.select(book, kunde, ctx["projekt"], ctx["von"] or None, ctx["bis"] or None)
            ctx["auswahl"] = items
            try:
                ctx["vorschau"] = abrechnung.preview(book, [e["id"] for e in items], ctx["detail"]) if items else None
            except BookError as exc:
                ctx["vorschau_fehler"] = str(exc)
    elif tab == "projekte":
        ctx["status"] = query.get("status") or "offen"
        ctx["liste"] = [kontrolle.project(book, nr) for nr, p in ctx["projekte"].items()
                        if ctx["status"] == "alle" or p.get("status") == ctx["status"]]
    elif tab == "kontrolle":
        ctx["zeilen"] = kontrolle.month(book, y, m)
        ctx["wer"] = query.get("wer") or ""
        if ctx["wer"]:
            ctx["jahr"] = kontrolle.year(book, y, ctx["wer"])
    return ctx



def _zeit(book, f):
    abr = f.get("abrechenbar") == "ja"
    return api.write(book, f"Zeit {f.get('stunden')} h erfasst", daten.add_time, f.get("datum"), f.get("wer") or "",
                     f.get("stunden"), f.get("text") or "", f.get("kunde") or "", f.get("projekt") or "", abr,
                     "" if abr else (f.get("kategorie") or ""), f.get("satz") or None)


def _material(book, f):
    return api.write(book, f"Produkt {f.get('produkt')} erfasst", daten.add_material, f.get("datum"),
                     f.get("kunde") or "", f.get("produkt") or "", f.get("menge"), f.get("projekt") or "",
                     f.get("preis") or None, f.get("text") or "", f.get("wer") or "")


def _loeschen(book, f):
    return api.write(book, f"Leistung {f.get('id')} gelöscht", daten.delete_entry, f.get("id"))


def _rechnung(book, f):
    ids = _list(f, "ids")
    res = api.write(book, f"Leistungen verrechnet ({len(ids)} Einträge)", abrechnung.bill, ids,
                    f.get("detail") == "1", f.get("datum") or None, f.get("text") or "", None, f.get("rapport") == "1")
    return {**res, "weiter": f"/debitoren/rechnung/{res['ergebnis']['rechnung']}"}


def _projekt_neu(book, f):
    return api.write(book, f"Projekt {f.get('name')} angelegt", daten.add_project, f.get("kunde") or "",
                     f.get("name") or "", f.get("abrechnung") or "aufwand", f.get("budget_stunden") or None,
                     f.get("budget_chf") or None, f.get("satz") or None)


def _projekt_status(book, f):
    return api.write(book, f"Projekt {f.get('nummer')}: {f.get('status')}", daten.update_project, f.get("nummer"),
                     f.get("status"))


def _person(book, f):
    return api.write(book, f"Leistungen: Satz {f.get('nummer') or f.get('name')}", daten.set_person,
                     f.get("nummer") or "", f.get("satz") or None, f.get("kostensatz") or None, f.get("name") or "",
                     f.get("soll_woche") or None, int(f["vortrag_jahr"]) if f.get("vortrag_jahr") else None,
                     f.get("vortrag") or None)


def _produkt_neu(book, f):
    return api.write(book, f"Produkt {f.get('text')} erfasst", daten.add_product, f.get("text") or "",
                     f.get("preis"), f.get("einheit") or "Stk", f.get("konto") or "", f.get("mwst") or "")


def _produkt(book, f):
    aktiv = {"ja": True, "nein": False}.get(f.get("aktiv") or "")
    return api.write(book, f"Produkt {f.get('nummer')} geändert", daten.update_product, f.get("nummer"),
                     f.get("text"), f.get("preis") or None, f.get("einheit"), f.get("konto") or None,
                     f.get("mwst"), aktiv, f.get("kunde") or "", f.get("kundenpreis"))


def _feiertage(book, f):
    days = [d.strip() for d in (f.get("feiertage") or "").replace(",", "\n").splitlines() if d.strip()]
    return api.write(book, "Leistungen: Feiertage und Stundenkontrolle", daten.set_holidays, days,
                     f.get("kontrolle_ab") or "")


def _kunde(book, f):
    fields = {k: f.get(k) for k in ("name", "firma", "strasse", "nr", "plz", "ort", "email", "stundensatz")}
    return api.write(book, f"Kunde {f.get('firma') or f.get('name')} angelegt", daten.add_customer, **fields)


def _lohn(book, f):
    y, m = (int(x) for x in (f.get("monat") or "").split("-"))
    return api.write(book, f"Stunden {m:02d}/{y} in den Lohnlauf übernommen", kontrolle.transfer_to_payroll, y, m,
                     f.get("wer") or "")


# ---------- quotes ----------

def _positions(f: dict) -> list[dict]:
    cols = {k: _list(f, f"pos_{k}") for k in ("art", "produkt", "text", "menge", "preis", "einheit")}
    out = []
    for i in range(len(cols["art"])):
        row = {k: (v[i] if i < len(v) else "") for k, v in cols.items()}
        art = row["art"]
        if art == "produkt" and row["produkt"]:
            out.append({"produkt": row["produkt"], "menge": row["menge"] or 1, "text": row["text"],
                        "preis": row["preis"] or None})
        elif art == "stunden" and row["menge"]:
            out.append({"stunden": row["menge"], "text": row["text"], "preis": row["preis"] or None,
                        "wer": f.get("wer") or None})
        elif art == "frei" and row["text"]:
            out.append({"text": row["text"], "menge": row["menge"] or 1, "preis": row["preis"] or 0,
                        "einheit": row["einheit"]})
    if not out:
        raise BookError("Mindestens eine Position erfassen")
    return out


def _offerten_context(book, query):
    ctx = _common(book)
    qs = offerten.quotes(book)
    ctx["status"] = query.get("status") or ""
    ctx["offerten"] = [{**q, "offen": offerten.remaining(book, q) if q["status"] == "angenommen" else None}
                       for q in sorted(qs.values(), key=lambda q: q["nummer"], reverse=True)
                       if not ctx["status"] or q["status"] == ctx["status"]]
    nr = query.get("nr") or ""
    if nr in qs:
        q = qs[nr]
        ctx["sel"] = {**q, "offen": offerten.remaining(book, q), "verrechnet": offerten.billed(book, q),
                      "pdf": book.rel(q["_pfad"].with_suffix(".pdf"))}
    ctx["neu_kunde"] = query.get("kunde") or ""
    ctx["abgelaufen"] = set(offerten.expired(book))
    return ctx


def _offerte_neu(book, f):
    res = api.write(book, f"Offerte für {f.get('kunde')} erstellt", offerten.create, f.get("kunde") or "",
                    _positions(f), f.get("datum") or None, f.get("titel") or "", f.get("text") or "",
                    int(f["gueltig"]) if f.get("gueltig") else None, f.get("abrechnung") or "pauschal")
    return {**res, "weiter": f"/p/leistungen/offerten?nr={res['ergebnis']['nummer']}"}


def _offerte_status(book, f):
    return api.write(book, f"Offerte {f.get('nummer')}: {f.get('status')}", offerten.set_status, f.get("nummer"),
                     f.get("status"), f.get("projektname") or "")


def _offerte_rechnung(book, f):
    art = f.get("art") or "voll"
    res = api.write(book, f"Rechnung zu Offerte {f.get('nummer')}", offerten.invoice, f.get("nummer"),
                    f.get("anteil") if art == "teil" else None, f.get("betrag") if art == "betrag" else None,
                    art == "schluss", f.get("datum") or None)
    return {**res, "weiter": f"/debitoren/rechnung/{res['ergebnis']['rechnung']}"}


PAGES = [
    Page("leistungen", "Leistungen", "leistungen.html", _context,
         {"zeit": _zeit, "material": _material, "loeschen": _loeschen, "rechnung": _rechnung,
          "projekt_neu": _projekt_neu, "projekt_status": _projekt_status, "person": _person,
          "produkt_neu": _produkt_neu, "produkt": _produkt, "feiertage": _feiertage, "kunde": _kunde, "lohn": _lohn}),
    Page("offerten", "Offerten", "offerten.html", _offerten_context,
         {"neu": _offerte_neu, "status": _offerte_status, "rechnung": _offerte_rechnung}),
]

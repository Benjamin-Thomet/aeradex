"""Pages in the UI: «Leistungen» (Erfassen, Abrechnen, Projekte, Kontrolle, Stammdaten) and «Offerten»."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from aeradex import api, invoices
from aeradex.book import BookError
from aeradex.plugins import Page

from . import (abrechnung, abwesenheit, azk, daten, entwuerfe, kontrolle, offerten, rapporte, schnell, stoppuhr,
               woche, woche_rapport)

TABS = [("erfassen", "Erfassen"), ("woche", "Woche"), ("abwesenheiten", "Abwesenheiten"),
        ("auswertung", "Auswertung"), ("stammdaten", "Stammdaten")]
HIDDEN_TABS = {"abrechnen"}          # still reachable (all customers at once), linked from «Erfassen»
ALIAS = {"projekte": "auswertung", "kontrolle": "auswertung"}
STATUS_FILTER = {"offen": "nicht abgerechnet", "abgerechnet": "abgerechnet", "": "alle"}


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
    people = daten.people(book)
    return {"tabs": TABS, "kunden": {k: invoices.qr.invoice_name(c) for k, c in custs.items()},
            "personen": people, "produkte": daten.products(book), "projekte": daten.projects(book),
            "leistungsarten": daten.services(book), "material": daten.materials(book),
            "cfg": daten.settings(book), "abwesenheit_arten": list(abwesenheit.ARTEN)}


def _wer(query: dict, people: dict) -> str:
    wer = str(query.get("wer") or "").upper()
    if wer in people:
        return wer
    return next((k for k, p in people.items() if p.get("aktiv", True)), "")


def _week_start(query: dict) -> date:
    raw = query.get("woche") or ""
    try:
        return woche.monday(date.fromisoformat(raw)) if raw else woche.monday(date.today())
    except ValueError:
        return woche.monday(date.today())


def _woche_ctx(book, query) -> dict:
    """Everything the week view (and its fragment) shows: grid, timer, recent, favourites, entries."""
    ctx = _common(book)
    wer = _wer(query, ctx["personen"])
    start = _week_start(query)
    ctx.update(wer=wer, woche_start=start, vorwoche=start - timedelta(days=7), naechste=start + timedelta(days=7),
               heute=date.today(), stoppuhren=stoppuhr.running(book))
    if not wer:
        return ctx
    ctx["grid"] = woche.grid(book, wer, start, with_previous=query.get("vorwoche") == "1")
    ctx["zuletzt"] = woche.recent(book, wer)
    ctx["favoriten"] = woche.favourites(book, wer)
    fav_ids = {f["id"] for f in ctx["favoriten"]}
    for r in ctx["zuletzt"]:
        r["favorit"] = r["id"] in fav_ids
    ctx["eintraege"] = sorted(abrechnung.select(book, wer=wer, von=start, bis=start + timedelta(days=6), status=""),
                              key=lambda e: (e["datum"], e["id"]), reverse=True)
    t = ctx["stoppuhren"].get(wer)
    if t:
        ctx["stoppuhr"] = {**t, "minuten": stoppuhr.elapsed_minutes(t),
                           "label": woche.combo(book, (t["kunde"], t["projekt"], t["leistung"], t["abrechenbar"],
                                                       t["kategorie"], t["text"]))}
    return ctx


def _vorschau_ctx(book, query) -> dict:
    ctx = _common(book)
    text = (query.get("text") or "").strip()
    ctx["text"] = text
    ctx["heute"] = date.today()
    if text:
        today = date.fromisoformat(query["datum"]) if query.get("datum") else None
        ctx["p"] = schnell.parse(book, text, _wer(query, ctx["personen"]), today)
    return ctx


def _abrechnen_ctx(book, query) -> dict:
    ctx = _common(book)
    bis = query.get("bis") or ""
    ctx["bis"] = bis
    ctx["karten"] = abrechnung.summary(book, bis or None)
    ctx["gruppen"] = abrechnung.groups(book, bis or None, ctx["cfg"]["abrechnen_je"])
    ctx["total_offen"] = sum((k["betrag"] for k in ctx["karten"]), Decimal(0))
    kunde = query.get("kunde") or ""
    ctx.update(kunde=kunde, projekt=query.get("projekt") or "", von=query.get("von") or "",
               detail=query.get("detail") == "1")
    if kunde:
        items = abrechnung.select(book, kunde, ctx["projekt"], ctx["von"] or None, bis or None)
        ctx["auswahl"] = items
        try:
            ctx["vorschau"] = abrechnung.preview(book, [e["id"] for e in items], ctx["detail"]) if items else None
        except BookError as exc:
            ctx["vorschau_fehler"] = str(exc)
    return ctx


def _kalender_ctx(book, query) -> dict:
    ctx = _common(book)
    wer = _wer(query, ctx["personen"])
    year = int(query.get("jahr") or date.today().year)
    ctx.update(wer=wer, jahr=year, heute=date.today())
    if not wer:
        return ctx
    person = ctx["personen"][wer]
    hols = abwesenheit.holiday_names(book, year)
    rows = [a for a in abwesenheit.items(book) if a["wer"] == wer]
    months = []
    for m in range(1, 13):
        days = []
        for d in range(1, calendar.monthrange(year, m)[1] + 1):
            day = date(year, m, d)
            a = abwesenheit.on_day(book, wer, day, rows)
            days.append({"datum": day, "wochenende": day.weekday() >= 5, "feiertag": hols.get(day, ""),
                         "art": a["art"] if a else "", "farbe": abwesenheit.FARBEN.get(a["art"], "") if a else "",
                         "halb": bool(a and a["anteil"] < 1), "id": a["id"] if a else ""})
        months.append({"monat": m, "tage": days})
    ctx.update(monate=months, feiertage=hols, abwesenheiten=[a for a in rows if a["von"].year <= year <= a["bis"].year],
               ferien=abwesenheit.holiday_account(book, wer, year), person=person,
               kanton=abwesenheit.canton(book),
               team=[abwesenheit.holiday_account(book, nr, year) for nr, p in ctx["personen"].items() if p.get("aktiv", True)])
    return ctx


def _erfassen_ctx(book, query) -> dict:
    """«Erfassen»: one form for hours and products per customer, and every entry, filtered and grouped by
    customer, ready to bill per customer."""
    ctx = _common(book)
    status = query.get("status", "offen")
    status = status if status in STATUS_FILTER else "offen"
    f = {"status": status, "kunde": (query.get("kunde") or "").upper(), "von": query.get("von") or "",
         "bis": query.get("bis") or "", "art": query.get("art") or ""}
    try:
        items = abrechnung.select(book, kunde=f["kunde"], von=f["von"] or None, bis=f["bis"] or None,
                                  art=f["art"], status=status)
    except BookError:
        items = []
    if not status:                                   # «alle»: billable work only, newest first
        items = [e for e in items if e["status"] in ("offen", "abgerechnet")]
    groups: dict[str, dict] = {}
    for e in sorted(items, key=lambda e: (e["datum"], e["id"]), reverse=True):
        g = groups.setdefault(e["kunde"], {"kunde": e["kunde"], "eintraege": [], "betrag": Decimal(0),
                                           "stunden": Decimal(0), "offen": 0})
        g["eintraege"].append(e)
        g["betrag"] += e["betrag"]
        if e["art"] == "Zeit":
            g["stunden"] += e["menge"]
        if e["status"] == "offen":
            g["offen"] += 1
    ctx["gruppen"] = sorted(groups.values(), key=lambda g: ctx["kunden"].get(g["kunde"], g["kunde"]).lower())
    ctx["filter"] = f
    ctx["status_filter"] = STATUS_FILTER
    ctx["total"] = sum((g["betrag"] for g in groups.values()), Decimal(0))
    ctx["wer"] = _wer(query, ctx["personen"])
    ctx["heute"] = date.today().isoformat()
    ctx["neu_kunde"] = (query.get("neu_kunde") or f["kunde"] or "").upper()
    return ctx


def _neu(book, f):
    """One entry from the «Erfassen» form: a Leistungsart (hours), plain hours, or a product."""
    was = str(f.get("was") or "").strip().upper()
    kunde = str(f.get("kunde") or "").strip().upper()
    if not kunde:
        raise BookError("Kunde wählen")
    datum = f.get("datum") or date.today().isoformat()
    menge = f.get("menge") or ""
    preis = f.get("preis") or None
    text = f.get("text") or ""
    if was in ("", "ZEIT") or daten.is_service(daten.product(book, was)):
        res = api.write(book, f"Zeit erfasst für {kunde}", daten.add_time, datum, f.get("wer") or "", menge, text,
                        kunde, "", True, "", preis, "" if was in ("", "ZEIT") else was)
    else:
        res = api.write(book, f"Produkt erfasst für {kunde}", daten.add_material, datum, kunde, was, menge, "",
                        preis, text, f.get("wer") or "")
    e = res["ergebnis"]
    return {**res, "meldung": f"Erfasst: {e['text']} · {daten.num(Decimal(str(e['menge'])))}",
            "weiter": f"/p/leistungen/leistungen?tab=erfassen&neu_kunde={kunde}"}


def _context(book, query):
    tab = ALIAS.get(query.get("tab") or "", query.get("tab") or "erfassen")
    if tab not in dict(TABS) and tab not in HIDDEN_TABS:
        tab = "erfassen"
    y, m = _month(query)
    if tab == "erfassen":
        ctx = _erfassen_ctx(book, query)
    elif tab == "woche":
        ctx = _woche_ctx(book, query)
    elif tab == "abrechnen":
        ctx = _abrechnen_ctx(book, query)
    elif tab == "abwesenheiten":
        ctx = _kalender_ctx(book, query)
    else:
        ctx = _common(book)
    ctx["tab"] = tab
    ctx["monat"] = f"{y}-{m:02d}"
    if tab == "auswertung":
        ctx["status"] = query.get("status") or "offen"
        ctx["liste"] = [kontrolle.project(book, nr) for nr, p in ctx["projekte"].items()
                        if ctx["status"] == "alle" or p.get("status") == ctx["status"]]
        ctx["zeilen"] = kontrolle.month(book, y, m)
        ctx["wer"] = query.get("wer") or ""
        if ctx["wer"]:
            ctx["jahr"] = kontrolle.year(book, y, ctx["wer"])
    return ctx


def _week(book, f, meldung: str, extra: dict | None = None) -> dict:
    """An action's answer: the week fragment again (no page reload)."""
    q = {"wer": f.get("wer") or "", "woche": f.get("woche") or ""}
    return {"fragment": "woche", "query": q, "meldung": meldung, **(extra or {})}


# ---------- fast entry ----------

def _schnell(book, f):
    today = date.fromisoformat(f["datum"]) if f.get("datum") else None
    res = api.write(book, f"Schnell erfasst: {(f.get('text') or '').strip()}", schnell.save, f.get("text") or "",
                    f.get("wer") or "", today)
    e = res["ergebnis"]
    unit = "h" if e["art"] == "Zeit" else daten.products(book).get(e["produkt"], {}).get("einheit", "")
    return _week(book, f, f"Erfasst: {e['text']} · {daten.num(Decimal(str(e['menge'])))} {unit}".strip())


def _start(book, f):
    if (f.get("text") or "").strip() and not f.get("kunde") and not f.get("projekt"):
        p = schnell.parse(book, f["text"], f.get("wer") or "")
        if p["kandidaten"]:
            raise BookError("Nicht eindeutig: " + ", ".join(f"{k} {n}" for k, n in next(iter(p["kandidaten"].values()))))
        args = dict(kunde=p["kunde"], projekt=p["projekt"], leistung=p["leistung"], text=p["text"],
                    abrechenbar=p["abrechenbar"], kategorie=p["kategorie"])
        wer = p["wer"] or f.get("wer") or ""
    else:
        args = dict(kunde=f.get("kunde") or "", projekt=f.get("projekt") or "", leistung=f.get("leistung") or "",
                    text=f.get("text") or "", abrechenbar=f.get("abrechenbar", "True") in ("True", "1", "ja", "true"),
                    kategorie=f.get("kategorie") or "")
        wer = f.get("wer") or ""
    res = stoppuhr.start(book, wer, **args)
    stopped = res.get("gestoppt")
    msg = "Stoppuhr läuft" + (f" · vorherige gespeichert ({stopped['stunden']} h)" if stopped and not stopped.get("verworfen") else "")
    return _week(book, {**f, "wer": res["wer"]}, msg)


def _stop(book, f):
    res = stoppuhr.stop(book, f.get("wer") or "", f.get("text") if f.get("text") is not None else None)
    return _week(book, f, res.get("meldung") or f"{res['stunden']} h erfasst")


def _verwerfen(book, f):
    stoppuhr.discard(book, f.get("wer") or "")
    return _week(book, f, "Stoppuhr verworfen")


def _zelle(book, f):
    abr = f.get("abrechenbar", "True") in ("True", "1", "ja", "true")
    api.write(book, f"Woche: {f.get('datum')} {f.get('stunden') or 0} h", woche.set_cell, f.get("wer") or "",
              f.get("datum"), f.get("stunden") or "", f.get("kunde") or "", f.get("projekt") or "",
              f.get("leistung") or "", abr, f.get("kategorie") or "", f.get("text") or "")
    return _week(book, f, "")


def _favorit(book, f):
    res = api.write(book, "Leistungen: Favorit", woche.toggle_favourite, f.get("wer") or "", f.get("combo") or "")
    return _week(book, f, "Als Favorit gemerkt" if res["ergebnis"]["favorit"] else "Favorit entfernt")


def _eintrag(book, f):
    fields = {k: f.get(k) for k in ("datum", "menge", "text", "preis") if f.get(k) not in (None, "")}
    for k in ("kunde", "projekt", "leistung"):
        if f.get(k) is not None:
            fields[k] = f.get(k)
    api.write(book, f"Leistung {f.get('id')} geändert", daten.update_entry, f.get("id"), **fields)
    return _week(book, f, "Gespeichert")


def _aendern(book, f):
    """Change an open entry from the list in «Erfassen» (the page reloads with it). Another project brings its
    customer along; another customer drops a project of the old one; moved elsewhere, the rate is determined
    again unless a price was typed in."""
    e = daten.entry(book, f.get("id") or "")
    fields = {k: f.get(k) for k in ("datum", "menge", "text") if f.get(k) not in (None, "")}
    kunde = str(f.get("kunde") if f.get("kunde") is not None else e["kunde"]).strip().upper()
    projekt = str(f.get("projekt") if f.get("projekt") is not None else e["projekt"]).strip().upper()
    leistung = str(f.get("leistung") if f.get("leistung") is not None else e["produkt"]).strip().upper()
    if e["abrechenbar"]:
        if projekt != e["projekt"] and projekt:
            kunde = daten.project(book, projekt)["kunde"]
        elif kunde != e["kunde"] and projekt and daten.project(book, projekt)["kunde"] != kunde:
            projekt = ""
        moved = (kunde, projekt) != (e["kunde"], e["projekt"]) or (e["art"] == "Zeit" and leistung != e["produkt"])
        fields.update(kunde=kunde, projekt=projekt)
        if e["art"] == "Zeit":
            fields["leistung"] = leistung
        price = str(f.get("preis") or "").strip()
        try:
            same_price = e["preis"] is not None and bool(price) and daten.money(price.replace(",", ".")) == e["preis"]
        except ArithmeticError:
            same_price = False                     # not a number: update_entry says so
        if moved and (not price or same_price):
            fields["neu_bewerten"] = True
        elif price:
            fields["preis"] = price
    return api.write(book, f"Leistung {e['id']} geändert", daten.update_entry, e["id"], **fields)


def _kopieren(book, f):
    e = daten.entry(book, f.get("id") or "")
    d = f.get("datum") or date.today().isoformat()
    if e["art"] == "Zeit":
        api.write(book, f"Leistung {e['id']} kopiert", daten.add_time, d, e["wer"], e["menge"], e["text"], e["kunde"],
                  e["projekt"], e["abrechenbar"], e["kategorie"], None, e["produkt"])
    else:
        api.write(book, f"Leistung {e['id']} kopiert", daten.add_material, d, e["kunde"], e["produkt"], e["menge"],
                  e["projekt"], None, e["text"], e["wer"])
    return _week(book, f, f"Kopiert auf {d}")


def _loeschen_schnell(book, f):
    api.write(book, f"Leistung {f.get('id')} gelöscht", daten.delete_entry, f.get("id"))
    return _week(book, f, "Gelöscht")


def _verschieben(book, f):
    ids = _list(f, "ids")
    if not ids:
        raise BookError("Keine Einträge ausgewählt")
    fields = {}
    if f.get("projekt"):
        fields["projekt"] = f["projekt"]
        fields["kunde"] = ""
    elif f.get("kunde"):
        fields["kunde"], fields["projekt"] = f["kunde"], ""
    if f.get("leistung") is not None and f.get("leistung") != "__":
        fields["leistung"] = f["leistung"]
    if not fields:
        raise BookError("Ziel wählen: Projekt, Kunde oder Leistungsart")
    fields["neu_bewerten"] = f.get("neu_bewerten") == "1"

    def move(book_):
        touched = []
        for i in ids:
            _, paths = daten.update_entry(book_, i, **fields)
            touched += paths
        return {"anzahl": len(ids)}, touched
    api.write(book, f"{len(ids)} Leistungen verschoben", move)
    return _week(book, f, f"{len(ids)} Einträge verschoben")


# ---------- absences ----------

def _abwesenheit(book, f):
    res = api.write(book, f"Abwesenheit {f.get('art')} {f.get('von')}", abwesenheit.add, f.get("wer") or "",
                    f.get("art") or "", f.get("von"), f.get("bis") or None, f.get("anteil") or 1, f.get("notiz") or "")
    return {"fragment": "kalender", "query": {"wer": f.get("wer") or "", "jahr": f.get("jahr") or ""},
            "meldung": f"{res['ergebnis']['art']} eingetragen"}


def _abwesenheit_loeschen(book, f):
    api.write(book, f"Abwesenheit {f.get('id')} gelöscht", abwesenheit.delete, f.get("id") or "")
    return {"fragment": "kalender", "query": {"wer": f.get("wer") or "", "jahr": f.get("jahr") or ""},
            "meldung": "Abwesenheit gelöscht"}


# ---------- classic forms (kept: CLI-like, also used by the tests and older links) ----------

def _zeit(book, f):
    abr = f.get("abrechenbar") == "ja"
    return api.write(book, f"Zeit {f.get('stunden')} h erfasst", daten.add_time, f.get("datum"), f.get("wer") or "",
                     f.get("stunden"), f.get("text") or "", f.get("kunde") or "", f.get("projekt") or "", abr,
                     "" if abr else (f.get("kategorie") or ""), f.get("satz") or None, f.get("leistung") or "")


def _material(book, f):
    return api.write(book, f"Produkt {f.get('produkt')} erfasst", daten.add_material, f.get("datum"),
                     f.get("kunde") or "", f.get("produkt") or "", f.get("menge"), f.get("projekt") or "",
                     f.get("preis") or None, f.get("text") or "", f.get("wer") or "")


def _loeschen(book, f):
    return api.write(book, f"Leistung {f.get('id')} gelöscht", daten.delete_entry, f.get("id"))


def _rechnung(book, f):
    ids = _list(f, "ids")
    res = api.write(book, f"Leistungen verrechnet ({len(ids)} Einträge)", abrechnung.bill, ids,
                    f.get("detail") == "1", f.get("datum") or None, f.get("text") or "", None, f.get("rapport") == "1",
                    f.get("rabatt") or None, _list(f, "akonto"))
    return {**res, "weiter": f"/debitoren/rechnung/{res['ergebnis']['rechnung']}"}


def _alle_abrechnen(book, f):
    cfg = daten.settings(book)
    res = api.write(book, f"Leistungen abgerechnet bis {f.get('bis') or 'heute'}", abrechnung.bill_all,
                    f.get("bis") or None, f.get("je") or cfg["abrechnen_je"], f.get("datum") or None, True)
    n = res["ergebnis"]["anzahl"]
    total = Decimal(str(res["ergebnis"]["total"]))
    warn = " ".join(res["ergebnis"].get("warnungen") or [])
    return {**res, "meldung": f"{n} Rechnung(en) über CHF {total:,.2f} ausgestellt".replace(",", "'")
            + (f". {warn}" if warn else ""),
            "weiter": "/debitoren?status=offen"}


def _projekt_neu(book, f):
    word = daten.settings(book)["projekt_wort"]
    label = f.get("name") or " ".join(x for x in (f.get("strasse"), f.get("hausnr"), f.get("ort")) if x)
    return api.write(book, f"{word} {label} angelegt", daten.add_project, f.get("kunde") or "",
                     f.get("name") or "", f.get("abrechnung") or "aufwand", f.get("budget_stunden") or None,
                     f.get("budget_chf") or None, f.get("satz") or None, strasse=f.get("strasse") or "",
                     hausnr=f.get("hausnr") or "", plz=f.get("plz") or "", ort=f.get("ort") or "",
                     zugang=f.get("zugang") or "")


def _projekt_aendern(book, f):
    word = daten.settings(book)["projekt_wort"]
    fields = {k: f.get(k) for k in ("name", "strasse", "hausnr", "plz", "ort", "zugang", "notizen") if k in f}
    return api.write(book, f"{word} {f.get('nummer')} geändert", daten.update_project, f.get("nummer"), None,
                     **fields)


def _projekt_status(book, f):
    return api.write(book, f"Projekt {f.get('nummer')}: {f.get('status')}", daten.update_project, f.get("nummer"),
                     f.get("status"))


def _person(book, f):
    res = api.write(book, f"Leistungen: Satz {f.get('nummer') or f.get('name')}", daten.set_person,
                    f.get("nummer") or "", f.get("satz") or None, f.get("kostensatz") or None, f.get("name") or "",
                    f.get("soll_woche") or None, int(f["vortrag_jahr"]) if f.get("vortrag_jahr") else None,
                    f.get("vortrag") or None, f.get("ferien_tage") or None,
                    int(f["vortrag_jahr"]) if f.get("vortrag_jahr") else None, f.get("ferien_vortrag") or None)
    return res


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


def _einstellungen(book, f):
    return api.write(book, "Leistungen: Einstellungen", daten.set_options, rundung=f.get("rundung"),
                     ferien_tage=f.get("ferien_tage"), feiertage_kanton=f.get("feiertage_kanton"),
                     abrechnen_je=f.get("abrechnen_je"), projekt_wort=f.get("projekt_wort"),
                     projekt_pflicht=f.get("projekt_pflicht"), rechnung_tage=f.get("rechnung_tage"))


def _nachweis(book, f):
    """The Arbeitszeitnachweis of a person and month as PDF (in berichte/, not in git), opened directly."""
    y, m = (int(x) for x in (f.get("monat") or date.today().strftime("%Y-%m")).split("-"))
    p = daten.person(book, f.get("wer") or "")
    data = abwesenheit.time_record_pdf(book, p["nummer"], y, m)
    target = book.root / "berichte" / f"Arbeitszeitnachweis {p['name']} {y}-{m:02d}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    from urllib.parse import quote
    return {"meldung": "Arbeitszeitnachweis erstellt", "weiter": "/datei/" + quote(book.rel(target))}


def _kunde(book, f):
    fields = {k: f.get(k) for k in ("name", "firma", "strasse", "nr", "plz", "ort", "email", "telefon", "mobil", "stundensatz")}
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


# ---------- invoice drafts ----------

def _entwuerfe_context(book, query):
    ctx = _common(book)
    ctx["entwuerfe"] = entwuerfe.overview(book)
    nr = (query.get("nr") or "").upper()
    if nr:
        ctx["sel"] = entwuerfe.view(book, nr)
        ctx["materialien"] = daten.materials(book)
        ctx["dazu"] = [e for e in abrechnung.select(book, ctx["sel"]["kunde"])
                       if e["id"] not in set(ctx["sel"]["eintraege"]) and e["id"] not in entwuerfe.in_drafts(book)]
    return ctx


def _back(nr: str, meldung: str, res: dict | None = None) -> dict:
    return {**(res or {}), "meldung": meldung, "weiter": f"/p/leistungen/entwuerfe?nr={nr}"}


def _entwurf_neu(book, f):
    res = api.write(book, f"Rechnungsentwurf für {f.get('kunde')}", entwuerfe.add, f.get("kunde") or "",
                    _list(f, "ids"), f.get("projekt") or "")
    return _back(res["ergebnis"]["nummer"], "Entwurf angelegt", res)


def _entwurf_preis(book, f):
    """Price of one entry; with `sortiment` also the product's (or Leistungsart's) price for later entries."""
    nr, eid = f.get("nummer") or "", f.get("id") or ""
    preis = (f.get("preis") or "").strip()
    if not preis:
        raise BookError("Preis eingeben")

    def run(b):
        e = daten.entry(b, eid)
        touched = []
        out, paths = daten.update_entry(b, eid, preis=preis)
        touched += paths
        if f.get("sortiment") != "1":
            return out, touched
        # remember it: the person's rate (hours) or the product's price (material), and fill in the draft's other
        # entries of the same person or product that are still without a price
        if e["art"] == "Zeit":
            _, paths = daten.set_person(b, e["wer"], preis)
        else:
            _, paths = daten.update_product(b, e["produkt"], preis=preis)
        touched += paths
        open_ids = [i for d in entwuerfe.drafts(b).values() if d.get("status", "offen") == "offen"
                    for i in d["eintraege"] if i != eid]           # every open draft, not only this one
        if e["art"] == "Zeit":
            touched += entwuerfe.fill_prices(b, open_ids, wer=e["wer"])
        else:
            touched += entwuerfe.fill_prices(b, open_ids, produkt=e["produkt"])
        return out, touched
    api.write(book, f"{nr}: Preis {eid}", run)
    return _back(nr, "Preis gesetzt")


def _entwurf_aendern(book, f):
    nr = f.get("nummer") or ""
    api.write(book, f"{nr} geändert", entwuerfe.update, nr, f.get("text") if "text" in f else None,
              f.get("rabatt") if "rabatt" in f else None, _list(f, "entfernen") or None, _list(f, "hinzufuegen") or None)
    return _back(nr, "Gespeichert")


def _entwurf_ausstellen(book, f):
    nr = f.get("nummer") or ""
    res = api.write(book, f"{nr}: Rechnung ausgestellt", entwuerfe.issue, nr, f.get("datum") or None)
    return {**res, "weiter": f"/debitoren/rechnung/{res['ergebnis']['rechnung']}"}


def _entwurf_verwerfen(book, f):
    nr = f.get("nummer") or ""
    api.write(book, f"{nr} verworfen", entwuerfe.discard, nr)
    return {"meldung": "Entwurf verworfen, die Einträge bleiben offen", "weiter": "/p/leistungen/entwuerfe"}


# ---------- scanned reports ----------

def _rapporte_context(book, query):
    ctx = _common(book)
    alle = sorted(rapporte.reports(book).values(), key=lambda r: (r.get("eingelesen") or "", r["nummer"]), reverse=True)
    status = query.get("status") or "ausgelesen"
    ctx["status"] = status
    ctx["liste"] = [r for r in alle if not status or r.get("status") == status]
    ctx["anzahl"] = {s: sum(1 for r in alle if r.get("status") == s) for s in rapporte.STATUS}
    ctx["arten"] = rapporte.ARTEN
    ctx["objekte"] = {k: p for k, p in ctx["projekte"].items() if p.get("status", "offen") == "offen"}
    ziel = query.get("ziel") or ""                  # from the customer's page: print for this customer
    ctx["druck_kunde"], ctx["druck_projekt"] = (ziel[2:], "") if ziel.startswith("K:") else ("", "")
    today = date.today()
    jahr, kw = (int(x) for x in (query.get("kw") or f"{today.isocalendar()[0]}-{today.isocalendar()[1]}").split("-"))
    ctx["kw"] = {"jahr": jahr, "kw": kw, "zeilen": woche_rapport.overview(book, jahr, kw),
                 "vorher": "{}-{}".format(*(date.fromisocalendar(jahr, kw, 1) - timedelta(days=7)).isocalendar()[:2]),
                 "nachher": "{}-{}".format(*(date.fromisocalendar(jahr, kw, 1) + timedelta(days=7)).isocalendar()[:2])}
    nr = query.get("nr") or ""
    if nr:
        r = rapporte.report(book, nr)
        r.pop("_pfad", None)
        ctx["sel"] = r
        ctx["probleme"] = rapporte.problems(book, r) if r.get("status") == "ausgelesen" else []
        ctx["ist_pdf"] = str(r.get("scan") or "").lower().endswith(".pdf")
        if r["art"] == "woche":
            for t in r.get("tage") or []:
                for line in t.get("zeilen") or []:
                    line["_art"] = woche_rapport.kind_of(book, line)
                    line["_kategorie"] = line.get("kategorie") or (woche_rapport.category_of(book, line)
                                                                   if line["_art"] == "intern" else "")
                    line["_absenz"] = line.get("absenz") or woche_rapport.absence_of(line)
    return ctx


def _rows(f: dict, prefix: str, keys: tuple[str, ...]) -> list[dict]:
    cols = {k: _list(f, f"{prefix}_{k}") for k in keys}
    n = max((len(v) for v in cols.values()), default=0)
    return [{k: (cols[k][i] if i < len(cols[k]) else "").strip() for k in keys} for i in range(n)]


def _ziel(book, value: str) -> dict:
    """The target chosen for a week line: «P:P0003» object (with its customer), «K:K0002» customer,
    «C:Werkstatt» category, «A:Ferien» absence."""
    kind, _, val = (value or "").partition(":")
    kunde = val if kind == "K" else (daten.project(book, val)["kunde"] if kind == "P" else "")
    return {"kunde": kunde, "projekt": val if kind == "P" else "",
            "kategorie": val if kind == "C" else "", "absenz": val if kind == "A" else ""}


def _rapport_fields(book, f) -> dict:
    meta = rapporte.report(book, f.get("nummer") or "")
    art = meta["art"]
    out: dict = {}
    if art in ("regie", "material"):
        ziel = f.get("ziel") or ""
        if ziel.startswith("P:"):
            out["projekt"] = ziel[2:]
            out["kunde"] = daten.project(book, ziel[2:])["kunde"]
        else:
            out["kunde"], out["projekt"] = ziel[2:] if ziel.startswith("K:") else "", ""
        out["unterschrieben_kunde"] = f.get("unterschrieben_kunde") == "1"
    if art == "regie":
        out["arbeiten"] = [l.strip() for l in (f.get("arbeiten") or "").splitlines() if l.strip()]
        out["stunden"] = [r for r in _rows(f, "s", ("person", "datum", "stunden", "ansatz"))
                          if r["stunden"] or r["person"]]
    elif art == "material":
        out["zeilen"] = [r for r in _rows(f, "z", ("datum", "produkt", "text", "einheit", "menge", "preis"))
                         if r["text"] or r["produkt"]]
        prods = daten.products(book)
        for r in out["zeilen"]:
            if r["produkt"] and not r["text"]:
                r["text"] = prods.get(r["produkt"], {}).get("text", "")
    elif art == "woche":
        out["person"] = f.get("person") or ""
        days = {r["datum"]: {"datum": r["datum"], "reisezeit": r["reise"], "zeilen": []}
                for r in _rows(f, "t", ("datum", "reise")) if r["datum"]}
        for r in _rows(f, "l", ("datum", "text", "ziel", "stunden", "bemerkung")):
            if r["datum"] not in days or not (r["text"] or r["stunden"] or r["ziel"]):
                continue
            days[r["datum"]]["zeilen"].append({"text": r["text"], "stunden": r["stunden"],
                                              "bemerkung": r["bemerkung"], **_ziel(book, r["ziel"])})
        out["tage"] = sorted(days.values(), key=lambda t: t["datum"])
    return out


def _rapport_speichern(book, f):
    nr = f.get("nummer") or ""
    api.write(book, f"Rapport {nr} korrigiert", rapporte.save, nr, _rapport_fields(book, f))
    return {"meldung": "Gespeichert", "weiter": f"/p/leistungen/rapporte?nr={nr}"}


def _rapport_uebernehmen(book, f):
    nr = f.get("nummer") or ""
    fields = _rapport_fields(book, f)

    def run(b):
        _, touched = rapporte.save(b, nr, fields)
        res, more = rapporte.take_over(b, nr)
        return res, touched + more
    res = api.write(book, f"Rapport {nr} übernommen", run)["ergebnis"]
    if res.get("entwurf"):
        return {"meldung": f"Übernommen in Rechnungsentwurf {res['entwurf']}",
                "weiter": f"/p/leistungen/entwuerfe?nr={res['entwurf']}"}
    return {"meldung": f"{len(res.get('eintraege') or [])} Einträge übernommen",
            "weiter": f"/p/leistungen/rapporte?nr={nr}"}


def _rapport_verwerfen(book, f):
    nr = f.get("nummer") or ""
    api.write(book, f"Rapport {nr} verworfen", rapporte.discard, nr)
    return {"meldung": "Verworfen", "weiter": "/p/leistungen/rapporte"}


def _rapport_hochladen(book, f):
    upload = f.get("datei")
    if upload is None or not getattr(upload, "filename", ""):
        raise BookError("Scan wählen")
    stored = api.inbox_add(book, upload.filename, upload.file.read())
    res = rapporte.take_in(book, book.root / stored["datei"], f.get("art") or "")
    return {"meldung": res["meldung"], "weiter": res["url"]}


def _formulare(book, f):
    """Printed forms with number and QR (formulare.py), as one PDF to print."""
    from . import formulare
    art = f.get("art") or "regie"
    if art == "woche":
        res = api.write(book, "Wochenrapporte gedruckt", formulare.weeks, f.get("kw") or "", _list(f, "personen"))
    else:
        res = api.write(book, f"{rapporte.ARTEN[art]} gedruckt", formulare.reports, art, f.get("ziel") or "",
                        int(f.get("anzahl") or 1))
    from urllib.parse import quote
    return {"meldung": res["ergebnis"]["meldung"], "weiter": "/datei/" + quote(res["ergebnis"]["datei"])}


# ---------- SMGV Arbeitszeitkontrolle ----------

def _azk_context(book, query):
    ctx = _common(book)
    years = book.years()
    default = date.today().year if not years or date.today().year in years else max(years)
    jahr = int(query.get("jahr") or default)
    ctx["jahr"] = jahr
    ctx["vorlagen"] = {y: book.rel(p) for y, p in sorted(azk.templates(book).items())}
    ctx["abgeleitet"] = jahr not in ctx["vorlagen"] and any(y < jahr for y in ctx["vorlagen"])
    wer = (query.get("wer") or "").upper()
    rows = []
    for nr, p in ctx["personen"].items():
        if not p.get("aktiv", True):
            continue
        months = azk.summary(book, nr, jahr)
        rows.append({"nummer": nr, "name": p["name"], "monate": months,
                     "arbeit": sum((m["arbeit"] for m in months), Decimal(0)),
                     "reise": sum((m["reise"] for m in months), Decimal(0)),
                     "absenzen": sum((m["absenzen"] for m in months), Decimal(0))})
    ctx["zeilen"] = rows
    ctx["sel"] = next((r for r in rows if r["nummer"] == wer), None)
    return ctx


def _azk_vorlage(book, f):
    upload = f.get("datei")
    if upload is None or not getattr(upload, "filename", ""):
        raise BookError("Excel-Datei des SMGV wählen")
    res = api.write(book, "Arbeitszeitkontrolle: Vorlage", azk.store_template, upload.file.read(),
                    int(f["jahr"]) if f.get("jahr") else None)["ergebnis"]
    return {"meldung": f"Vorlage {res['jahr']} ({res['variante']}) gespeichert"
            + (" — aus der Vorlage des Vorjahrs erstellt" if res.get("abgeleitet") else ""),
            "weiter": f"/p/leistungen/arbeitszeit?jahr={res['jahr']}"}


def _azk_excel(book, f):
    jahr = int(f.get("jahr") or date.today().year)
    p = daten.person(book, f.get("wer") or "")
    data = azk.fill(book, p["nummer"], jahr)
    target = book.root / "berichte" / "Arbeitszeitkontrolle" / f"Arbeitszeitkontrolle {jahr} {p['name']}.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    from urllib.parse import quote
    return {"meldung": "Arbeitszeitkontrolle erstellt", "weiter": "/datei/" + quote(book.rel(target))}


PAGES = [
    Page("leistungen", "Leistungen", "leistungen.html", _context,
         {"neu": _neu, "zeit": _zeit, "material": _material, "loeschen": _loeschen, "rechnung": _rechnung,
          "projekt_neu": _projekt_neu, "projekt_aendern": _projekt_aendern, "projekt_status": _projekt_status, "person": _person,
          "produkt_neu": _produkt_neu, "produkt": _produkt, "feiertage": _feiertage, "kunde": _kunde, "lohn": _lohn,
          "schnell": _schnell, "start": _start, "stop": _stop, "verwerfen": _verwerfen, "zelle": _zelle,
          "favorit": _favorit, "eintrag": _eintrag, "aendern": _aendern, "kopieren": _kopieren, "entfernen": _loeschen_schnell,
          "verschieben": _verschieben, "abwesenheit": _abwesenheit, "abwesenheit_loeschen": _abwesenheit_loeschen,
          "alle_abrechnen": _alle_abrechnen, "einstellungen": _einstellungen, "nachweis": _nachweis},
         bereich="leistungen", bereich_titel="Leistungen", nach="einkauf", kuerzel="z",
         fragments={"woche": ("leistungen/_woche.html", _woche_ctx),
                    "vorschau": ("leistungen/_vorschau.html", _vorschau_ctx),
                    "kalender": ("leistungen/_kalender.html", _kalender_ctx),
                    "rechnung_vorschau": ("leistungen/_rechnung_vorschau.html", _abrechnen_ctx)}),
    Page("offerten", "Offerten", "offerten.html", _offerten_context,
         {"neu": _offerte_neu, "status": _offerte_status, "rechnung": _offerte_rechnung}, bereich="leistungen"),
    Page("entwuerfe", "Rechnungsentwürfe", "entwuerfe.html", _entwuerfe_context,
         {"neu": _entwurf_neu, "preis": _entwurf_preis, "aendern": _entwurf_aendern, "ausstellen": _entwurf_ausstellen,
          "verwerfen": _entwurf_verwerfen}, bereich="verkauf", nach="entwurf"),
    Page("rapporte", "Rapporte", "rapporte.html", _rapporte_context,
         {"speichern": _rapport_speichern, "uebernehmen": _rapport_uebernehmen, "verwerfen": _rapport_verwerfen,
          "hochladen": _rapport_hochladen, "formulare": _formulare}, bereich="leistungen"),
    Page("arbeitszeit", "Arbeitszeitkontrolle", "arbeitszeit.html", _azk_context,
         {"vorlage": _azk_vorlage, "excel": _azk_excel}, bereich="lohn"),
]

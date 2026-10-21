"""Anlagenbuchhaltung: Abschreibungen (linear/degressiv, ESTV-Höchstsätze) und Anlagenspiegel."""
from __future__ import annotations

from datetime import date

from aeradex import api
from aeradex.plugins import Command, DossierTeil, Finding, Page, Source, Todo, hookimpl

from . import anlagen

AERADEX_PLUGIN_API = 1
__version__ = "0.2.0"
AERADEX_TITEL = "Anlagen"
AERADEX_TEXT = "Fahrzeuge, Maschinen und Geräte mit Abschreibungen und Anlagenspiegel"
PAGE = "/p/anlagen/anlagen"


@hookimpl
def aeradex_sources():
    return [Source("abschreibung", "Anlage", anlagen.rows, link=lambda ref: PAGE)]


@hookimpl
def aeradex_check(book, rows):
    """Each fixed-asset account holding assets should equal their book values (at the last year-end);
    a purchase on an asset account that is no asset yet is pointed out."""
    from aeradex.ledger import BalanceEngine
    out = [Finding("hinweis", "anlagen", f"Anschaffung Beleg {k['beleg']} auf {k['konto']} "
                                         f"({k['rest']:.2f}) ist noch keine Anlage — unter Anlagen erfassen")
           for k in anlagen.kandidaten(book)]
    items = anlagen.assets(book)
    if not items or not book.years():
        return out
    year = max(book.years())
    sched = anlagen.schedule(book, year)
    per = {}
    for line in sched["anlagen"]:
        per[line["konto"]] = per.get(line["konto"], anlagen.ZERO) + line["ende"]
    eng = BalanceEngine(book)
    for konto, total in sorted(per.items()):
        if konto not in book.accounts:
            out.append(Finding("warnung", "anlagen", f"Konto {konto} fehlt im Kontenplan"))
            continue
        saldo = eng.balance(konto, year)
        if saldo != total:
            out.append(Finding("hinweis", "anlagen", f"Konto {konto}: Saldo {saldo:.2f}, Anlagen {total:.2f} — "
                               "eine Anschaffung ohne Anlage erfasst, oder umgekehrt?"))
    open_ = [l["nummer"] for l in sched["anlagen"] if l["offen"]]
    if open_ and date.today() > date(year, 12, 31):
        out.append(Finding("hinweis", "anlagen", f"Abschreibungen {year} noch nicht gebucht ({', '.join(open_[:5])})"))
    return out


@hookimpl
def aeradex_nach_beleg(book, beleg):
    """After a bill or receipt was booked onto an asset account: continue on the Anlagen page with it."""
    if anlagen.kandidaten(book, beleg):
        return f"{PAGE}?beleg={beleg}#kandidaten"
    return None


def todos(book, today: date | None = None) -> list[Todo]:
    """Purchases that are not an asset yet; from December on, the year's depreciation still to book
    (in December as a reminder, after the year end as a warning)."""
    today = today or date.today()
    out = []
    cands = anlagen.kandidaten(book)
    if cands:
        n = len(cands)
        detail = " · ".join(f"{k['bezeichnung']} {k['rest']:,.2f}".replace(",", "'") for k in cands[:3])
        out.append(Todo(f"{n} Anschaffung{'en' if n > 1 else ''} als Anlage erfassen", f"{PAGE}#kandidaten",
                        detail + (" …" if n > 3 else ""), "Anlagen"))
    for year in sorted(book.years()):
        if year > today.year or year == today.year and today.month < 12:
            continue
        todo = anlagen.preview(book, year)
        if todo:
            total = sum((t["betrag"] for t in todo), anlagen.ZERO)
            out.append(Todo(f"Abschreibungen {year} buchen", f"{PAGE}?jahr={year}#spiegel",
                            f"{len(todo)} Anlage{'n' if len(todo) > 1 else ''} · {total:,.2f}".replace(",", "'")
                            + " · per 31.12.", "Anlagen", "warn" if year < today.year else "info"))
    return out


@hookimpl
def aeradex_todos(book):
    """On the Übersicht: purchases that are not an asset yet, depreciation due at the year end."""
    return todos(book)


# ---------- Jahresabschluss ----------

@hookimpl
def aeradex_abschluss_schritte(book, jahr):
    """The year-end checklist: purchases still to turn into assets, the year's depreciation."""
    from aeradex.plugins import AbschlussSchritt
    out = []
    cands = [k for k in anlagen.kandidaten(book) if k["datum"].year <= int(jahr)]
    if cands:
        out.append(AbschlussSchritt("anlagen_erfassen", "Anschaffungen als Anlage erfassen", "offen",
                                    f"{PAGE}#kandidaten", f"{len(cands)} Anschaffung(en) auf Anlagekonten ohne Anlage: "
                                    + ", ".join(k["bezeichnung"] for k in cands[:3]), "bewerten"))
    active = [m for m in anlagen.assets(book).values() if anlagen.start_of(m).year <= int(jahr)
              and not (m.get("abgang") and str(m["abgang"]["datum"])[:4] < str(jahr))]
    todo = anlagen.preview(book, int(jahr))
    if todo:
        total = sum((t["betrag"] for t in todo), anlagen.ZERO)
        out.append(AbschlussSchritt("abschreibungen", f"Abschreibungen {jahr}", "offen", f"{PAGE}?jahr={jahr}#spiegel",
                                    f"{len(todo)} Anlage(n), zusammen {total:,.2f} — per 31.12. buchen.".replace(",", "'")))
    elif active:
        out.append(AbschlussSchritt("abschreibungen", f"Abschreibungen {jahr}", "ok", f"{PAGE}?jahr={jahr}#spiegel",
                                    "Gebucht; der Anlagenspiegel liegt in den Unterlagen."))
    else:
        out.append(AbschlussSchritt("abschreibungen", f"Abschreibungen {jahr}", "entfaellt", PAGE,
                                    "Keine Anlagen im Anlagenspiegel."))
    return out


@hookimpl
def aeradex_abschluss_vorschlaege(book, jahr, art):
    """Privatanteil of each vehicle: 0.9 % of the purchase price per month it was in the business that year."""
    if art != "privatanteile":
        return []
    from aeradex.jahresabschluss import fahrzeug_anteil
    out = []
    for nr, m in anlagen.assets(book).items():
        if m.get("kategorie") != "fahrzeuge":
            continue
        start = max(anlagen.parse_date(m["datum"]), date(int(jahr), 1, 1))
        end = anlagen.parse_date(m["abgang"]["datum"]) if m.get("abgang") else date(int(jahr), 12, 31)
        end = min(end, date(int(jahr), 12, 31))
        if start > end:
            continue
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        price = anlagen.money(m["wert"])
        out.append({"art": "fahrzeug", "konto": "6270", "text": f"Fahrzeug {m['bezeichnung']}",
                    "betrag": fahrzeug_anteil(price, months),
                    "grundlage": f"0.9 % × {price:,.2f} × {months} Monate".replace(",", "'"),
                    "detail": f"aus Anlagen {nr} — nur wenn privat genutzt", "quelle": "anlagen"})
    return out


# ---------- Abschlussunterlagen and Berichte ----------

def _dossier(book, year, fmt):
    from aeradex import berichte
    rep = anlagen.report(book, year)
    if fmt == "csv":
        return [(f"Anlagenspiegel {year}.csv", berichte.csv(rep))]
    return [(f"Anlagenspiegel {year}.pdf",
             berichte.pdf(book, [{"bericht": rep, "kommentar": None}], f"Anlagenspiegel {year}"))]


@hookimpl
def aeradex_dossier_teile():
    return [DossierTeil("anlagen", "Anlagenspiegel", ("pdf", "csv"), _dossier,
                        lambda book, year: bool(anlagen.schedule(book, year)["anlagen"]),
                        "Anlagen mit Anfang, Zugang, Abschreibung, Abgang und Ende")]


@hookimpl
def aeradex_reports():
    from aeradex.reports import Report
    return [Report("anlagenspiegel", "Anlagenspiegel", lambda book, p: anlagen.report(book, int(p.get("jahr") or
                                                                                               max(book.years()))),
                   params=(), gruppe="Abschluss",
                   beschreibung="Anlagen mit Anfang, Zugang, Abschreibung, Abgang und Ende")]


# ---------- agent ----------

def fixed_assets(jahr: int = 0) -> dict:
    """Anlagenspiegel: Anlagen mit Anfangsbestand, Zugang, Abschreibung, Abgang und Endbestand im Jahr.

    Args:
        jahr: Geschäftsjahr (0 = letztes Jahr mit Buchungen).
    """
    from aeradex.tools import call
    return call(lambda b: api.jsonable(anlagen.schedule(b, jahr or max(b.years()))))


def asset_candidates() -> list | dict:
    """Gebuchte Anschaffungen auf Anlagekonten (15xx–17xx), die noch keine Anlage sind: Beleg, Konto, Datum,
    noch nicht erfasster Betrag, Vorschlag für Bezeichnung, Kategorie und Nutzungsdauer."""
    from aeradex.tools import call
    return call(lambda b: api.jsonable(anlagen.kandidaten(b)))


def add_asset_from_booking(beleg: str, konto: str, bezeichnung: str = "", kategorie: str = "",
                           nutzungsdauer: str = "", wert: str = "") -> dict:
    """Eine gebuchte Anschaffung als Anlage erfassen (Datum, Konto und Wert aus der Buchung; bucht nichts).

    Args:
        beleg: Belegnummer der Anschaffung (aus asset_candidates).
        konto: Anlagekonto der Buchung, z.B. "1540".
        bezeichnung: z.B. "Airless-Spritzgerät Graco"; leer = aus dem Buchungstext.
        kategorie: mobiliar, bueromaschinen, edv, fahrzeuge, maschinen, werkzeuge, geruest; leer = aus dem Konto.
        nutzungsdauer: Jahre (linear); leer = aus der Kategorie (halber ESTV-Höchstsatz).
        wert: nur bei mehreren Anlagen auf einem Beleg: Teilbetrag; leer = ganzer Rest.
    """
    from aeradex.tools import call
    return call(lambda b: api.write(b, f"Anlage aus Beleg {beleg} erfasst", anlagen.add_from_booking, beleg, konto,
                                    bezeichnung, kategorie, wert or None, nutzungsdauer or None))


def add_existing_asset(bezeichnung: str, datum: str, wert: str, buchwert: str, kategorie: str = "",
                       nutzungsdauer: str = "") -> dict:
    """Eine Anlage übernehmen, die schon vor dem ersten Buchjahr da war (steht in der Eröffnungsbilanz; bucht
    nichts). Eine Einlage aus dem Privatvermögen bucht ein Mensch unter Anlagen.

    Args:
        bezeichnung: z.B. "Lieferwagen VW Crafter".
        datum: ursprüngliches Kaufdatum JJJJ-MM-TT.
        wert: ursprünglicher Kaufpreis.
        buchwert: Wert per 1. Januar des ersten Buchjahrs.
        kategorie: mobiliar, bueromaschinen, edv, fahrzeuge, maschinen, werkzeuge, geruest.
        nutzungsdauer: Jahre ab Kauf (linear); leer = aus der Kategorie.
    """
    from aeradex.tools import call
    return call(lambda b: api.write(b, f"Anlage {bezeichnung} übernommen", anlagen.add_existing, bezeichnung, datum,
                                    wert, buchwert, None, kategorie, "", "linear", None, nutzungsdauer or None))


@hookimpl
def aeradex_tools():
    return [fixed_assets, asset_candidates, add_asset_from_booking, add_existing_asset]


@hookimpl
def aeradex_instructions():
    return ("- Anlagen (Plugin anlagen): Anschaffungen mit mehrjähriger Nutzung (Fahrzeug, Maschine, Spritzgerät, "
            "Gerüst, Computer — ab etwa CHF 1000) aufs Anlagekonto 15xx statt in den Aufwand; danach mit "
            "add_asset_from_booking als Anlage erfassen (asset_candidates zeigt, was fehlt). Was vor dem ersten "
            "Buchjahr schon da war: add_existing_asset. fixed_assets() zeigt den Anlagenspiegel. Abschreibungen, "
            "Abgänge und Einlagen bucht ein Mensch (Buchhaltung → Anlagen).")


# ---------- CLI ----------

def _run(book, a):
    if a.aktion == "list":
        return anlagen.schedule(book, a.jahr or max(book.years()))
    if a.aktion == "kandidaten":
        return anlagen.kandidaten(book)
    if a.aktion == "add":
        if a.beleg:
            return api.write(book, f"Anlage aus Beleg {a.beleg} erfasst", anlagen.add_from_booking, a.beleg, a.konto,
                             a.bezeichnung, a.kategorie, a.wert, a.nutzungsdauer, a.methode, a.satz)
        return api.write(book, f"Anlage {a.bezeichnung} erfasst", anlagen.add, a.bezeichnung, a.datum, a.wert,
                         a.kategorie, a.konto, a.methode, a.satz, nutzungsdauer=a.nutzungsdauer)
    if a.aktion == "uebernehmen":
        return api.write(book, f"Anlage {a.bezeichnung} übernommen", anlagen.add_existing, a.bezeichnung, a.datum,
                         a.wert, a.buchwert, a.per, a.kategorie, a.konto, a.methode, a.satz, a.nutzungsdauer,
                         einlage=a.einlage)
    if a.aktion == "aendern":
        fields = {k: getattr(a, k) for k in ("bezeichnung", "kategorie", "satz", "nutzungsdauer", "restwert")
                  if getattr(a, k) not in (None, "")}
        return api.write(book, f"Anlage {a.nummer} geändert", anlagen.update, a.nummer, **fields)
    if a.aktion == "entfernen":
        return api.write(book, f"Anlage {a.nummer} entfernt", anlagen.remove, a.nummer)
    if a.aktion == "abschreiben":
        return api.write(book, f"Abschreibungen {a.jahr} gebucht", anlagen.depreciate, a.jahr)
    if a.aktion == "abgang":
        return api.write(book, f"Abgang {a.nummer}", anlagen.dispose, a.nummer, a.datum, a.wert or 0)


def _setup(p):
    p.add_argument("aktion", choices=["list", "kandidaten", "add", "uebernehmen", "aendern", "entfernen",
                                      "abschreiben", "abgang"])
    p.add_argument("--jahr", type=int, default=0)
    p.add_argument("--bezeichnung", default="")
    p.add_argument("--datum", help="Anschaffung · uebernehmen: ursprünglicher Kauf · abgang: Datum")
    p.add_argument("--wert", help="add: Anschaffungswert (mit --beleg: Teilbetrag) · uebernehmen: Kaufpreis · "
                                  "abgang: Verkaufserlös")
    p.add_argument("--beleg", default="", help="add: aus dieser gebuchten Anschaffung")
    p.add_argument("--buchwert", help="uebernehmen: Buchwert per --per")
    p.add_argument("--per", help="uebernehmen: ab wann im Buch (Standard: 1.1. des ersten Jahres)")
    p.add_argument("--einlage", action="store_true", help="uebernehmen: als Einlage an Eigenkapital buchen")
    p.add_argument("--kategorie", default="", choices=[""] + list(anlagen.KATEGORIEN))
    p.add_argument("--konto", default="")
    p.add_argument("--methode", default="linear", choices=list(anlagen.METHODEN))
    p.add_argument("--satz")
    p.add_argument("--nutzungsdauer", help="Jahre (linear)")
    p.add_argument("--restwert")
    p.add_argument("--nummer", default="")


@hookimpl
def aeradex_commands():
    return [Command("anlagen", "Anlagenbuchhaltung: Anlagen, Abschreibungen, Anlagenspiegel", _run, _setup)]


# ---------- page ----------

def _context(book, query):
    years = book.years() or [date.today().year]
    year = int(query.get("jahr") or max(years))
    cands = anlagen.kandidaten(book)
    focus = query.get("beleg") or ""
    return {"jahr": year, "jahre": years, "spiegel": anlagen.schedule(book, year),
            "vorschau": anlagen.preview(book, year), "kategorien": anlagen.KATEGORIEN,
            "lebensdauer": {k: anlagen.useful_life(k) for k in anlagen.KATEGORIEN},
            "kandidaten": cands, "fokus": focus, "erstes_jahr": book.settings.erstes_jahr,
            "eigenkapital": anlagen.equity_account(book), "tab": query.get("tab") or "",
            "rundung": anlagen.settings(book)["rundung"], "rundungen": anlagen.ROUNDINGS,
            "konten_aktiv": [a for a in book.accounts.values()
                             if a.klasse == "aktiv" and a.gruppe in anlagen.ASSET_GROUPS]}


def _blank(form, key):
    v = form.get(key)
    return None if v in (None, "") else v


def _add(book, form):
    return api.write(book, f"Anlage {form.get('bezeichnung')} erfasst", anlagen.add, form.get("bezeichnung") or "",
                     form.get("datum"), form.get("wert"), form.get("kategorie") or "", form.get("konto") or "",
                     form.get("methode") or "linear", _blank(form, "satz"), form.get("restwert") or 0,
                     nutzungsdauer=_blank(form, "nutzungsdauer"), notiz=form.get("notiz") or "")


def _from_booking(book, form):
    res = api.write(book, f"Anlage aus Beleg {form.get('beleg')} erfasst", anlagen.add_from_booking,
                    form.get("beleg") or "", form.get("konto") or "", form.get("bezeichnung") or "",
                    form.get("kategorie") or "", _blank(form, "wert"), _blank(form, "nutzungsdauer"),
                    form.get("methode") or "linear", _blank(form, "satz"))
    return {**res, "weiter": PAGE}


def _ignore(book, form):
    res = api.write(book, f"Beleg {form.get('beleg')} auf {form.get('konto')}: keine Anlage", anlagen.ignore,
                    form.get("beleg") or "", form.get("konto") or "")
    return {**res, "weiter": PAGE}


def _existing(book, form):
    return api.write(book, f"Anlage {form.get('bezeichnung')} übernommen", anlagen.add_existing,
                     form.get("bezeichnung") or "", form.get("datum"), form.get("wert"), form.get("buchwert"),
                     _blank(form, "per"), form.get("kategorie") or "", form.get("konto") or "",
                     form.get("methode") or "linear", _blank(form, "satz"), _blank(form, "nutzungsdauer"),
                     form.get("restwert") or 0, einlage=form.get("einlage") == "1",
                     gegenkonto=form.get("gegenkonto") or "", notiz=form.get("notiz") or "")


def _update(book, form):
    fields = {k: form.get(k) for k in anlagen.EDITABLE + anlagen.EDITABLE_UNBOOKED if form.get(k) is not None}
    return api.write(book, f"Anlage {form.get('nummer')} geändert", anlagen.update, form.get("nummer"), **fields)


def _remove(book, form):
    return api.write(book, f"Anlage {form.get('nummer')} entfernt", anlagen.remove, form.get("nummer"))


def _depreciate(book, form):
    amounts = {k[len("betrag_"):]: v for k, v in form.items() if k.startswith("betrag_")}
    return api.write(book, f"Abschreibungen {form.get('jahr')} gebucht", anlagen.depreciate, int(form.get("jahr")),
                     amounts or None)


def _settings(book, form):
    return api.write(book, f"Anlagen: Buchwerte runden auf {form.get('rundung') or 0}", anlagen.save_settings,
                     form.get("rundung") or 0)


def _dispose(book, form):
    return api.write(book, f"Abgang {form.get('nummer')}", anlagen.dispose, form.get("nummer"), form.get("datum"),
                     form.get("erloes") or 0)


@hookimpl
def aeradex_pages():
    return [Page("anlagen", "Anlagen", "anlagen.html", _context,
                 {"erfassen": _add, "aus_beleg": _from_booking, "keine_anlage": _ignore, "uebernehmen": _existing,
                  "aendern": _update, "entfernen": _remove, "abschreiben": _depreciate, "abgang": _dispose,
                  "einstellungen": _settings},
                 bereich="buchhaltung")]


@hookimpl
def aeradex_datensicherung(book):
    from aeradex.datensicherung import liste
    return [liste("anlagenverzeichnis", "Buchhaltung", "Anlagenverzeichnis",
                  lambda b, jahr: [{**meta, "nummer": meta.get("nummer") or nr}
                                   for nr, meta in sorted(anlagen.assets(b).items())],
                  [("nummer", "Nr"), ("bezeichnung", "Bezeichnung"), ("kategorie", "Kategorie"), ("konto", "Konto"),
                   ("datum", "Anschaffung"), ("wert", "Wert"), ("methode", "Methode"), ("satz", "Satz %"),
                   ("nutzungsdauer", "Nutzungsdauer")],
                  ("anlagen/**",), beschreibung="alle Anlagen mit Anschaffung und Abschreibungsmethode")]


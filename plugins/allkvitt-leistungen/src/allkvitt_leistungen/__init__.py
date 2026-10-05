"""Leistungen: Offerten, Zeiterfassung, Produkte, Projekte, Stundenkontrolle und Abrechnung an Kunden."""
from __future__ import annotations

from datetime import date

from allkvitt import api
from allkvitt.book import BookError
from allkvitt.files import FormatError
from allkvitt.plugins import Command, Finding, hookimpl

from . import abrechnung, daten, kontrolle, offerten

ALLKVITT_PLUGIN_API = 1
__version__ = "0.1.0"


# ---------- check ----------

@hookimpl
def allkvitt_check(book, rows):
    from allkvitt import invoices
    out = []
    try:
        items = daten.entries(book)
        projs = daten.projects(book)
        prods = daten.products(book)
        persons = daten.people(book)
    except (BookError, FormatError, KeyError, ValueError) as exc:
        return [Finding("fehler", "leistungen", f"Leistungen lassen sich nicht lesen: {exc}")]
    custs = invoices.customers(book)
    status, _ = daten.context_maps(book)
    seen = set()
    for e in items:
        where = f"{book.rel(e['_datei'])} {e['id']}"
        if e["id"] in seen:
            out.append(Finding("fehler", where, "ID doppelt"))
        seen.add(e["id"])
        if e["art"] not in daten.ARTEN:
            out.append(Finding("fehler", where, f"Art '{e['art']}' (Zeit oder Produkt)"))
        if e["kunde"] and e["kunde"] not in custs:
            out.append(Finding("fehler", where, f"Kunde {e['kunde']} unbekannt"))
        if e["projekt"] and e["projekt"] not in projs:
            out.append(Finding("fehler", where, f"Projekt {e['projekt']} unbekannt"))
        elif e["projekt"] and e["kunde"] != projs[e["projekt"]].get("kunde"):
            out.append(Finding("fehler", where, f"Projekt {e['projekt']} gehört zu einem anderen Kunden"))
        if e["produkt"] and e["produkt"] not in prods:
            out.append(Finding("fehler", where, f"Produkt {e['produkt']} unbekannt"))
        if e["wer"] and e["wer"] not in persons:
            out.append(Finding("warnung", where, f"Person {e['wer']} unbekannt"))
        if e["abrechenbar"] and (not e["kunde"] or e["preis"] is None):
            out.append(Finding("fehler", where, "abrechenbar ohne Kunde oder Preis"))
        if e["rechnung"]:
            if e["rechnung"] not in status:
                out.append(Finding("fehler", where, f"Rechnung {e['rechnung']} gibt es nicht"))
            elif status[e["rechnung"]] == "storniert":
                out.append(Finding("hinweis", where, f"{e['rechnung']} ist storniert — Eintrag ist wieder offen"))
    if out and any(f.level == "fehler" for f in out):
        return out
    out += [Finding("warnung", "leistungen", m) for m in abrechnung.mismatches(book)]
    out += [Finding("hinweis", "leistungen", m) for m in kontrolle.over_budget(book)]
    out += [Finding("hinweis", "offerten", f"Offerte {nr} ist abgelaufen und noch offen (versendet)")
            for nr in offerten.expired(book)]
    return out


# ---------- agent ----------

def _w(message, fn, *args, **kwargs):
    from allkvitt.tools import call
    return call(lambda b: api.write(b, message, fn, *args, **kwargs))


def record_time(datum: str, wer: str, stunden: str, text: str, kunde: str = "", projekt: str = "",
                abrechenbar: bool = True, kategorie: str = "") -> dict:
    """Arbeitszeit erfassen (Leistungen). Abrechenbar braucht Kunde (K0001) oder Projekt (P0001); der Stundensatz
    wird automatisch bestimmt (Projekt > Kunde > Person). Nicht abrechenbar: kategorie (Intern, Ferien, Krank …).

    Args:
        datum: JJJJ-MM-TT.
        wer: Personennummer (M0001 aus dem Lohn oder X01 für Personen ohne Lohn), siehe services_master_data.
        stunden: z.B. "2.5".
        text: was gemacht wurde (erscheint auf der Rechnung).
        kunde: Kundennummer.
        projekt: Projektnummer (Kunde ergibt sich daraus).
        abrechenbar: false für interne Zeit, Ferien, Krankheit.
        kategorie: bei nicht abrechenbar.
    """
    return _w(f"Zeit {stunden} h erfasst", daten.add_time, datum, wer, stunden, text, kunde, projekt,
              abrechenbar, kategorie)


def record_material(datum: str, kunde: str, produkt: str, menge: str, projekt: str = "", text: str = "") -> dict:
    """Produkt/Material für einen Kunden erfassen; der Preis kommt aus dem Produkt (Kundenpreis zuerst).

    Args:
        datum: JJJJ-MM-TT.
        kunde: Kundennummer.
        produkt: Produktnummer (P001), siehe services_master_data.
        menge: z.B. "3".
        projekt: Projektnummer, optional.
        text: abweichender Text, optional.
    """
    return _w(f"Produkt {produkt} erfasst", daten.add_material, datum, kunde, produkt, menge, projekt, None, text)


def services_master_data() -> dict:
    """Stammdaten der Leistungen: Personen mit Sätzen, Produkte mit Preisen, offene Projekte, Kategorien."""
    from allkvitt.tools import call
    return call(lambda b: api.jsonable({
        "personen": list(daten.people(b).values()), "produkte": list(daten.products(b).values()),
        "projekte": [{k: v for k, v in p.items() if not k.startswith("_")} for p in daten.projects(b).values()
                     if p.get("status") == "offen"],
        "kategorien": daten.settings(b)["kategorien"]}))


def open_services(kunde: str = "", projekt: str = "", bis: str = "") -> dict:
    """Noch nicht verrechnete Leistungen (Stunden und Produkte): Übersicht je Kunde/Projekt, mit kunde die Einträge.

    Args:
        kunde: Kundennummer, optional.
        projekt: Projektnummer, optional.
        bis: nur Einträge bis zu diesem Datum.
    """
    from allkvitt.tools import call

    def run(b):
        if not kunde and not projekt:
            return api.jsonable({"uebersicht": abrechnung.summary(b, bis or None)})
        items = abrechnung.select(b, kunde, projekt, bis=bis or None)
        return api.jsonable({"eintraege": [{k: v for k, v in e.items() if not k.startswith("_")} for e in items]})
    return call(run)


def billing_preview(kunde: str, projekt: str = "", bis: str = "", detail: bool = False) -> dict:
    """Vorschau der Rechnung für die offenen Leistungen eines Kunden. Ausstellen tut ein Mensch
    (Plugins → Abrechnen oder `allkvitt leistungen abrechnen`).

    Args:
        kunde: Kundennummer.
        projekt: nur dieses Projekt.
        bis: nur Einträge bis zu diesem Datum.
        detail: eine Position pro Eintrag statt gruppiert.
    """
    from allkvitt.tools import call

    def run(b):
        ids = [e["id"] for e in abrechnung.select(b, kunde, projekt, bis=bis or None)]
        return api.jsonable(abrechnung.preview(b, ids, detail))
    return call(run)


def draft_quote(kunde: str, positionen: list[dict], titel: str = "", text: str = "", abrechnung_art: str = "pauschal") -> dict:
    """Offerte als Entwurf anlegen (mit PDF). Versenden und Annehmen macht ein Mensch.

    Args:
        kunde: Kundennummer.
        positionen: Liste von {"produkt": "P001", "menge": "2"} oder {"stunden": "8", "text": "Montage", "wer": "M0001"}
            (oder "preis") oder frei {"text", "menge", "preis", "einheit"}.
        titel: kurzer Titel, z.B. "Badezimmer streichen".
        text: Einleitungstext.
        abrechnung_art: pauschal (Festpreis) oder aufwand (nach Aufwand, Offerte = Schätzung).
    """
    return _w(f"Offerte für {kunde} entworfen", offerten.create, kunde, positionen, None, titel, text, None, abrechnung_art)


def hours_control(monat: str) -> dict:
    """Stundenkontrolle: Soll, Ist, abrechenbar, Saldo im Monat und im Jahr je Person.

    Args:
        monat: JJJJ-MM.
    """
    from allkvitt.tools import call
    y, m = (int(x) for x in monat.split("-"))
    return call(lambda b: api.jsonable({"monat": monat, "personen": kontrolle.month(b, y, m)}))


def project_status(projekt: str) -> dict:
    """Projekt: Budget gegen Ist (Stunden, Wert, Kosten, verrechnet, offen, Deckungsbeitrag).

    Args:
        projekt: Projektnummer P0001.
    """
    from allkvitt.tools import call
    return call(lambda b: api.jsonable(kontrolle.project(b, projekt)))


@hookimpl
def allkvitt_tools():
    return [record_time, record_material, services_master_data, open_services, billing_preview, draft_quote,
            hours_control, project_status]


@hookimpl
def allkvitt_instructions():
    return ("- Leistungen (Plugin leistungen): Arbeitszeit mit record_time, Material mit record_material erfassen "
            "(Nummern aus services_master_data). Offene Leistungen: open_services, Rechnungsvorschau: billing_preview — "
            "Rechnungen stellt ein Mensch aus. Offerten nur als Entwurf (draft_quote). Stundenkontrolle: hours_control, "
            "Projekte: project_status.")


# ---------- CLI ----------

def _ids(raw) -> list[str]:
    return [i.strip() for part in raw or [] for i in str(part).split(",") if i.strip()]


def _pos(raw: list[str]) -> list[dict]:
    """--pos "Text;Menge;Preis[;Einheit]" · --produkt P001[:Menge] · --stunden 8[:Text[:Satz]]"""
    out = []
    for item in raw or []:
        kind, _, value = item.partition("=")
        if kind == "produkt":
            nr, _, menge = value.partition(":")
            out.append({"produkt": nr, "menge": menge or 1})
        elif kind == "stunden":
            parts = value.split(":")
            out.append({"stunden": parts[0], "text": parts[1] if len(parts) > 1 else "",
                        "preis": parts[2] if len(parts) > 2 else None})
        else:
            parts = value.split(";")
            if len(parts) < 3:
                raise BookError(f"Position '{value}': Text;Menge;Preis[;Einheit]")
            out.append({"text": parts[0], "menge": parts[1], "preis": parts[2],
                        "einheit": parts[3] if len(parts) > 3 else ""})
    return out


def _run(book, a):
    w = api.write
    b = a.bereich
    if b == "satz":
        return w(book, f"Leistungen: Satz {a.wer or a.name}", daten.set_person, a.wer, a.satz, a.kostensatz,
                 a.name, a.soll_woche)
    if b == "feiertage":
        return w(book, "Leistungen: Feiertage", daten.set_holidays, a.daten or None, a.ab)
    if b == "produkt":
        if a.aktion == "list":
            return api.jsonable(list(daten.products(book).values()))
        if a.aktion == "add":
            return w(book, f"Produkt {a.text} erfasst", daten.add_product, a.text, a.preis, a.einheit, a.konto,
                     a.mwst, a.nummer)
        return w(book, f"Produkt {a.nummer} geändert", daten.update_product, a.nummer, a.text or None, a.preis,
                 a.einheit or None, a.konto or None, a.mwst, None, a.kunde, a.kundenpreis)
    if b == "projekt":
        if a.aktion == "list":
            return api.jsonable([kontrolle.project(book, nr) for nr in daten.projects(book)])
        if a.aktion == "add":
            return w(book, f"Projekt {a.name} angelegt", daten.add_project, a.kunde, a.name, a.abrechnung,
                     a.budget_stunden, a.budget_chf, a.satz)
        if a.aktion == "status":
            return api.jsonable(kontrolle.project(book, a.nummer))
        return w(book, f"Projekt {a.nummer} abgeschlossen", daten.update_project, a.nummer, "abgeschlossen")
    if b == "zeit":
        return w(book, f"Zeit {a.stunden} h erfasst", daten.add_time, a.datum or date.today(), a.wer, a.stunden,
                 a.text, a.kunde, a.projekt, not a.nicht_abrechenbar, a.kategorie, a.satz)
    if b == "material":
        return w(book, f"Produkt {a.produkt} erfasst", daten.add_material, a.datum or date.today(), a.kunde,
                 a.produkt, a.menge, a.projekt, a.preis, a.text, a.wer)
    if b == "loeschen":
        return w(book, f"Leistung {a.id} gelöscht", daten.delete_entry, a.id)
    if b == "liste":
        items = abrechnung.select(book, a.kunde, a.projekt, a.von, a.bis, status="" if a.status == "alle" else a.status)
        return api.jsonable([{k: v for k, v in e.items() if not k.startswith("_")} for e in items])
    if b == "abrechnen":
        if not a.kunde and not a.ids:
            return api.jsonable(abrechnung.summary(book, a.bis))
        ids = _ids(a.ids) or [e["id"] for e in abrechnung.select(book, a.kunde, a.projekt, a.von, a.bis)]
        if a.vorschau:
            return api.jsonable(abrechnung.preview(book, ids, a.detail))
        return w(book, f"Leistungen verrechnet ({len(ids)} Einträge)", abrechnung.bill, ids, a.detail, a.datum,
                 a.text, None, not a.ohne_rapport)
    if b == "offerte":
        if a.aktion == "list":
            return api.jsonable([{k: v for k, v in q.items() if not k.startswith("_")}
                                 for q in offerten.quotes(book).values()])
        if a.aktion == "create":
            return w(book, f"Offerte für {a.kunde} erstellt", offerten.create, a.kunde, _pos(a.pos), a.datum,
                     a.titel, a.text, a.gueltig, a.abrechnung)
        if a.aktion == "status":
            return w(book, f"Offerte {a.nummer}: {a.status}", offerten.set_status, a.nummer, a.status)
        return w(book, f"Rechnung zu Offerte {a.nummer}", offerten.invoice, a.nummer, a.anteil, a.betrag,
                 a.schluss, a.datum)
    if b == "kontrolle":
        y, m = (int(x) for x in (a.monat or date.today().strftime("%Y-%m")).split("-"))
        if a.wer and a.jahr:
            return api.jsonable(kontrolle.year(book, y, a.wer))
        return api.jsonable(kontrolle.month(book, y, m, a.wer))
    if b == "lohn":
        y, m = (int(x) for x in a.monat.split("-"))
        return w(book, f"Stunden {m:02d}/{y} in den Lohnlauf übernommen", kontrolle.transfer_to_payroll, y, m, a.wer)
    raise BookError("Bereich fehlt — allkvitt leistungen -h")


def _setup(p):
    sub = p.add_subparsers(dest="bereich", required=True)
    s = sub.add_parser("satz", help="Verrechnungs-/Kostensatz einer Person; ohne --wer: Person ohne Lohn anlegen")
    s.add_argument("--wer", default="")
    s.add_argument("--name", default="")
    s.add_argument("--satz")
    s.add_argument("--kostensatz")
    s.add_argument("--soll-woche", dest="soll_woche", help="Person ohne Lohn: Sollstunden pro Woche")
    s = sub.add_parser("feiertage", help="Feiertage (ersetzen die Liste) und Beginn der Stundenkontrolle")
    s.add_argument("daten", nargs="*")
    s.add_argument("--ab", help="Stundenkontrolle ab diesem Datum (frühere Tage zählen nicht)")
    s = sub.add_parser("produkt", help="Produkte: list | add | set")
    s.add_argument("aktion", choices=["list", "add", "set"])
    s.add_argument("--nummer", default="")
    s.add_argument("--text", default="")
    s.add_argument("--preis")
    s.add_argument("--einheit", default="Stk")
    s.add_argument("--konto", default="")
    s.add_argument("--mwst")
    s.add_argument("--kunde", default="", help="set: Kundenpreis für diesen Kunden")
    s.add_argument("--kundenpreis")
    s = sub.add_parser("projekt", help="Projekte: list | add | status | abschliessen")
    s.add_argument("aktion", choices=["list", "add", "status", "abschliessen"])
    s.add_argument("--nummer", default="")
    s.add_argument("--kunde", default="")
    s.add_argument("--name", default="")
    s.add_argument("--abrechnung", default="aufwand", choices=list(daten.ABRECHNUNG))
    s.add_argument("--budget-stunden", dest="budget_stunden")
    s.add_argument("--budget-chf", dest="budget_chf")
    s.add_argument("--satz")
    s = sub.add_parser("zeit", help="Arbeitszeit erfassen")
    s.add_argument("--datum")
    s.add_argument("--wer", required=True)
    s.add_argument("--stunden", required=True)
    s.add_argument("--text", default="")
    s.add_argument("--kunde", default="")
    s.add_argument("--projekt", default="")
    s.add_argument("--nicht-abrechenbar", dest="nicht_abrechenbar", action="store_true")
    s.add_argument("--kategorie", default="")
    s.add_argument("--satz")
    s = sub.add_parser("material", help="Produkt für einen Kunden erfassen")
    s.add_argument("--datum")
    s.add_argument("--kunde", default="")
    s.add_argument("--projekt", default="")
    s.add_argument("--produkt", required=True)
    s.add_argument("--menge", required=True)
    s.add_argument("--preis")
    s.add_argument("--text", default="")
    s.add_argument("--wer", default="")
    s = sub.add_parser("loeschen", help="Eintrag löschen (nur nicht abgerechnete)")
    s.add_argument("id")
    for name, hlp in (("liste", "Einträge filtern"), ("abrechnen", "Offene Leistungen verrechnen (ohne --kunde: Übersicht)")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--kunde", default="")
        s.add_argument("--projekt", default="")
        s.add_argument("--von")
        s.add_argument("--bis")
        if name == "liste":
            s.add_argument("--status", default="offen", choices=["offen", "abgerechnet", "pauschal", "intern", "alle"])
        else:
            s.add_argument("--ids", nargs="*", help="nur diese Einträge (L-2026-0001 …)")
            s.add_argument("--detail", action="store_true", help="eine Position pro Eintrag")
            s.add_argument("--vorschau", action="store_true")
            s.add_argument("--datum")
            s.add_argument("--text", default="")
            s.add_argument("--ohne-rapport", dest="ohne_rapport", action="store_true")
    s = sub.add_parser("offerte", help="Offerten: list | create | status | rechnung")
    s.add_argument("aktion", choices=["list", "create", "status", "rechnung"])
    s.add_argument("--nummer", default="")
    s.add_argument("--kunde", default="")
    s.add_argument("--pos", action="append", default=[],
                   help='"frei=Text;Menge;Preis[;Einheit]" · "produkt=P001:2" · "stunden=8:Montage[:Satz]"')
    s.add_argument("--titel", default="")
    s.add_argument("--text", default="")
    s.add_argument("--datum")
    s.add_argument("--gueltig", type=int, help="Tage")
    s.add_argument("--abrechnung", default="pauschal", choices=list(daten.ABRECHNUNG))
    s.add_argument("--status", choices=list(offerten.STATUS))
    s.add_argument("--anteil", help="Teilrechnung in Prozent")
    s.add_argument("--betrag", help="Teilrechnung netto CHF")
    s.add_argument("--schluss", action="store_true")
    s = sub.add_parser("kontrolle", help="Stundenkontrolle Soll/Ist/Saldo")
    s.add_argument("--monat", help="JJJJ-MM (Standard: dieser Monat)")
    s.add_argument("--wer", default="")
    s.add_argument("--jahr", action="store_true", help="mit --wer: alle Monate des Jahres")
    s = sub.add_parser("lohn", help="Stunden der Stundenlöhner in den Lohnlauf übernehmen")
    s.add_argument("--monat", required=True)
    s.add_argument("--wer", default="")


@hookimpl
def allkvitt_commands():
    return [Command("leistungen", "Offerten, Zeiterfassung, Produkte, Projekte, Abrechnung", _run, _setup)]


from .seiten import PAGES  # noqa: E402  (pages need the functions above)


@hookimpl
def allkvitt_pages():
    return PAGES

"""Leistungen: Offerten, Zeiterfassung, Produkte, Projekte, Stundenkontrolle und Abrechnung an Kunden."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from aeradex import api
from aeradex.book import BookError
from aeradex.files import FormatError
from aeradex.plugins import Command, Finding, hookimpl

from . import abrechnung, abwesenheit, daten, entwuerfe, kontrolle, offerten, schnell, stoppuhr, woche

AERADEX_PLUGIN_API = 1
__version__ = "0.1.0"


# ---------- check ----------

# How the settings show this plugin (Einstellungen › Funktionen).
AERADEX_TITEL = "Leistungen & Offerten"
AERADEX_TEXT = "Stunden und Material erfassen, Offerten schreiben und an Kunden verrechnen"


@hookimpl
def aeradex_check(book, rows):
    from aeradex import invoices
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
    from aeradex.tools import call
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


def quick_entry(text: str, wer: str = "", datum: str = "") -> dict:
    """Zeit oder Material in einer Zeile erfassen, wie in der Schnelleingabe der Oberfläche, z.B.
    "3.5h Fassade spachteln", "2:30 P0003 Malerarbeiten Decke", "12 l Dispersionsfarbe Huber", "1h intern Buchhaltung".
    Ist etwas unklar (zwei Kunden passen), wird nichts gebucht und die Kandidaten kommen zurück.

    Args:
        text: die Zeile.
        wer: Personennummer (M0001, X01); leer = erste aktive Person.
        datum: Bezugsdatum JJJJ-MM-TT für «gestern», «mo» usw.; leer = heute.
    """
    from aeradex.tools import call

    def run(b):
        p = schnell.parse(b, text, wer, date.fromisoformat(datum) if datum else None)
        if p["kandidaten"] or p["fehlt"]:
            return api.jsonable({"ok": False, "fehlt": p["fehlt"], "kandidaten": p["kandidaten"], "erkannt": {
                k: p[k] for k in ("art", "datum", "stunden", "menge", "kunde", "projekt", "leistung", "produkt", "text")}})
        return api.write(b, f"Schnell erfasst: {text}", schnell.save, text, wer,
                         date.fromisoformat(datum) if datum else None)
    return call(run)


def record_absence(wer: str, art: str, von: str, bis: str = "", halber_tag: bool = False, notiz: str = "") -> dict:
    """Abwesenheit eintragen (Ferien, Krank, Unfall, Militär/ZS, Mutterschaft/Vaterschaft, Weiterbildung,
    Kompensation, Unbezahlt). Zählt als Ist-Zeit an Arbeitstagen (ausser Kompensation, Unbezahlt).

    Args:
        wer: Personennummer.
        art: siehe oben.
        von: JJJJ-MM-TT.
        bis: JJJJ-MM-TT; leer = ein Tag.
        halber_tag: true = je Tag ein halber.
        notiz: optional.
    """
    return _w(f"Abwesenheit {art} {von}", abwesenheit.add, wer, art, von, bis or None, "0.5" if halber_tag else 1, notiz)


def holiday_balance(wer: str, jahr: int) -> dict:
    """Ferienkonto einer Person: Anspruch, Vortrag, bezogen, geplant, Rest (Tage)."""
    from aeradex.tools import call
    return call(lambda b: api.jsonable(abwesenheit.holiday_account(b, wer, jahr)))


def services_master_data() -> dict:
    """Stammdaten der Leistungen: Personen mit Sätzen, Produkte mit Preisen, offene Projekte, Kategorien."""
    from aeradex.tools import call
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
    from aeradex.tools import call

    def run(b):
        if not kunde and not projekt:
            return api.jsonable({"uebersicht": abrechnung.summary(b, bis or None)})
        items = abrechnung.select(b, kunde, projekt, bis=bis or None)
        return api.jsonable({"eintraege": [{k: v for k, v in e.items() if not k.startswith("_")} for e in items]})
    return call(run)


def billing_preview(kunde: str, projekt: str = "", bis: str = "", detail: bool = False) -> dict:
    """Vorschau der Rechnung für die offenen Leistungen eines Kunden. Ausstellen tut ein Mensch
    (Leistungen → Abrechnen oder `aeradex leistungen abrechnen`).

    Args:
        kunde: Kundennummer.
        projekt: nur dieses Projekt.
        bis: nur Einträge bis zu diesem Datum.
        detail: eine Position pro Eintrag statt gruppiert.
    """
    from aeradex.tools import call

    def run(b):
        ids = [e["id"] for e in abrechnung.select(b, kunde, projekt, bis=bis or None)]
        return api.jsonable(abrechnung.preview(b, ids, detail))
    return call(run)


def invoice_drafts() -> list | dict:
    """Offene Rechnungsentwürfe (aus Rapporten oder offenen Leistungen gesammelt): Kunde, Objekt, Betrag, ohne Preis."""
    from aeradex.tools import call
    return call(lambda b: api.jsonable(entwuerfe.overview(b)))


def invoice_draft(nummer: str) -> dict:
    """Ein Rechnungsentwurf mit seinen Einträgen, der Rechnungsvorschau und dem, was fehlt (Preise, Beschreibungen).

    Args:
        nummer: z.B. "RE-0003".
    """
    from aeradex.tools import call

    def run(b):
        v = entwuerfe.view(b, nummer)
        v["eintraege_offen"] = [{k: x for k, x in e.items() if not k.startswith("_")} for e in v["eintraege_offen"]]
        v["nicht_mehr_offen"] = [e["id"] for e in v["nicht_mehr_offen"]]
        return api.jsonable(v)
    return call(run)


def update_invoice_draft(nummer: str = "", kunde: str = "", eintraege: list[str] | None = None, text: str = "",
                         rabatt_prozent: str = "", entfernen: list[str] | None = None) -> dict:
    """Offene Leistungen in einen Rechnungsentwurf legen (ohne nummer: in den offenen Entwurf des Kunden bzw. einen
    neuen) oder einen Entwurf ändern. Preise fehlender Einträge mit update_service_entry setzen. Ausstellen tut ein
    Mensch unter Leistungen › Rechnungsentwürfe.

    Args:
        nummer: Entwurf, z.B. "RE-0003"; leer = Entwurf des Kunden.
        kunde: Kundennummer (nötig ohne nummer).
        eintraege: IDs offener Leistungen (open_services), die dazukommen.
        text: zusätzlicher Text auf der Rechnung.
        rabatt_prozent: Rabatt in Prozent.
        entfernen: IDs, die aus dem Entwurf genommen werden (bleiben offen).
    """
    if not nummer:
        return _w(f"Rechnungsentwurf für {kunde}", entwuerfe.add, kunde, list(eintraege or []), "", None, text)
    return _w(f"{nummer} geändert", entwuerfe.update, nummer, text or None, rabatt_prozent or None,
              list(entfernen or []) or None, list(eintraege or []) or None)


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


def update_quote(nummer: str, positionen: list[dict] | None = None, titel: str = "", text: str = "",
                 adresse_neu: bool = False) -> dict:
    """Offerte ändern, solange sie nicht angenommen oder abgelehnt ist (das PDF wird neu erstellt).

    Args:
        nummer: Offertnummer, z.B. "O-2026-0002".
        positionen: neue Positionen (ersetzen die bisherigen), Format wie bei draft_quote; weglassen = unverändert.
        titel: neuer Titel; leer = unverändert.
        text: neuer Einleitungstext; leer = unverändert.
        adresse_neu: Adresse und Kontaktperson neu vom Kunden übernehmen (nach einer Änderung beim Kunden).
    """
    return _w(f"Offerte {nummer} geändert", offerten.update, nummer, positionen, titel or None, text or None,
              None, None, adresse_neu)


def hours_control(monat: str) -> dict:
    """Stundenkontrolle: Soll, Ist, abrechenbar, Saldo im Monat und im Jahr je Person.

    Args:
        monat: JJJJ-MM.
    """
    from aeradex.tools import call
    y, m = (int(x) for x in monat.split("-"))
    return call(lambda b: api.jsonable({"monat": monat, "personen": kontrolle.month(b, y, m)}))


def project_status(projekt: str) -> dict:
    """Projekt: Budget gegen Ist (Stunden, Wert, Kosten, verrechnet, offen, Deckungsbeitrag).

    Args:
        projekt: Projektnummer P0001.
    """
    from aeradex.tools import call
    return call(lambda b: api.jsonable(kontrolle.project(b, projekt)))


def customer_objects(kunde: str) -> dict:
    """Objekte (Projekte) eines Kunden mit Adresse, Zugang, Status und Budget gegen Ist — z.B. die Liegenschaften
    einer Verwaltung.

    Args:
        kunde: Kundennummer, z.B. "K0003".
    """
    from aeradex.tools import call
    return call(lambda b: api.jsonable([{**kontrolle.project(b, nr), "notizen": p.get("_notizen", "")}
                                        for nr, p in daten.projects(b).items() if p.get("kunde") == kunde]))


def add_object(kunde: str, strasse: str = "", hausnr: str = "", plz: str = "", ort: str = "", name: str = "",
               zugang: str = "", notizen: str = "") -> dict:
    """Ein Objekt (Liegenschaft/Baustelle) für einen Kunden anlegen; ein Kunde kann mehrere haben. Vorher mit
    customer_objects prüfen, ob es das Objekt schon gibt. Zeit und Material lassen sich danach dem Objekt
    zuordnen, und die Rechnung nennt es.

    Args:
        kunde: Kundennummer, z.B. "K0003".
        strasse: Strasse des Objekts.
        hausnr: Hausnummer.
        plz: Postleitzahl.
        ort: Ort.
        name: Bezeichnung, z.B. "MFH Seestrasse"; leer = die Adresse.
        zugang: Schlüssel, Code, Hauswart/Ansprechperson vor Ort.
        notizen: weitere Angaben zum Objekt in ganzen Sätzen.
    """
    label = name or " ".join(x for x in (strasse, hausnr, ort) if x)
    return _w(f"Objekt {label} für {kunde} angelegt", daten.add_project, kunde, name, "aufwand", None, None, None,
              "", notizen, strasse=strasse, hausnr=hausnr, plz=plz, ort=ort, zugang=zugang)


def update_object(nummer: str, name: str = "", strasse: str = "", hausnr: str = "", plz: str = "", ort: str = "",
                  zugang: str = "", notizen: str = "") -> dict:
    """Adresse, Zugang oder Notizen eines Objekts (Projekts) ändern; nur die mitgegebenen Felder.

    Args:
        nummer: Objekt-/Projektnummer, z.B. "P0004".
        name: neue Bezeichnung.
        strasse: Strasse.
        hausnr: Hausnummer.
        plz: Postleitzahl.
        ort: Ort.
        zugang: Schlüssel, Code, Hauswart.
        notizen: ersetzt die Notizen zum Objekt.
    """
    fields = {k: v for k, v in {"name": name, "strasse": strasse, "hausnr": hausnr, "plz": plz, "ort": ort,
                                "zugang": zugang, "notizen": notizen}.items() if str(v).strip()}
    if not fields:
        return {"ok": False, "fehler": "Keine Änderung angegeben"}
    return _w(f"Objekt {nummer} geändert", daten.update_project, nummer, None, **fields)


def set_person_rates(nummer: str = "", name: str = "", satz: str = "", kostensatz: str = "", soll_woche: str = "",
                     ferien_tage: str = "", ferien_jahr: int | None = None, ferien_vortrag: str = "",
                     vortrag_jahr: int | None = None, vortrag: str = "") -> dict:
    """Stundenansatz, Ferienanspruch und Vorträge einer Person (M0001 aus dem Lohn) — oder eine Person ohne Lohn
    anlegen/ändern (name, ohne nummer; z.B. die Inhaberin). Nur die mitgegebenen Werte.

    Args:
        nummer: M0001 oder X01; leer mit name = neue Person ohne Lohn.
        name: Name einer Person ohne Lohn.
        satz: verrechneter Stundenansatz CHF, z.B. "95".
        kostensatz: interne Kosten pro Stunde CHF.
        soll_woche: Wochenstunden (nur Personen ohne Lohn; bei Angestellten aus Pensum × Betriebswoche).
        ferien_tage: Ferientage pro Jahr (z.B. "25"); leer = allgemeine Einstellung.
        ferien_jahr: Jahr für ferien_vortrag.
        ferien_vortrag: Ferientage aus dem Vorjahr in ferien_jahr.
        vortrag_jahr: Jahr für vortrag.
        vortrag: Stundensaldo aus dem Vorjahr (Überstunden +, Minusstunden −).
    """
    loc = locals()
    kwargs = {k: loc[k] for k in ("satz", "kostensatz", "soll_woche", "ferien_tage", "ferien_vortrag", "vortrag")
              if str(loc[k]).strip()}
    if ferien_jahr:
        kwargs["ferien_jahr"] = ferien_jahr
    if vortrag_jahr:
        kwargs["vortrag_jahr"] = vortrag_jahr
    return _w(f"Person {nummer or name}: Ansätze", daten.set_person, nummer, name=name, **kwargs)


def change_pensum(nummer: str, ab: str, wert: str = "", entfernen: bool = False) -> dict:
    """Pensum (Angestellte, %) bzw. Wochenstunden (Personen ohne Lohn) ab einem Datum ändern, für die Soll-Stunden.
    Den Lohn ändert das nicht (update_employee).

    Args:
        nummer: M0001 oder X01.
        ab: ab Datum JJJJ-MM-TT.
        wert: neues Pensum in % bzw. Wochenstunden.
        entfernen: true = die Änderung an diesem Datum löschen.
    """
    return _w(f"Pensum {nummer} ab {ab}", daten.set_pensum, nummer, ab, wert or None, entfernen)


def add_product(text: str, preis: str, einheit: str = "Stk", konto: str = "", mwst: str = "") -> dict:
    """Produkt/Material oder Leistungsart in die Stammdaten (vorher services_master_data prüfen).

    Args:
        text: Bezeichnung.
        preis: Verkaufspreis CHF pro Einheit.
        einheit: Stk, l, kg, m, m2, h …
        konto: Ertragskonto; leer = Standard.
        mwst: MWST-Code.
    """
    return _w(f"Produkt {text} angelegt", daten.add_product, text, preis, einheit, konto, mwst)


def update_product(nummer: str, text: str = "", preis: str = "", einheit: str = "", aktiv: bool | None = None,
                   kunde: str = "", kundenpreis: str | None = None) -> dict:
    """Produkt ändern; ein neuer Preis gilt für neue Einträge. Mit kunde + kundenpreis ein Kundenpreis
    (kundenpreis "" entfernt ihn).

    Args:
        nummer: Produktnummer.
        text: Bezeichnung.
        preis: Preis CHF.
        einheit: Einheit.
        aktiv: false = nicht mehr anbieten.
        kunde: Kundennummer für einen Kundenpreis.
        kundenpreis: Preis für diesen Kunden.
    """
    return _w(f"Produkt {nummer} geändert", daten.update_product, nummer, text or None, preis or None,
              einheit or None, None, None, aktiv, kunde, kundenpreis)


def add_project(kunde: str, name: str, abrechnung: str = "aufwand", budget_stunden: str = "", budget_chf: str = "",
                satz: str = "", offerte: str = "", notizen: str = "") -> dict:
    """Projekt/Auftrag für einen Kunden (für eine Liegenschaft mit Adresse: add_object).

    Args:
        kunde: Kundennummer.
        name: Bezeichnung.
        abrechnung: "aufwand" (nach Stunden) oder "pauschal".
        budget_stunden: Budget in Stunden.
        budget_chf: Budget CHF.
        satz: eigener Stundenansatz für dieses Projekt.
        offerte: Offertnummer, falls daraus.
        notizen: Notizen.
    """
    return _w(f"Projekt {name} für {kunde} angelegt", daten.add_project, kunde, name, abrechnung,
              budget_stunden or None, budget_chf or None, satz or None, offerte, notizen)


def update_project(nummer: str, status: str = "", felder: dict | None = None) -> dict:
    """Projekt ändern oder abschliessen.

    Args:
        nummer: Projektnummer.
        status: offen oder abgeschlossen.
        felder: name, abrechnung, budget_stunden, budget_chf, satz, notizen …
    """
    return _w(f"Projekt {nummer} geändert", daten.update_project, nummer, status or None, **(felder or {}))


def update_service_entry(id: str, felder: dict) -> dict:
    """Erfasste Zeit oder Material korrigieren, solange nicht verrechnet (datum, menge, preis, text, abrechenbar,
    kategorie, kunde, projekt, leistung; neu_bewerten=true bestimmt den Stundenansatz neu).

    Args:
        id: Eintrag-ID aus open_services.
        felder: die zu ändernden Felder.
    """
    return _w(f"Eintrag {id} geändert", daten.update_entry, id, **(felder or {}))


def delete_service_entry(id: str) -> dict:
    """Erfasste Zeit oder Material löschen, solange nicht verrechnet. Nur auf Wunsch des Menschen.

    Args:
        id: Eintrag-ID.
    """
    return _w(f"Eintrag {id} gelöscht", daten.delete_entry, id)


@hookimpl
def aeradex_tools():
    return [record_time, record_material, quick_entry, record_absence, holiday_balance, services_master_data,
            open_services, billing_preview, invoice_drafts, invoice_draft, update_invoice_draft, draft_quote, update_quote, hours_control, project_status,
            customer_objects, add_object, update_object, set_person_rates, change_pensum, add_product,
            update_product, add_project, update_project, update_service_entry, delete_service_entry]


@hookimpl
def aeradex_instructions():
    return ("- Leistungen (Plugin leistungen): am schnellsten mit quick_entry («3.5h Fassade spachteln»), sonst "
            "Arbeitszeit mit record_time, Material mit record_material (Nummern aus services_master_data). Abwesenheiten "
            "mit record_absence, Ferienkonto mit holiday_balance. Offene Leistungen: open_services, Rechnungsvorschau: billing_preview, "
            "Rechnungsentwürfe: invoice_drafts/invoice_draft, sammeln mit update_invoice_draft — Rechnungen stellt ein "
            "Mensch aus. Offerten nur als Entwurf (draft_quote), ändern mit update_quote. Stundenkontrolle: hours_control, "
            "Projekte: project_status. Objekte/Liegenschaften eines Kunden (mit Adresse und Zugang): customer_objects, "
            "neu add_object, ändern update_object. Neue Mitarbeiter:in: nach add_employee Stundenansatz und "
            "Ferientage mit set_person_rates; Pensum-Wechsel mit change_pensum.")


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
    if b == "pensum":
        verb = "entfernt" if a.entfernen else f"{a.wert} ab {a.ab}"
        return w(book, f"Leistungen: Pensum {a.wer} {verb}", daten.set_pensum, a.wer, a.ab, a.wert, a.entfernen)
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
        if a.alle:
            je = a.je or daten.settings(book)["abrechnen_je"]
            if a.vorschau:
                return api.jsonable(abrechnung.groups(book, a.bis, je))
            return w(book, f"Leistungen abgerechnet bis {a.bis or 'heute'}", abrechnung.bill_all, a.bis, je, a.datum,
                     not a.ohne_rapport)
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
    if b == "schnell":
        line = " ".join(a.text)
        if a.vorschau:
            return api.jsonable({k: v for k, v in schnell.parse(book, line, a.wer).items() if k != "eingabe"})
        return w(book, f"Schnell erfasst: {line}", schnell.save, line, a.wer)
    if b == "start":
        line = " ".join(a.text)
        p = schnell.parse(book, line, a.wer)
        if p["kandidaten"]:
            raise BookError("Nicht eindeutig: " + str(p["kandidaten"]))
        return api.jsonable(stoppuhr.start(book, p["wer"], p["kunde"], p["projekt"], p["leistung"], p["text"],
                                           p["abrechenbar"], p["kategorie"]))
    if b == "stop":
        return api.jsonable(stoppuhr.stop(book, a.wer, a.text or None))
    if b == "stoppuhr":
        return api.jsonable(stoppuhr.running(book))
    if b == "woche":
        start = date.fromisoformat(a.woche) if a.woche else date.today()
        g = woche.grid(book, a.wer, start)
        return api.jsonable({"start": g["start"], "zeilen": [{"titel": r["titel"], "unter": r["unter"],
                             "stunden": [c["stunden"] for c in r["zellen"]], "total": r["total"]} for r in g["zeilen"]],
                             "gearbeitet": g["gearbeitet"], "soll": g["soll"], "saldo": g["saldo"]})
    if b == "abwesenheit":
        if a.loeschen:
            return w(book, f"Abwesenheit {a.loeschen} gelöscht", abwesenheit.delete, a.loeschen)
        if not a.art:
            return api.jsonable([x for x in abwesenheit.items(book) if not a.wer or x["wer"] == a.wer.upper()])
        return w(book, f"Abwesenheit {a.art} {a.von}", abwesenheit.add, a.wer, a.art, a.von, a.bis, a.anteil, a.notiz)
    if b == "ferien":
        return api.jsonable(abwesenheit.holiday_account(book, a.wer, a.jahr or date.today().year))
    if b == "nachweis":
        y, m = (int(x) for x in a.monat.split("-"))
        target = Path(a.pdf or f"Arbeitszeitnachweis {a.wer} {y}-{m:02d}.pdf")
        target.write_bytes(abwesenheit.time_record_pdf(book, a.wer, y, m))
        return {"ok": True, "pdf": str(target)}
    raise BookError("Bereich fehlt — aeradex leistungen -h")


def _setup(p):
    sub = p.add_subparsers(dest="bereich", required=True)
    s = sub.add_parser("pensum", help="Pensum (Mitarbeitende, %%) bzw. Wochenstunden (ohne Lohn) ab einem Datum, für das Soll")
    s.add_argument("--wer", required=True)
    s.add_argument("--ab", required=True, help="JJJJ-MM-TT")
    s.add_argument("--wert", help="Pensum in %% bzw. Wochenstunden")
    s.add_argument("--entfernen", action="store_true", help="die Änderung an diesem Datum löschen")
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
            s.add_argument("--alle", action="store_true", help="alles Offene auf einmal: eine Rechnung je Kunde (--je projekt: je Projekt)")
            s.add_argument("--je", default="", choices=["", "kunde", "projekt"])
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
    s = sub.add_parser("schnell", help='eine Zeile erfassen, z.B. "3.5h Fassade spachteln"')
    s.add_argument("text", nargs="+")
    s.add_argument("--wer", default="")
    s.add_argument("--vorschau", action="store_true", help="nur zeigen, was erkannt wird")
    s = sub.add_parser("start", help='Stoppuhr starten, z.B. start P0003 Malerarbeiten')
    s.add_argument("text", nargs="*")
    s.add_argument("--wer", default="")
    s = sub.add_parser("stop", help="Stoppuhr stoppen und erfassen")
    s.add_argument("--wer", required=True)
    s.add_argument("--text", default="")
    sub.add_parser("stoppuhr", help="laufende Stoppuhren")
    s = sub.add_parser("woche", help="Wochenübersicht einer Person")
    s.add_argument("--wer", required=True)
    s.add_argument("--woche", default="", help="ein Tag der Woche, JJJJ-MM-TT")
    s = sub.add_parser("abwesenheit", help="Abwesenheiten anzeigen, eintragen (--art) oder löschen (--loeschen ID)")
    s.add_argument("--wer", default="")
    s.add_argument("--art", default="", help=", ".join(abwesenheit.ARTEN))
    s.add_argument("--von")
    s.add_argument("--bis")
    s.add_argument("--anteil", default="1", help="1 oder 0.5")
    s.add_argument("--notiz", default="")
    s.add_argument("--loeschen", default="")
    s = sub.add_parser("ferien", help="Ferienkonto einer Person")
    s.add_argument("--wer", required=True)
    s.add_argument("--jahr", type=int)
    s = sub.add_parser("nachweis", help="Arbeitszeitnachweis (ArGV 1 Art. 73) als PDF")
    s.add_argument("--wer", required=True)
    s.add_argument("--monat", required=True)
    s.add_argument("--pdf", default="")


@hookimpl
def aeradex_abschluss_vorschlaege(book, jahr, art):
    """Year end: open work per 31.12. at cost, holidays and overtime of the employees."""
    from . import abschluss
    return abschluss.proposals(book, jahr, art)


@hookimpl
def aeradex_commands():
    return [Command("leistungen", "Offerten, Zeiterfassung, Produkte, Projekte, Abrechnung", _run, _setup)]


from .seiten import PAGES  # noqa: E402  (pages need the functions above)


@hookimpl
def aeradex_pages():
    return PAGES


@hookimpl
def aeradex_reports():
    from .berichte import REPORTS
    return REPORTS


@hookimpl
def aeradex_kunde_teile(book, kunde):
    """On the customer's page: objects (projects) with address and budget, quotes, open work."""
    from decimal import Decimal

    from aeradex.files import parse_date
    from aeradex.plugins import KundeTeil
    cfg = daten.settings(book)
    word = cfg["projekt_wort"]
    projs = [p for p in daten.projects(book).values() if p.get("kunde") == kunde]
    objekte = [{**kontrolle.project(book, p["nummer"]), "notizen": p.get("_notizen", ""),
                "adresse_teile": p.get("adresse") or {}} for p in projs]
    objekte.sort(key=lambda o: (o["status"] != "offen", o["nummer"]))
    today = date.today()
    quotes = sorted((q for q in offerten.quotes(book).values() if q.get("kunde") == kunde),
                    key=lambda q: str(q.get("datum")), reverse=True)
    quote_rows = [{"nummer": q["nummer"], "titel": q.get("titel") or "", "datum": str(q.get("datum")),
                   "gueltig_bis": str(q.get("gueltig_bis") or ""), "status": q.get("status") or "entwurf",
                   "total": daten.money(q.get("total")), "projekt": q.get("projekt") or "",
                   "abgelaufen": q.get("status") == "versendet" and bool(q.get("gueltig_bis"))
                   and parse_date(q["gueltig_bis"]) < today} for q in quotes]
    open_items = abrechnung.select(book, kunde=kunde)
    offen = sum((e["betrag"] for e in open_items), Decimal(0))
    stunden = sum((e["menge"] for e in open_items if e["art"] == "Zeit"), Decimal(0))
    names = {p["nummer"]: p["name"] for p in projs}
    per_obj: dict[str, dict] = {}
    for e in open_items:
        g = per_obj.setdefault(e["projekt"] or "", {"projekt": e["projekt"] or "", "name": names.get(e["projekt"], ""),
                                                    "betrag": Decimal(0), "stunden": Decimal(0), "von": e["datum"]})
        g["betrag"] += e["betrag"]
        g["stunden"] += e["menge"] if e["art"] == "Zeit" else 0
        g["von"] = min(g["von"], e["datum"])
    zeit = [{"datum": q["datum"], "art": "offerte", "titel": f"Offerte {q['nummer']}",
             "text": f"{q['titel']} · CHF {q['total']:,.2f} · {q['status']}".replace(",", "'"),
             "link": f"/p/leistungen/offerten?nr={q['nummer']}"} for q in quote_rows]
    zeit += [{"datum": str(p.get("erstellt") or ""), "art": "objekt", "titel": f"{word} {p['nummer']} angelegt",
              "text": p["name"], "link": ""} for p in projs if p.get("erstellt")]
    figures = []
    if offen:
        figures.append({"label": "Offene Leistungen", "wert": offen, "meta": f"{stunden.normalize():f} h nicht verrechnet",
                        "link": f"/p/leistungen/leistungen?tab=abrechnen&kunde={kunde}"})
    for draft in (d for d in entwuerfe.overview(book) if d["kunde"] == kunde):
        figures.append({"label": "Rechnungsentwurf", "wert": draft["betrag"],
                        "meta": f"{draft['nummer']} offen" + (f" · {draft['ohne_preis']} ohne Preis" if draft["ohne_preis"] else ""),
                        "link": f"/p/leistungen/entwuerfe?nr={draft['nummer']}"})
    pending = [q for q in quote_rows if q["status"] in ("entwurf", "versendet")]
    if pending:
        figures.append({"label": "Offerten offen", "wert": sum((q["total"] for q in pending), Decimal(0)),
                        "meta": f"{len(pending)} Offerte(n)", "link": "#offerten"})
    ctx = {"word": word, "objekte": objekte, "offerten": quote_rows, "offen": offen, "stunden": stunden,
           "offen_je": sorted(per_obj.values(), key=lambda g: g["von"]), "heute": today.isoformat()}
    return [KundeTeil(titel=f"{word}e, Offerten, Leistungen", template="leistungen/_kunde.html", context=ctx,
                      zeitachse=zeit, kennzahlen=figures,
                      aktionen=[("Offerte", f"/p/leistungen/offerten?kunde={kunde}"),
                                ("Rapport drucken", f"/p/leistungen/rapporte?ziel=K:{kunde}#drucken"),
                                ("Leistung erfassen", f"/p/leistungen/leistungen?tab=erfassen&neu_kunde={kunde}")])]


@hookimpl
def aeradex_eingang_arten(book):
    """Scanned Regie-, Material- and Wochenrapporte: chosen when scanning, or recognised by the QR code of a form
    printed by aeradex; read and kept for checking under Leistungen › Rapporte."""
    from aeradex.plugins import EingangsArt
    from . import rapporte

    def kind(art):
        def recognise(b, path):
            q = rapporte.qr_of(path) if path.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".webp") else None
            return bool(q and q["art"] == art)
        return EingangsArt(art, rapporte.ARTEN[art], lambda b, path: rapporte.take_in(b, path, art), recognise)
    return [kind(a) for a in rapporte.ARTEN]


@hookimpl
def aeradex_todos(book):
    """On the Übersicht: invoice drafts waiting for prices or to be issued, scanned reports to check."""
    from aeradex.plugins import Todo
    from . import rapporte
    out = []
    drafts = entwuerfe.overview(book)
    if drafts:
        missing = sum(1 for d in drafts if d["ohne_preis"])
        out.append(Todo(f"{len(drafts)} Rechnungsentw{'ürfe' if len(drafts) > 1 else 'urf'} offen",
                        "/p/leistungen/entwuerfe", f"{missing} mit fehlenden Preisen" if missing else "bereit zum Ausstellen",
                        "Verkauf", "warn" if missing else "info"))
    todo = rapporte.to_check(book)
    if todo:
        out.append(Todo(f"{len(todo)} gescannte{'r' if len(todo) == 1 else ''} Rapport{'e' if len(todo) > 1 else ''} prüfen",
                        "/p/leistungen/rapporte", ", ".join(r["nummer"] for r in todo[:4]), "Leistungen", "warn"))
    return out


@hookimpl
def aeradex_datensicherung(book):
    from aeradex.datensicherung import liste

    def in_year(items, jahr, field="datum"):
        return [e for e in items if jahr is None or str(e.get(field) or "")[:4] == str(jahr)]
    return [
        liste("leistungen", "Verkauf", "Erfasste Leistungen", lambda b, jahr: in_year(daten.entries(b), jahr),
              [("id", "ID"), ("datum", "Datum"), ("wer", "Wer"), ("kunde", "Kunde"), ("projekt", "Projekt"),
               ("art", "Art"), ("text", "Text"), ("menge", "Menge"), ("betrag", "Betrag"), ("status", "Status")],
              ("leistungen/**",), True, "Zeiten und Material je Kunde/Projekt; Einstellungen, Produkte, "
              "Abwesenheiten bei «Dateien»"),
        liste("projekte", "Verkauf", "Projekte", lambda b, jahr: [{"nr": nr, **m} for nr, m in sorted(daten.projects(b).items())],
              [("nr", "Nr"), ("name", "Name"), ("kunde", "Kunde"), ("abrechnung", "Abrechnung"), ("status", "Status")],
              ("projekte/**",)),
        liste("offerten", "Verkauf", "Offerten", lambda b, jahr: in_year(
              [{"nummer": nr, **m} for nr, m in sorted(offerten.quotes(b).items())], jahr),
              [("nummer", "Nummer"), ("datum", "Datum"), ("kunde", "Kunde"), ("titel", "Titel"), ("total", "Total"),
               ("status", "Status")], ("offerten/**",), True),
        liste("produkte", "Verkauf", "Produkte und Leistungsarten",
              lambda b, jahr: [{"nr": nr, **m} for nr, m in sorted(daten.products(b).items())],
              [("nr", "Nr"), ("text", "Text"), ("preis", "Preis"), ("einheit", "Einheit"), ("konto", "Konto")]),
    ]


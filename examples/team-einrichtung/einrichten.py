"""Ein Buch mit Team-App aus einer YAML-Datei anlegen: Firma, Leistungen, Mitarbeitende, App-Konten.

    python einrichten.py ~/buchhaltung/muster                   # mit muster.yaml
    python einrichten.py ~/buchhaltung/firma --daten firma.yaml

Braucht aeradex mit den Plugins leistungen und team. Legt nur ein neues Buch an; ein bestehender Ordner wird nie
überschrieben. Am Ende stehen die Punkte, die noch in der Oberfläche zu ergänzen sind.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import yaml
from aeradex import api
from aeradex.book import Book
from aeradex_leistungen import daten as leistungen
from aeradex_team import daten as team

HERE = Path(__file__).resolve().parent


def W(root: Path, message: str, fn, *args, **kw):
    return api.write(Book(root), message, fn, *args, **kw).get("ergebnis")


def _set_categories(book: Book, kategorien: list[str]):
    cfg = leistungen.settings(book)
    cfg["kategorien"] = list(dict.fromkeys(kategorien))
    return cfg["kategorien"], [leistungen.save_settings(book, cfg)]


def build(root: Path, d: dict) -> list[str]:
    todo = []
    f = d["firma"]
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} existiert schon und ist nicht leer — nichts geändert")
    extra = {k: str(f[k]) for k in ("uid", "iban", "telefon", "email") if f.get(k)}
    api.init_book(root, f["name"], int(f["jahr"]), rechtsform=f["rechtsform"], strasse=f["strasse"], nr=str(f["nr"]),
                  plz=str(f["plz"]), ort=f["ort"], **extra)
    todo += [f"Firma: {k.upper()} fehlt (Einstellungen)" for k in ("uid", "iban") if not f.get(k)]
    if d.get("mwst", "keine") != "keine":
        api.settings_update(Book(root), mwst={"methode": d["mwst"]})
    else:
        todo.append("MWST: Status/Methode klären (Einstellungen › MWST)")

    for name in ("leistungen", "team"):
        api.plugin_enable(Book(root), name)

    l = d["leistungen"]
    W(root, "Leistungen: Grundeinstellungen", leistungen.set_options, None, l["ferien_tage"], l["feiertage_kanton"],
      "projekt" if l.get("objekte") else None, projekt_wort="Objekt" if l.get("objekte") else None,
      projekt_pflicht=bool(l.get("objekte")), rechnung_tage=bool(l.get("objekte")))
    W(root, "Leistungen: Überstundenkontrolle ab", leistungen.set_holidays, None, str(l["kontrolle_ab"]))
    W(root, "Leistungen: Kategorien", _set_categories, l["kategorien"])
    for item in l["leistungsarten"]:
        W(root, f"Leistungsart {item['text']}", leistungen.add_product, item["text"], str(item.get("preis") or 0), "h")
    for item in l["material"]:
        W(root, f"Material {item['text']}", leistungen.add_product, item["text"], str(item.get("preis") or 0),
          item["einheit"])
    if l.get("leistungsart_fest"):
        fixed = next(nr for nr, p in leistungen.services(Book(root)).items() if p.get("text") == l["leistungsart_fest"])
        W(root, f"Team: Leistungsart immer {l['leistungsart_fest']}", team.set_fixed_service, fixed)
    if any(not Decimal(str(i.get("preis") or 0)) for i in l["leistungsarten"] + l["material"]):
        todo.append("Preise der Leistungsarten und des Materials (Plugins › Leistungen › Stammdaten)")
    if d.get("team"):
        W(root, "Team: Branche und Texte", team.set_texts, d["team"])

    owner = d.get("inhaber")
    if owner:
        nr = W(root, f"Person {owner['name']}", leistungen.set_person, "", None, None, owner["name"])["nummer"]
        W(root, f"Team: App-Konto {owner['name']}", team.set_account, nr, owner["benutzer"])

    for emp in d.get("mitarbeitende") or []:
        res = api.employee_add(Book(root), emp["vorname"], emp["nachname"], pensum=100,
                               vollzeit_stunden_woche=l["wochenstunden"], eintritt=f"{f['jahr']}-01-01")
        nr = res["mitarbeiter"]["nummer"]
        if emp.get("ferien_tage"):
            W(root, f"Ferien {nr}", leistungen.set_person, nr, ferien_tage=emp["ferien_tage"])
        W(root, f"Team: App-Konto {nr}", team.set_account, nr, emp["benutzer"])
        todo.append(f"{nr} {emp['vorname']} {emp['nachname']}: Lohn, Pensum, AHV-Nr., Geburtsdatum, Adresse, "
                    f"Eintritt (Lohn › Mitarbeitende)")
    todo.append("Eröffnungsbilanz (Bank, Kasse, Eigenkapital) im Kontenplan")
    todo.append("Wochenstunden und Ferientage mit Arbeitsvertrag bzw. GAV abgleichen")
    return todo


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("ziel", type=Path)
    p.add_argument("--daten", type=Path, default=HERE / "muster.yaml")
    args = p.parse_args(argv)
    d = yaml.safe_load(args.daten.read_text(encoding="utf-8"))
    root = args.ziel.expanduser().resolve()
    todo = build(root, d)
    print(f"Buch angelegt: {root}")
    print(f"Starten:  aeradex --buch {root} ui")
    print("Noch zu ergänzen:")
    for t in todo:
        print(f"  - {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

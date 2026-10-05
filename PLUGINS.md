# Plugins

Plugins erweitern allkvitt, ohne allkvitt zu verändern. Wie man eines baut: [docs/plugins.md](docs/plugins.md).
Massgebend ist der Katalog `src/allkvitt/data/plugin_katalog.json` (in allkvitt: Einstellungen → Plugins → Katalog);
eigenes Plugin per Pull Request dort eintragen, ein Maintainer prüft es (siehe docs/plugins.md).

| Plugin | Art | Was es tut | Stand |
|---|---|---|---|
| [allkvitt-anlagen](plugins/allkvitt-anlagen) | Dokumente, Seite | Anlagenbuchhaltung: Abschreibungen, Abgänge, Anlagenspiegel | ungeprüft |
| [allkvitt-leistungen](plugins/allkvitt-leistungen) | Seiten, Werkzeuge | Offerten, Zeiterfassung, Produkte, Projekte, Stundenkontrolle, Abrechnung an Kunden | ungeprüft |
| [allkvitt-revolut](plugins/allkvitt-revolut) | Bankformat | Revolut-Kontoauszüge (CSV, Business und Privat) | ungeprüft, mit echtem Export zu prüfen |

## Gesucht

Gute erste Plugins — wer eines baut, trägt es hier ein:

- **Bankformate:** PostFinance-CSV, Wise, TWINT-Abrechnungen, Stripe- und SumUp-Auszahlungen, Kreditkartenabrechnungen
- **Quellensteuer-Tabellen** weiterer Kantone (Datenplugin, Format wie `src/allkvitt/data/qst_tarife/BS-2026.json`,
  einlesbar mit `allkvitt qst import`)
- **Kontenpläne:** Einzelfirma, Genossenschaft, Stiftung, Landwirtschaft, Gastro
- **Dokumente mit Buchungen:** Lager, Projekte und Kostenstellen, Leasing
- **Exporte:** Banana, Bexio, Abacus, Treuhänder-Paket

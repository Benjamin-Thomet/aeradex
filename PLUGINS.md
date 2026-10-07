# Plugins

Plugins erweitern aeradex, ohne aeradex zu verändern. Wie man eines baut: [docs/plugins.md](docs/plugins.md).
Massgebend ist der Katalog `src/aeradex/data/plugin_katalog.json` (in aeradex: Einstellungen → Plugins → Katalog);
eigenes Plugin per Pull Request dort eintragen, ein Maintainer prüft es (siehe docs/plugins.md).

| Plugin | Art | Was es tut | Stand |
|---|---|---|---|
| [aeradex-anlagen](plugins/aeradex-anlagen) | Dokumente, Seite | Anlagenbuchhaltung: Abschreibungen, Abgänge, Anlagenspiegel | ungeprüft |
| [aeradex-leistungen](plugins/aeradex-leistungen) | Seiten, Werkzeuge | Offerten, Zeiterfassung, Produkte, Projekte, Stundenkontrolle, Abrechnung an Kunden | ungeprüft |
| [aeradex-revolut](plugins/aeradex-revolut) | Bankformat | Revolut-Kontoauszüge (CSV, Business und Privat) | ungeprüft, mit echtem Export zu prüfen |

## Gesucht

Gute erste Plugins — wer eines baut, trägt es hier ein:

- **Bankformate:** PostFinance-CSV, Wise, TWINT-Abrechnungen, Stripe- und SumUp-Auszahlungen, Kreditkartenabrechnungen
- **Quellensteuer-Tabellen** weiterer Kantone (Datenplugin, Format wie `src/aeradex/data/qst_tarife/BS-2026.json`,
  einlesbar mit `aeradex qst import`)
- **Kontenpläne:** Einzelfirma, Genossenschaft, Stiftung, Landwirtschaft, Gastro
- **Dokumente mit Buchungen:** Lager, Projekte und Kostenstellen, Leasing
- **Exporte:** Banana, Bexio, Abacus, Treuhänder-Paket

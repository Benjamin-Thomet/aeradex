# Plugins

Plugins erweitern batzen, ohne batzen zu verändern. Wie man eines baut: [docs/plugins.md](docs/plugins.md).
Eigenes Plugin eintragen: Pull Request auf diese Datei.

| Plugin | Art | Was es tut | Stand |
|---|---|---|---|
| [batzen-kontenplan-verein](plugins/batzen-kontenplan-verein) | Daten | Kontenplan für Schweizer Vereine | Beispiel im Repo |
| [batzen-revolut](plugins/batzen-revolut) | Bankformat | Revolut-Kontoauszüge (CSV, Business und Privat) | Beispiel im Repo, mit echtem Export zu prüfen |

## Gesucht

Gute erste Plugins — wer eines baut, trägt es hier ein:

- **Bankformate:** PostFinance-CSV, Wise, TWINT-Abrechnungen, Stripe- und SumUp-Auszahlungen, Kreditkartenabrechnungen
- **Quellensteuer-Tabellen** weiterer Kantone (Datenplugin, Format wie `src/batzen/data/qst_tarife/BS-2026.json`,
  einlesbar mit `batzen qst import`)
- **Kontenpläne:** Einzelfirma, Genossenschaft, Stiftung, Landwirtschaft, Gastro
- **Dokumente mit Buchungen:** Anlagenbuchhaltung mit Abschreibungen, Lager, Projekte und Kostenstellen
- **Exporte:** Banana, Bexio, Abacus, Treuhänder-Paket

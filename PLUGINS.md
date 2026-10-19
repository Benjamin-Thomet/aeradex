# Plugins

Plugins erweitern aeradex, ohne aeradex zu verändern. Wie man eines baut: [docs/plugins.md](docs/plugins.md).
Massgebend ist der Katalog `src/aeradex/data/plugin_katalog.json` (in aeradex: Einstellungen → Plugins → Katalog);
eigenes Plugin per Pull Request dort eintragen, ein Maintainer prüft es (siehe docs/plugins.md).

| Plugin | Art | Was es tut | Stand |
|---|---|---|---|
| [aeradex-anlagen](plugins/aeradex-anlagen) | Dokumente, Seite, Werkzeuge | Anlagenbuchhaltung: Anschaffungen aus dem Belegeingang, Bestand übernehmen, Abschreibungen (linear/degressiv), Abgänge, Anlagenspiegel (auch in den Abschlussunterlagen) | ungeprüft |
| [aeradex-leistungen](plugins/aeradex-leistungen) | Seiten, Werkzeuge | Zeit und Material (Schnellerfassung, Stoppuhr, Wochenraster), Ferien und Überzeit mit kantonalen Feiertagen, Arbeitszeitnachweis, Offerten, Projekte, Abrechnung als QR-Rechnung | ungeprüft |
| [aeradex-revolut](plugins/aeradex-revolut) | Bankformat | Revolut-Kontoauszüge (CSV, Business und Privat) | ungeprüft, mit echtem Export zu prüfen |
| [aeradex-team](plugins/aeradex-team) | Seite, Werkzeuge, Dienst | Team-App: Mitarbeitende erfassen Zeit, Material und Abwesenheiten auf dem Handy (Portal [aeradex-team-portal](plugins/aeradex-team-portal)), Anträge, Prüfliste, Überstunden, Lohndokumente Ende-zu-Ende verschlüsselt | ungeprüft |
| [aeradex-zugriff](plugins/aeradex-zugriff) | Seite | Zugriff für Claude Code auf ein Buch auf dem Server: SSH-Schlüssel, beschränkt auf die Werkzeuge und den Belegeingang | ungeprüft |
| [aeradex-anruf](plugins/aeradex-anruf) | Seite, Werkzeuge | Sekretärin mit Stimme (ElevenLabs), Gedächtnis und Karten zum Bestätigen | ungeprüft |
| [aeradex-telegram](plugins/aeradex-telegram) | Befehl | Telegram-Bot für aeradex-anruf: Chat, Sprachnachrichten, Belege, Anrufen | ungeprüft |

## Gesucht

Gute erste Plugins — wer eines baut, trägt es hier ein:

- **Bankformate:** PostFinance-CSV, Wise, TWINT-Abrechnungen, Stripe- und SumUp-Auszahlungen, Kreditkartenabrechnungen
- **Quellensteuer-Tabellen** weiterer Kantone (Datenplugin, Format wie `src/aeradex/data/qst_tarife/BS-2026.json`,
  einlesbar mit `aeradex qst import`)
- **Kontenpläne:** Einzelfirma, Genossenschaft, Stiftung, Landwirtschaft, Gastro
- **Dokumente mit Buchungen:** Lager, Projekte und Kostenstellen, Leasing
- **Exporte:** Banana, Bexio, Abacus, Treuhänder-Paket

# aeradex-ebics

Kontoauszüge automatisch von der Bank: EBICS 3.0 (H005), camt.053 «Tagesend-Kontoauszug» (BTF `EOP` / `CH` / `ZIP`).
Was die Bank liefert, läuft durch den normalen Bankimport von aeradex: abgelegt unter `bank/auszuege/`, Zahlungen
zu Rechnungen und Kreditoren gebucht oder abgeglichen, nichts doppelt.

Dazu **Zahlungen an die Bank**: die pain.001-Dateien, die aeradex schon erzeugt (Zahlungsläufe im Einkauf,
Lohnzahlungsdateien), mit der elektronischen Unterschrift (A006) senden (BTF `MCT` / `CH` / pain.001 v09) und die
Statusmeldungen der Bank (pain.002, `PSR`) beim nächsten Abholen lesen.

Eigener EBICS-Client (keine Fremdbibliothek): `cryptography` + `lxml`.

## Einrichten (einmal, unter Bank › EBICS, nur Admins)

1. **Verbindung** aus dem Brief «EBICS-Verbindungsparameter» der Bank: URL, Host-ID, Partner-ID, Teilnehmer-ID,
   dazu die vier Hashwerte der Bank. Beim Speichern entstehen die drei Schlüssel (A006, X002, E002, RSA 2048,
   selbst signierte Zertifikate — in der Schweiz zulässig).
2. **Senden**: INI und HIA gehen an die Bank. Geht nur einmal.
3. **Brief**: Initialisierungsbrief (PDF) drucken, unterschreiben, der Bank schicken. Die Bank schaltet frei.
4. **Bankschlüssel holen** (HPB). Stimmen alle vier Hashwerte mit dem Brief der Bank überein, ist EBICS sofort
   aktiv; fehlen Werte, vergleicht ein Mensch und bestätigt. Weicht einer ab, wird nichts übernommen.
5. **Aktiv**: «Jetzt abholen» oder der Timer (`aeradex ebics abholen --still`, z.B. dreimal täglich).

Dasselbe auf der Kommandozeile: `aeradex ebics einrichten|senden --ja|brief|bankschluessel|bestaetigen|abholen|status|protokoll`.

## Zahlungen senden

Bank › EBICS › «Zahlungen an die Bank»: jede Zahlungsdatei mit Ausführungsdatum, Anzahl, Total und Stand bei der
Bank; «An Bank senden» nach Rückfrage. Schutz vor Fehlern, weil hier Geld das Konto verlässt:

- nur ein Mensch sendet (Admin in der Oberfläche oder `aeradex ebics zahlung-senden --datei … --total … --ja`), nie der Agent
- nur Dateien, die aeradex erstellt hat (`zahlungen/`, Lohnzahlungen mit Manifest und unverändertem SHA-256)
- jede Datei nur einmal (nach Inhalt und MsgId); bestätigtes Total muss dem CtrlSum der Datei entsprechen
- kein Ausführungsdatum in der Vergangenheit
- abgelehnte Zahlungen (pain.002 RJCT/PART) erscheinen rot in der To-do-Liste

Gebucht wird die Zahlung wie bisher, wenn sie im Kontoauszug erscheint.

## Wo was liegt

Nichts davon kommt ins Buch (git): `<buch>/.aeradex/lokal/ebics/`

| Datei | Inhalt |
|---|---|
| `teilnehmer.yaml` | Verbindung, Hashwerte der Bank, Stand der Schritte, letzter Abruf |
| `schluessel.yaml` | die privaten Schlüssel (0600; verschlüsselt, wenn `AERADEX_EBICS_PASSWORT` gesetzt ist) und die Zertifikate |
| `eingang/` | Geliefertes, bis es importiert ist (ein Fehler beim Import lässt die Datei für den nächsten Lauf liegen) |
| `archiv/` | Importiertes; Auszüge, die vor dem ersten Buchjahr enden, nur hier |
| `zahlungen.yaml` | gesendete Zahlungsdateien (SHA-256, MsgId, Auftrag, wer, wann, Status der Bank) |
| `status/` | Statusmeldungen der Bank (pain.002) |
| `protokoll.jsonl` | jeder Austausch mit der Bank |

Ohne Datumsbereich liefert die Bank alles, was noch nicht abgeholt wurde; bestätigt (Quittung) wird erst, wenn die
Dateien gespeichert sind. Mit `--von/--bis` lässt sich ein Zeitraum nochmals holen (der Import erkennt Doppel).

## Tests

`pytest` — eine Testbank prüft jede Nachricht gegen die offiziellen H005-Schemas (`tests/xsd/`) und die Signaturen;
die Kanonisierung (C14N) ist zusätzlich gegen echte, von Banken signierte Antworten geprüft (`tests/fixtures/`,
aus dem Ruby-Client [epics](https://github.com/railslove/epics), MIT).

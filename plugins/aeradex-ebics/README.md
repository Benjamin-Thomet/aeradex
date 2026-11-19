# aeradex-ebics

Kontoauszüge automatisch von der Bank: EBICS 3.0 (H005), camt.053 «Tagesend-Kontoauszug» (BTF `EOP` / `CH` / `ZIP`).
Was die Bank liefert, läuft durch den normalen Bankimport von aeradex: abgelegt unter `bank/auszuege/`, Zahlungen
zu Rechnungen und Kreditoren gebucht oder abgeglichen, nichts doppelt.

Eigener EBICS-Client (keine Fremdbibliothek): `cryptography` + `lxml`. Nur Abholen — Zahlungen hochladen
(pain.001) ist nicht Teil davon.

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

## Wo was liegt

Nichts davon kommt ins Buch (git): `<buch>/.aeradex/lokal/ebics/`

| Datei | Inhalt |
|---|---|
| `teilnehmer.yaml` | Verbindung, Hashwerte der Bank, Stand der Schritte, letzter Abruf |
| `schluessel.yaml` | die privaten Schlüssel (0600; verschlüsselt, wenn `AERADEX_EBICS_PASSWORT` gesetzt ist) und die Zertifikate |
| `eingang/` | Geliefertes, bis es importiert ist (ein Fehler beim Import lässt die Datei für den nächsten Lauf liegen) |
| `archiv/` | Importiertes; Auszüge, die vor dem ersten Buchjahr enden, nur hier |
| `protokoll.jsonl` | jeder Austausch mit der Bank |

Ohne Datumsbereich liefert die Bank alles, was noch nicht abgeholt wurde; bestätigt (Quittung) wird erst, wenn die
Dateien gespeichert sind. Mit `--von/--bis` lässt sich ein Zeitraum nochmals holen (der Import erkennt Doppel).

## Tests

`pytest` — eine Testbank prüft jede Nachricht gegen die offiziellen H005-Schemas (`tests/xsd/`) und die Signaturen;
die Kanonisierung (C14N) ist zusätzlich gegen echte, von Banken signierte Antworten geprüft (`tests/fixtures/`,
aus dem Ruby-Client [epics](https://github.com/railslove/epics), MIT).

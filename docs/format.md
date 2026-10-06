# Dateiformat eines allkvitt-Buchs

Alle Dateien sind UTF-8. Beträge stehen in Dateien immer ohne Tausendertrennzeichen mit Punkt und zwei Nachkommastellen (`1234.50`); beim Einlesen werden `1'234.50` und `1234.5` ebenfalls akzeptiert. Datum: `JJJJ-MM-TT`.

Details zu Transaktionen, Wiederherstellung, Formatversionen und Agenten-Wiederholungen: [Textspeicher](storage.md).

## allkvitt.yaml

```yaml
format_version: 1            # ohne Feld: altes Format, Migration beim nächsten erfolgreichen Schreiben
firma: Muster GmbH
rechtsform: GmbH
uid: CHE-123.456.789          # mit Suffix " MWST" = MWST-pflichtig
adresse: {strasse: Bahnhofstrasse, nr: '1', plz: '3000', ort: Bern, land: CH}
iban: CH93 0076 2011 6238 5295 7   # QR-IBAN → QRR-Referenz, sonst SCOR (RF…)
qr_referenz_praefix: ''
waehrung: CHF
zahlungsfrist_tage: 30
erstes_jahr: 2026             # Eröffnungssalden im Kontenplan gelten für dieses Jahr
sperre_bis: null              # nur über `allkvitt lock` / `allkvitt unlock` ändern
agent_modus: vorschlag        # vorschlag | direkt
konten:                       # Systemkonten
  bank: '1020'
  debitoren: '1100'
  ertrag: '3400'
  gutschrift: '3800'
  gewinnvortrag: '2970'
  jahresergebnis: '2979'
  dividende: '2261'
  reserve: '2950'
plugins: [revolut]          # optional: Plugins, die dieses Buch braucht (siehe docs/plugins.md)
```

Steht ein Plugin unter `plugins:`, das nicht installiert ist, meldet `allkvitt check` einen Fehler — Buchungen, die
dem Plugin gehören, könnten sonst nicht geprüft werden. Plugin-Einstellungen stehen unter dem Plugin-Namen
(z.B. `revolut: {konten: {CHF: "1022"}}`).

## kontenplan.yaml

```yaml
konten:
- {nr: "1020", name: Bank, klasse: aktiv, eroeffnung: 20000}
- {nr: "2800", name: Stammkapital, klasse: passiv, eroeffnung: -20000}
- {nr: "1440", name: Darlehen Gesellschafter, klasse: aktiv, gruppe: finanzanlagen_nahe}
```

- `klasse`: `aktiv | passiv | aufwand | ertrag`
- Vorzeichen: Aktiven und Aufwand positiv, Passiven und Ertrag negativ (wie Banana).
- `gruppe`: Zeile in Bilanz/Erfolgsrechnung. Standard aus der Kontonummer (KMU-Kontenrahmen), Codes siehe `statements.GROUPS`.
- `gruppe_negativ`: Darstellung bei Vorzeichenwechsel, z.B. Bank im Minus als Bankverbindlichkeit.
- `vorjahr`: Vorjahreszahl für das erste Jahr (Spalte «Vorjahr» der ersten Jahresrechnung).
- `waehrung`: Fremdwährung eines Bilanzkontos (`EUR`, `USD` …), z.B. `{nr: "1021", name: Bank EUR, klasse: aktiv, waehrung: EUR}`.
  Die Buchhaltung bleibt in CHF; jede Zeile auf diesem Konto trägt zusätzlich den Betrag in der Fremdwährung
  und den Kurs. `eroeffnung_fw`: Eröffnungssaldo in der Fremdwährung (`eroeffnung` ist der CHF-Wert).
- `abschluss`: Eigenkapitalkonto, in das der Saldo bei der Eröffnung des Folgejahres übergeht, z.B. bei der
  Einzelfirma `{nr: "2850", name: Privat, klasse: passiv, abschluss: "2800"}`. Das Konto beginnt jedes Jahr bei null.

Vorlagen (`allkvitt init --rechtsform …` wählt die passende, `--kontenplan` übersteuert): `kmu` für AG und GmbH,
`einzelfirma` (Eigenkapital 2800, Privat 2850, Privat AHV 2851, Privat Steuern 2852; Ergebnis direkt ins
Eigenkapital, keine Gewinnverwendung) und `verein` (Vereinsvermögen, Fonds, Mitgliederbeiträge).

## journal/JJJJ/JJJJ-MM.md

```markdown
# Journal Januar 2026

| Datum      | Beleg  | Text         | Soll | Haben |  Betrag | FW          | Kurs    | MWST | Quelle |
| ---------- | ------ | ------------ | ---- | ----- | ------: | ----------- | ------- | ---- | ------ |
| 2026-01-05 | 26-001 | Büromaterial | 6500 | 1020  |   45.80 |             |         |      |        |
| 2026-01-10 | 26-002 | Einkauf Coop | 6500 |       |   30.00 |             |         |      |        |
| 2026-01-10 | 26-002 | Einkauf Coop | 6641 |       |   20.00 |             |         |      |        |
| 2026-01-10 | 26-002 | Einkauf Coop |      | 1000  |   50.00 |             |         |      |        |
| 2026-01-12 | 26-003 | Papier       | 6500 |       |  100.00 |             |         | V81  |        |
| 2026-01-12 | 26-003 | Papier       | 1170 |       |    8.10 |             |         | V81  |        |
| 2026-01-12 | 26-003 | Papier       |      | 1020  |  108.10 |             |         |      |        |
| 2026-01-20 | 26-004 | Verkauf DE   | 1021 | 3200  |  944.45 | EUR 1000.00 | 0.94445 |      |        |
```

- Eine Zeile mit Soll **und** Haben ist eine einfache Buchung.
- Sammelbuchung: mehrere Zeilen mit derselben Belegnummer, jede nur mit Soll oder nur mit Haben. Der Beleg als Ganzes muss aufgehen.
- `Quelle` leer = manuelle Buchung. Sonst gehört die Zeile einem Dokument:
  `rechnung:R-2026-0001`, `zahlung:R-2026-0001`, `gutschrift:R-2026-0001`, `lohn:2026-01:M0001`, `abschluss:2026`, `bewertung:2026-12-31`.
- `FW`, `Kurs`: bei Fremdwährung der Betrag in der Währung und der Kurs (CHF je Einheit); `Betrag` ist immer CHF
  und muss `FW × Kurs` auf den Rappen entsprechen. Pflicht für jede Zeile auf einem Konto mit `waehrung`.
- `MWST`: Code der Zeile (siehe unten). Ältere Dateien ohne diese Spalte bleiben gültig; sie wird beim nächsten Schreiben ergänzt.
- Text vor und nach der Tabelle bleibt erhalten. Weitere Spalten dürfen ergänzt werden, sie werden mitgeführt.

## Fremdwährungen: Kurse und Bewertung

Kurse sind die Tageskurse des BAZG (dieselben, die die ESTV für die MWST verwendet):
`https://www.backend-rates.bazg.admin.ch/api/xmldaily?d=JJJJMMTT`. Ohne Kurs bucht allkvitt zum Kurs des
Buchungsdatums (Wochenende/Feiertag: letzter publizierter Tag); abgerufene Tabellen liegen unter
`.allkvitt/kurse/JJJJ-MM-TT.yaml`. Vorschläge des Agenten halten den Kurs beim Vorschlagen fest.

Per Stichtag (meist 31.12.) bewertet `allkvitt bewertung 2026-12-31 --buchen` (bzw. Abschluss → Fremdwährungen)
jedes Fremdwährungskonto zum BAZG-Kurs dieses Tages: Saldo FW × Kurs gegen den CHF-Buchwert, die Differenz geht auf
Kursgewinn (6952) bzw. Kursverlust (6942) (`konten.kursgewinn`/`kursverlust` in allkvitt.yaml). Die Bewertung liegt
unter `bewertung/JJJJ-MM-TT.yaml` und gehört ihren Journalzeilen (`Quelle bewertung:…`, FW 0.00, Kurs des Stichtags).
`check` meldet Jahre mit Fremdwährungskonten ohne Bewertung per 31.12.

## MWST

`allkvitt.yaml`:

```yaml
mwst:
  methode: effektiv        # keine | effektiv | saldo
  periode: quartal         # quartal | semester
  abrechnungsart: vereinbart # vereinbart | vereinnahmt
  saldosteuersatz: 6.2     # nur bei saldo
  konten: {vorsteuer: "1170", vorsteuer_inv: "1171", umsatzsteuer: "2200", abrechnung: "2201", saldosteuer: "3809"}
```

| Code | Bedeutung | Ziffer |
|---|---|---|
| U81 / U26 / U38 | Umsatz 8.1 / 2.6 / 3.8 % | 303 / 313 / 343 (Saldo: 322) |
| U0 | steuerbefreit (Export) | 220 |
| UA | von der Steuer ausgenommen | 230 |
| V81 / V26 / V38 | Vorsteuer Material und Dienstleistungen | 400 |
| I81 / I26 / I38 | Vorsteuer Investitionen und übriger Betriebsaufwand | 405 |
| B81 / B26 | Bezugsteuer: Dienstleistungen aus dem Ausland (Art. 45) | 382 / 383 (effektiv: Vorsteuer 400 bei 4xxx-Konten, sonst 405) |

Bezugsteuer: die Aufwandzeile trägt den Code (Bemessungsgrundlage), dazu die geschuldete Steuer auf
`konten.bezugsteuer` (Standard: Umsatzsteuerkonto) und — effektive Methode — ihr Abzug als Vorsteuer; bei der
Saldosteuersatzmethode ist die Steuer Aufwand auf demselben Konto.

Gebucht wird brutto mit Code; bei der effektiven Methode spaltet allkvitt die Steuer ab (Netto- und Steuerzeile tragen beide den Code). Bei der Saldosteuersatzmethode bleibt der Umsatz brutto und die Saldosteuer wird mit der Abrechnung gebucht. Gebuchte Abrechnungen liegen unter `mwst/<Periode>.yaml` und gehören ihren Journalzeilen (`Quelle mwst:2026-Q1`).

Bei `vereinnahmt` bleiben Rechnungen und Kreditoren im Hauptbuch am Belegdatum; die MWST-Abrechnung
berücksichtigt sie anteilig am Zahlungsdatum. `allkvitt mwst abstimmung <Jahr> --pdf` vergleicht die
Buchhaltung mit den gebuchten Abrechnungen und legt das PDF unter `berichte/` ab.

Die Abgrenzung der Steuer auf offenen Posten liegt unter `mwst/abgrenzung/<Jahr>.yaml`; sie besitzt
die Zeilen mit `Quelle mwst:abgrenzung-<Jahr>` (31.12. und Rückbuchung 1.1.). Die Konten
`mwst.konten.umsatzsteuer_offen` (Standard 2209) und `mwst.konten.vorsteuer_offen` (Standard 1172)
werden beim ersten Buchen angelegt, wenn sie fehlen. `allkvitt check` prüft die gespeicherten Zeilen
und meldet nachträglich geänderte offene Posten.

## vorschlaege.md

Gleiche Tabelle plus `ID` und `Begründung`. `allkvitt approve V-001` verschiebt die Zeile ins Journal.

## kunden/K0001-name.md

```yaml
---
nummer: K0001
name: Anna Beispiel          # Kontaktperson
firma: Beispiel AG
rechnung_an: firma           # firma | person
adresse: {strasse: Marktgasse, nr: '5', plz: '3011', ort: Bern, land: CH}
email: anna@example.ch
---
Freie Notizen.
```

## rechnungen/JJJJ/R-JJJJ-NNNN.md

Frontmatter mit `nummer, kunde, an` (Adress-Snapshot), `datum, faellig, waehrung, positionen[], total, referenz_typ, referenz, debitorenkonto, status, fingerprint`. Der Text nach dem Frontmatter erscheint als Einleitung auf der Rechnung. Nach der Ausstellung nur noch `status` (via `void`) änderbar.

Rechnungen in Fremdwährung (CHF oder EUR für den QR-Zahlteil) tragen `waehrung` und `kurs`; ihre Journalzeilen
sind in CHF mit FW und Kurs, Zahlungen gleichen den Buchwert aus (Differenz auf Kursgewinn/-verlust).

Rechnungen, die ausserhalb von allkvitt erstellt wurden, tragen `extern: {rechnungsnr: …}` und `datei` (das Original
unter belege/); sie haben eine eigene allkvitt-Nummer und werden sonst wie alle Rechnungen behandelt.

## personal/M0001-name.md

```yaml
---
nummer: M0001
vorname: Lea
nachname: Muster
adresse: {strasse: Weg, nr: '2', plz: '3000', ort: Bern, land: CH}
ahv_nr: 756.1234.5678.97
geburtsdatum: '1990-04-01'
eintritt: '2026-01-15'
austritt: null
lohnart: monat               # monat | stunde
monatslohn: 6000             # 100 %-Lohn
pensum: 80
stundenlohn: 0
standard_stunden: 0
vollzeit_stunden_woche: 42   # für das eigene Pensum bei Stundenlohn (QST)
ferienzuschlag_satz: 0       # 0.0833 = 4 Wochen, 0.1064 = 5 Wochen
ferien_inbegriffen: false
bvg_betrag: 250              # AN-Beitrag CHF/Monat
ag_bvg_betrag: 0             # nur bei ag_bvg: betrag
kinderzulagen: 0
qst: {kanton: BS, jahr: 2026, code: A0N}   # oder null und qst_satz: 0.05 (Pauschalsatz)
qst_satz: 0
aktiv: true
---
```

## lohn/JJJJ/MM/M0001.md

Frontmatter: `mitarbeiter, name, jahr, monat, status (entwurf|abgeschlossen), eingaben{stunden, bvg, kinderzulagen, korrektur, korrektur_text, qst_satzbestimmend, qst_gesamtpensum}, werte{…}`, nach Abschluss zusätzlich `ag{…}` und `fingerprint`. Der Text darunter wird erzeugt. Eingaben ändert man im Entwurf und rechnet mit `allkvitt payroll run` neu.

## Jev (optional)

```yaml
jev: {aktiv: true, schwelle: 0.7, modell: jev-latest}   # plus TYPESAFE_API_KEY in der Umgebung
```

## lieferanten/L0001-name.md

Frontmatter: `nummer, name, adresse{…}, iban, konto` (Standard-Aufwandkonto), `mwst` (Standard-Code), `email`.

## eingang/ENT-NNNN.yaml

Eingelesene, noch nicht gebuchte Belege (Entwürfe aus v0.6 unter `kreditoren/entwuerfe/` werden weiter gelesen).
`art`: `kreditor` (Lieferantenrechnung), `quittung` (bereits bezahlt) oder `debitor` (eigene, extern erstellte
Rechnung). Quittungen tragen `zahlung`: `{art: bank, bank: <ID>}` (offene Bankbewegung), `{art: buchung, beleg: …}`
(schon gebucht, nur ablegen) oder `{art: konto, konto: …}`. `datei` (meist inbox/…), `felder` mit je `wert` und
`quelle` (QR, Text, OCR, Lieferant L0001, Agent, Hand oder ein Plugin), `konto` und `mwst` mit Quelle (und
`begruendung`, wenn der Agent kontiert hat), `hinweise`, `konflikt` (Name auf der Rechnung ≠ Inhaber der IBAN) und
`status`: `bereit`, `unsicher` (Konto fehlt), `agent` (Agent arbeitet), `konflikt`, `unvollstaendig`.
Entwürfe besitzen keine Journalzeilen; beim Erfassen wird der Entwurf im selben Commit entfernt.
Der erkannte Text liegt unter `.allkvitt/erfassung/ENT-NNNN.txt` (nicht im git). In allkvitt.yaml schaltet
`kreditoren: {agent_automatisch: false}` die automatische Übergabe an den Agenten aus.

## kreditoren/JJJJ/E-JJJJ-NNNN.md

Frontmatter: `nummer, lieferant, name, rechnungsnr, datum, faellig, betrag` (brutto), `waehrung, iban, referenz_typ`
(QRR, SCOR, NON), `referenz, mitteilung, konto, mwst, kreditorenkonto, status, fingerprint`, nach dem Erfassen
`datei` (Beleg unter belege/), nach einem Zahlungslauf `zahlungslauf`. Gebucht als `Quelle kreditor:E-…`,
die Zahlung als `Quelle kzahlung:E-…` (Kreditoren an Bank).

Fremdwährung: `waehrung` (EUR …) und `kurs` (BAZG am Rechnungsdatum, sofern nicht angegeben); `betrag` ist in der
Rechnungswährung, die Journalzeilen sind in CHF mit FW und Kurs. Aufteilung: `positionen: [{konto, betrag, mwst,
text}]`, Summe = `betrag`. Beide Felder fliessen nur in den Fingerprint ein, wenn sie vorhanden sind — ältere
Kreditoren bleiben gültig. Die Zahlung einer Fremdwährungsrechnung besteht aus der Kreditorenzeile (Buchwert, FW
beglichen, Buchkurs), der Bankzeile (bezahlter Betrag) und, falls nötig, einer Zeile auf Kursgewinn/-verlust.
Offene Fremdwährungs-Kreditoren stehen in `bewertung/<datum>.yaml` unter `kreditoren:`.

## zahlungen/

Zahlungsdateien `JJJJ-MM-TT-xxxxxx.xml` (ISO 20022 pain.001.001.09, Swiss Payment Standards) zum Hochladen im E-Banking.
Eine QR-IBAN kann nicht belastet werden; dann `zahlungs_iban` in `allkvitt.yaml` setzen.

## bank/

`bank/auszuege/<JJJJ>/<Auszug-ID>.<xml|csv|xlsx|pdf>`: die Kontoauszüge wie von der Bank geliefert (sie sind der Beleg).
`bank/<JJJJ>.md`: eine Zeile pro Bankbewegung: `ID | Datum | Konto | Betrag | Gegenpartei | Referenz | Text | Status | Beleg | Auszug | Hinweis`
(`Hinweis`: z.B. unsicherer Jev-Vorschlag),
Status `gebucht`, `abgeglichen`, `offen`, `ignoriert`. `Beleg` verweist auf die Journalbuchung. Zuordnung IBAN → Konto in
`allkvitt.yaml` unter `bankkonten: {CH…: "1020"}` (Standard: die eigene IBAN → Bankkonto).

## bank/formate/<name>.yaml

Ein gelerntes CSV/Excel-Format (vom Agenten beschrieben, von einem Menschen bestätigt). Erkannt wird eine Datei an
ihrer Kopfzeile; gelesen wird ohne Agent.

```yaml
name: UBS Kontoauszug
bestaetigt: true
kopfzeile: [Abschlussdatum, Buchungstext, Beschreibung 2, Belastung, Gutschrift, Saldo]
datum: Abschlussdatum
datumsformat: "%d.%m.%Y"
belastung: Belastung        # oder betrag: <Spalte mit Vorzeichen> (vorzeichen: -1, wenn Belastungen positiv sind)
gutschrift: Gutschrift
text: [Buchungstext, Beschreibung 2]
saldo: Saldo                # optional: Saldo-Prüfung Zeile für Zeile und Schlusssaldo für die Abstimmung
konto: "1020"               # optional, wenn die Datei keine zugeordnete IBAN enthält; je Währung {CHF: "1020", EUR: "1021"}
```

Weitere Felder: `gegenpartei`, `referenz`, `id` (Transaktions-ID), `waehrung` (Spalte), `dezimal: ","`,
`trennzeichen`, `iban`. Ohne `id` erkennt allkvitt doppelt importierte Bewegungen an Datum, Betrag und Text.

## bank/karten/<hash>.yaml

Eine gelesene Kreditkartenabrechnung, benannt nach dem SHA-256 der PDF: `konto`, `herausgeber`, `karte`
(letzte 4 Ziffern), `von`, `bis`, `saldo_alt`, `saldo_neu`, `buchungen: [{datum, text, betrag, original}]` (betrag
wie auf der Abrechnung: Belastungen positiv, Zahlungen negativ). Importiert wird nur, wenn `saldo_alt` + Buchungen =
`saldo_neu` und jeder Betrag im Text der PDF steht; bei einer gescannten PDF ohne Text nach Kontrolle
`bestaetigt: true` setzen.

## .allkvitt/locks.yaml

`bis`, Hash pro gesperrtem Monat, `verlauf` aller Sperren und Entsperrungen (mit Grund).

## .allkvitt/belegnummern.yaml

```yaml
hinweis: Höchste je vergebene Belegnummer pro Jahr. …
letzte:
  '2026': 42
```

Die laufende Belegnummer `JJ-NNN` ist eins höher als die höchste Nummer im Journal, in offenen Vorschlägen und in
dieser Datei. allkvitt führt die Datei beim Buchen und beim Verwerfen eines Vorschlags nach; so wird eine Nummer nie
zweimal vergeben, auch wenn ihre Buchung wieder entfernt wurde. Belegdateien heissen `belege/JJJJ/<Belegnummer> <Name>`;
der Belegordner (`allkvitt dossier`) stempelt jede Seite mit dieser Nummer und listet fehlende Nummern der Serien
`JJ-NNN`, `R-JJJJ-NNNN` und `E-JJJJ-NNNN` mit Grund im Lückenverzeichnis.

## budget/JJJJ.yaml

Budget pro Erfolgskonto; Beträge natürlich (Ertrag und Aufwand positiv). Ein Jahresbetrag wird gleichmässig auf die
Monate verteilt (Rappenrest im Dezember), sonst zwölf Monatswerte. `allkvitt check` meldet unbekannte Konten,
Bilanzkonten und eine Summe der Monate, die nicht zu `jahr` passt.

```yaml
notiz: aus Ist 2026 +5 %
konten:
  "3400": {jahr: 240000}
  "6000": {monate: [2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000, 2000]}
```

## auswertungen/

`auswertungen/vorlagen.yaml` — gespeicherte Berichte (ohne Jahr, damit sie jedes Jahr gelten):

```yaml
Monatsreport Treuhand: {typ: erfolgsrechnung, parameter: {spalten: monat, vergleich: vorjahr}}
```

`auswertungen/kommentare/<bericht-und-parameter>.md` — ein Kommentar zu einem Bericht. Frontmatter `bericht`,
`titel`, `parameter`, `fingerprint` (SHA-256 über die Zahlen des Berichts beim Schreiben), `autor`, `datum`;
der Text ist der Body. Passt der Fingerabdruck nicht mehr zu den aktuellen Zahlen, gilt der Kommentar als veraltet.

Erzeugte Berichte (Monatsbericht, Exporte) landen wie die Abschlussunterlagen in `berichte/` — nicht in git, jederzeit
neu erzeugbar; jeder trägt den git-Commit seines Stands.

## Abschlussunterlagen (`allkvitt dossier`)

ZIP `<Firma> Abschluss <JJJJ>.zip` mit einem Ordner gleichen Namens; Dateien beginnen mit der Nummer ihres Teils
(`00 Inhalt.pdf`, `01 Jahresrechnung 2026.pdf`, `03 Journal 2026.csv`, `05 Belege/26-001 quittung.pdf` …).
CSV: Semikolon, UTF-8 mit BOM, Datum ISO, Beträge mit Punkt ohne Tausendertrenner. `manifest.json`:

```json
{"format": "allkvitt-abschluss", "version": 1, "allkvitt": "0.8.0", "firma": "…", "jahr": 2026,
 "erstellt": "…", "gesperrt_bis": "2026-12-31", "entwurf": false, "commit": "<git HEAD>",
 "uncommittete_aenderungen": false, "pruefung": {"fehler": 0, "warnungen": 0},
 "luecken": [{"beleg": "26-007", "art": "verworfen", "grund": "…"}],
 "dateien": {"00 Inhalt.pdf": "<sha256>", "…": "…"}}
```

Plugins steuern weitere Teile über den Hook `allkvitt_dossier_teile` bei (siehe docs/plugins.md).

## .allkvitt-Datei (Buch weitergeben)

Ein ZIP-Container:

| Eintrag           | Inhalt |
| ----------------- | ------ |
| `mimetype`        | `application/vnd.allkvitt+zip`, erster Eintrag, unkomprimiert |
| `manifest.json`   | `format: allkvitt-buch`, `version: 1`, allkvitt-Version, Firma, UID, Rechtsform, Jahre, Sperre, Plugins, Zeitpunkt, git-Commit, `uncommittete_aenderungen`, `mit_inbox`, `historie` (SHA-256 des Bundles), `dateien` (Pfad → SHA-256) |
| `buch/…`          | alle Dateien des Buchs wie auf der Festplatte, ohne `.git/`, `berichte/` und `.allkvitt/write.lock` |
| `historie.bundle` | `git bundle --all`: der vollständige Änderungsverlauf (optional) |

Verschlüsselt (`--passwort`) enthält die Datei nur `mimetype`, ein `manifest.json` mit `verschluesselt: true` und den
Parametern (`AES-256-GCM`, Schlüssel per `scrypt` n=2^15, r=8, p=1, Salt, Nonce) und `payload.enc`: der obige
Inhalt (ohne `mimetype`) als ZIP, verschlüsselt. Firma und Dateinamen sind dann nicht lesbar.

Import: Zielordner muss neu oder leer sein. Geprüft werden Formatversion, jeder Pfad (nur relativ, kein `..`, nichts
in `.git/`, keine Links), jede Prüfsumme und die Vollständigkeit gegenüber dem Manifest. Mit Verlauf wird das Bundle
geklont; was der Absender nicht committet hatte, wird als eigener Commit festgehalten. Danach `allkvitt check`.

## mahnungen/R-JJJJ-NNNN.yaml

Die Mahnungen einer Rechnung: `mahnungen: [{stufe, bezeichnung, datum, frist, offen, pdf}]`, die PDFs daneben
(`R-JJJJ-NNNN-M1.pdf` …). Mahnungen besitzen keine Journalzeilen. Einstellungen: `mahnwesen: {frist_tage: 10,
texte: {1: …, 2: …, 3: …}}` in allkvitt.yaml (Platzhalter `{frist}`, `{vorher}`, `{nummer}`).

## bank/regeln.yaml

`regeln: [{id, name, gegenpartei, text, betrag, richtung, konto, mwst, buchungstext, aktiv}]` — gesetzte Kriterien
müssen alle passen (Texte als Teil, ohne Gross/Klein; Betrag genau; Richtung `belastung`/`gutschrift`). Eine Regel
greift beim Import erst, wenn keine Rechnung, kein Kreditor und keine bestehende Buchung passt.

## abschluss/dividende-JJJJ.yaml

Die Auszahlung der beschlossenen Dividende: `jahr, datum, brutto, vst` (35 %), `netto, konto, formular` (103),
`frist` (30 Tage). Besitzt ihre Journalzeilen (`Quelle dividende:JJJJ`: Beschlossene Ausschüttungen an Bank bzw. an
Verrechnungssteuer `konten.verrechnungssteuer`, Standard 2206).

## spesen/JJJJ/SP-JJJJ-NNNN.yaml

Spesenbeleg einer Mitarbeiterin/eines Mitarbeiters: `nummer, mitarbeiter, name, datum, text, art` (`reise` →
Lohnausweis 13.1.1, `uebrige` → 13.1.2), `betrag`, `konto`/`mwst` oder `positionen`, optional `waehrung`/`kurs`,
`spesenkonto` (Standard 2210), `datei`. Besitzt seine Journalzeilen (`Quelle spesen:SP-…`: Aufwand an Spesenkonto).
Ausbezahlt ist ein Beleg, wenn eine Lohnabrechnung ihn unter `eingaben.spesen` nennt; abgeschlossen bucht sie
Spesenkonto an Auszahlungskonto (`werte.spesen`, `werte.auszahlung` = Nettolohn + Spesen).

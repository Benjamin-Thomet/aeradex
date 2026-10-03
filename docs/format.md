# Dateiformat eines batzen-Buchs

Alle Dateien sind UTF-8. Beträge stehen in Dateien immer ohne Tausendertrennzeichen mit Punkt und zwei Nachkommastellen (`1234.50`); beim Einlesen werden `1'234.50` und `1234.5` ebenfalls akzeptiert. Datum: `JJJJ-MM-TT`.

## batzen.yaml

```yaml
firma: Muster GmbH
rechtsform: GmbH
uid: CHE-123.456.789          # mit Suffix " MWST" = MWST-pflichtig
adresse: {strasse: Bahnhofstrasse, nr: '1', plz: '3000', ort: Bern, land: CH}
iban: CH93 0076 2011 6238 5295 7   # QR-IBAN → QRR-Referenz, sonst SCOR (RF…)
qr_referenz_praefix: ''
waehrung: CHF
zahlungsfrist_tage: 30
erstes_jahr: 2026             # Eröffnungssalden im Kontenplan gelten für dieses Jahr
sperre_bis: null              # nur über `batzen lock` / `batzen unlock` ändern
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
```

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

## journal/JJJJ/JJJJ-MM.md

```markdown
# Journal Januar 2026

| Datum      | Beleg  | Text         | Soll | Haben |  Betrag | Quelle |
| ---------- | ------ | ------------ | ---- | ----- | ------: | ------ |
| 2026-01-05 | 26-001 | Büromaterial | 6500 | 1020  |   45.80 |        |
| 2026-01-10 | 26-002 | Einkauf Coop | 6500 |       |   30.00 |        |
| 2026-01-10 | 26-002 | Einkauf Coop | 6641 |       |   20.00 |        |
| 2026-01-10 | 26-002 | Einkauf Coop |      | 1000  |   50.00 |        |
```

- Eine Zeile mit Soll **und** Haben ist eine einfache Buchung.
- Sammelbuchung: mehrere Zeilen mit derselben Belegnummer, jede nur mit Soll oder nur mit Haben. Der Beleg als Ganzes muss aufgehen.
- `Quelle` leer = manuelle Buchung. Sonst gehört die Zeile einem Dokument:
  `rechnung:R-2026-0001`, `zahlung:R-2026-0001`, `gutschrift:R-2026-0001`, `lohn:2026-01:M0001`, `abschluss:2026`.
- Text vor und nach der Tabelle bleibt erhalten. Weitere Spalten dürfen ergänzt werden, sie werden mitgeführt.

## vorschlaege.md

Gleiche Tabelle plus `ID` und `Begründung`. `batzen approve V-001` verschiebt die Zeile ins Journal.

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

Frontmatter: `mitarbeiter, name, jahr, monat, status (entwurf|abgeschlossen), eingaben{stunden, bvg, kinderzulagen, korrektur, korrektur_text, qst_satzbestimmend, qst_gesamtpensum}, werte{…}`, nach Abschluss zusätzlich `ag{…}` und `fingerprint`. Der Text darunter wird erzeugt. Eingaben ändert man im Entwurf und rechnet mit `batzen payroll run` neu.

## .batzen/locks.yaml

`bis`, Hash pro gesperrtem Monat, `verlauf` aller Sperren und Entsperrungen (mit Grund).

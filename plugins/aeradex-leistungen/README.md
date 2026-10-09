# aeradex-leistungen

Zeit und Material erfassen, Abwesenheiten, Offerten, Projekte und Abrechnung an Kunden für
[aeradex](../../README.md) — gebaut für das Büro: Tastatur zuerst, eine Zeile statt eines Formulars. Abgerechnet wird
über die normalen aeradex-Rechnungen (QR-Rechnung, Debitor 1100 an Ertrag, MWST). Oberfläche: **Plugins → Leistungen**
(Erfassen, Woche, Abwesenheiten, Auswertung, Stammdaten) und **Offerten**.

**Erfassen** (Startseite) — der einfache Weg: Datum, Kunde, *Was* (vorerfasste Leistungen in h und Produkte aus dem
Katalog, oder Arbeitszeit zum Satz der Person), Menge, optional Preis und Text → «Erfassen». Neue Produkte und
Leistungen direkt dort mit «+ Neues Produkt». Darunter alle Einträge, gefiltert nach Status (nicht abgerechnet,
abgerechnet, alle), Kunde, Zeitraum und Art, gruppiert pro Kunde mit Summe. «Abrechnen → Rechnung» stellt für die
angehakten Einträge eines Kunden die QR-Rechnung mit Leistungsrapport aus und verbucht sie als Debitor; abgerechnete
Einträge zeigen die Rechnungsnummer.

**Woche** — Schnelleingabe in einer Zeile mit Live-Vorschau, Enter speichert:

    3.5h Fassade spachteln          2:30 P0003 Malerarbeiten Decke       8-12 gestern Beratung Huber
    12 l Farbe Huber                1h intern Buchhaltung                start P0003 Spachteln   (▶ Stoppuhr)

Dazu die Stoppuhr (ein Klick, Wechsel stoppt die laufende, Rundung wählbar), «zuletzt verwendet» und Favoriten als
Chips (▶ starten, Klick übernimmt in die Eingabe), das Wochenraster wie eine Tabelle (Tab/Enter/Pfeile, Soll/Ist und
Saldo pro Tag, Feiertage des Kantons) und die Einträge der Woche (Doppelklick bearbeitet, mehrere auswählen und auf ein
anderes Projekt oder eine andere Leistungsart verschieben). Tasten: `/` Eingabe, `S` Stoppuhr stoppen, `←` `→` Woche,
`T` heute, `A` Zeile.

**Leistungsarten** sind Katalogeinträge mit Einheit h («Malerarbeiten 95/h»): sie tragen den Stundensatz, das Konto
und den MWST-Code und gruppieren die Rechnung («Malerarbeiten 12.5 h à 95»). Material hat jede andere Einheit
(Stk, m², l, Pauschal).

**Abwesenheiten** — Jahreskalender pro Person (Ferien, Krank, Unfall, Militär/ZS, Mutter-/Vaterschaft, Weiterbildung,
Kompensation, unbezahlt; halbe Tage), Ferienkonto (Anspruch pro rata Eintritt/Austritt, Vortrag, bezogen, geplant,
Rest), Teamübersicht und Arbeitszeitnachweis nach Art. 73 ArGV 1 als PDF. Die gesetzlichen Feiertage kommen aus dem
Kanton des Firmensitzes (`data/feiertage.json`, erzeugt mit `tools/feiertage.py`), eigene lassen sich ergänzen.

**Abrechnen** — eine Karte pro Kunde/Projekt mit Betrag, Stunden, Material, Alter und Budget-Ampel; «Rechnung
erstellen» zeigt die Vorschau und stellt mit einem Klick aus (mit Leistungsrapport). Zum Monatsende «Alle abrechnen»:
eine Rechnung je Kunde oder je Projekt, in einem Schritt.

```bash
pip install aeradex-leistungen && aeradex plugins ein leistungen

# Stammdaten
aeradex leistungen satz --wer M0001 --satz 120 --kostensatz 68        # Mitarbeitende aus dem Lohn
aeradex leistungen satz --name "Inhaber Ben" --satz 140 --soll-woche 42 # Person ohne Lohn → X01
aeradex leistungen produkt add --text "Dispersionsfarbe weiss" --preis 45 --einheit l
aeradex leistungen produkt set --nummer P001 --kunde K0001 --kundenpreis 40
aeradex leistungen projekt add --kunde K0001 --name "Fassade" --budget-stunden 40 --satz 110
aeradex leistungen feiertage 2026-12-25 2026-12-26 --ab 2026-10-01

# Erfassen — am schnellsten in einer Zeile
aeradex leistungen schnell 3.5h Fassade spachteln --wer M0001
aeradex leistungen schnell 12 l Dispersionsfarbe Huber --vorschau          # nur zeigen, was erkannt wird
aeradex leistungen start P0003 Malerarbeiten Decke --wer M0001             # Stoppuhr
aeradex leistungen stop --wer M0001
aeradex leistungen woche --wer M0001
aeradex leistungen zeit --wer M0001 --stunden 3.5 --kunde K0001 --text "Wände gespachtelt"
aeradex leistungen zeit --wer M0001 --stunden 8.4 --nicht-abrechenbar --kategorie Ferien
aeradex leistungen material --kunde K0001 --produkt P001 --menge 12

# Abwesenheiten
aeradex leistungen abwesenheit --wer M0001 --art Ferien --von 2026-12-21 --bis 2026-12-31
aeradex leistungen ferien --wer M0001 --jahr 2026
aeradex leistungen nachweis --wer M0001 --monat 2026-10                   # Arbeitszeitnachweis PDF

# Abrechnen
aeradex leistungen abrechnen                                 # Übersicht: was ist offen, je Kunde/Projekt
aeradex leistungen abrechnen --alle --bis 2026-10-31         # alles auf einmal, eine Rechnung je Kunde
aeradex leistungen abrechnen --kunde K0001 --vorschau
aeradex leistungen abrechnen --kunde K0001 --bis 2026-10-31  # Rechnung + Leistungsrapport (PDF)

# Offerten
aeradex leistungen offerte create --kunde K0002 --titel "Treppenhaus" \
    --pos "stunden=24:Malerarbeiten:95" --pos "produkt=P001:15" --pos "frei=Abdeckmaterial;1;80;Pauschal"
aeradex leistungen offerte status --nummer O-2026-0001 --status angenommen    # → Projekt P0001
aeradex leistungen offerte rechnung --nummer O-2026-0001 --anteil 40          # Teilrechnung
aeradex leistungen offerte rechnung --nummer O-2026-0001 --schluss            # Rest, Teilrechnungen abgezogen

# Kontrolle
aeradex leistungen kontrolle --monat 2026-10                 # Soll, Ist, Saldo je Person
aeradex leistungen projekt status --nummer P0001             # Budget, Wert, Kosten, verrechnet, Deckungsbeitrag
aeradex leistungen lohn --monat 2026-10                      # Stunden der Stundenlöhner in den Lohnlauf
```

Berichte (aeradex → Berichte, Gruppe «Leistungen», oder `aeradex bericht leistungen_personen|leistungen_produkte|leistungen_projekte`):
Auslastung je Person (Stunden, abrechenbar-Quote, Wert, Kosten, noch offen), Umsatz nach Produkt, Projekte mit
Budget gegen Ist und Deckungsbeitrag.

## Wie es rechnet

- **Stundensatz**, beim Erfassen festgehalten: angegeben › Projekt › Kundenpreis der Leistungsart › Kunde
  (`stundensatz`) › Leistungsart › Person. Der Lohn
  (`stundenlohn` im Personal) ist etwas anderes und bleibt unberührt; der **Kostensatz** dient nur dem
  Deckungsbeitrag.
- **Produktpreis**: Kundenpreis › Produktpreis. Ein neuer Preis gilt für neue Einträge.
- **Nicht abrechenbar** braucht eine Kategorie (Intern, Administration, Weiterbildung, Garantie, Ferien, Krank,
  Feiertag) und zählt in der Stundenkontrolle als Ist.
- **Abgerechnet** heisst: der Eintrag trägt eine Rechnungsnummer, und die Rechnung ist nicht storniert. Wird sie
  storniert, sind die Einträge wieder offen. Abgerechnete Einträge lassen sich nicht ändern; `aeradex check` warnt,
  wenn sie von Hand verändert wurden und nicht mehr zur Rechnung passen.
- **Offerte pauschal**: ganz, in Teilrechnungen (% oder Betrag, anteilig auf Konten und MWST-Codes) und mit
  Schlussrechnung, die die Teilrechnungen abzieht. Zeit auf einem Pauschalprojekt wird kontrolliert, nicht verrechnet.
  **Nach Aufwand**: die Offerte ist das Budget, verrechnet werden die erfassten Leistungen (zum Satz der Offerte).
- **Stundenkontrolle**: Soll je Arbeitstag = Wochenstunden × Pensum ÷ 5 (Mo–Fr, ohne Feiertage, ab Eintritt bzw.
  «Kontrolle ab»). Saldo je Monat und kumuliert im Jahr, Vortrag pro Person und Jahr in den Einstellungen.
  Stundenlöhner haben kein Soll.
- Der Agent darf erfassen, auswerten und Offerten entwerfen; Rechnungen stellt ein Mensch aus.

## Dateien im Buch

```
leistungen/einstellungen.yaml        Sätze, Personen ohne Lohn, Kategorien, Feiertage, Konten
leistungen/produkte.yaml             Produkte mit Preis, Einheit, Konto, MWST, Kundenpreisen
leistungen/erfassung/2026-10.md      Zeit und Produkte, eine Tabelle pro Monat
projekte/P0001-<name>.md             Projekte mit Budget
offerten/2026/O-2026-0001.md + .pdf  Offerten
rechnungen/2026/R-…-rapport.pdf      Leistungsrapport zur Rechnung
```

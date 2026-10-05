# allkvitt-leistungen

Offerten, Zeiterfassung, Produkte, Projekte, Stundenkontrolle und Abrechnung an Kunden für
[allkvitt](../../README.md). Abgerechnet wird über die normalen allkvitt-Rechnungen (QR-Rechnung, Debitor 1100 an
Ertrag, MWST) — das Plugin bucht selbst nichts. Oberfläche: Plugins → **Leistungen** und **Offerten**.

```bash
pip install allkvitt-leistungen && allkvitt plugins ein leistungen

# Stammdaten
allkvitt leistungen satz --wer M0001 --satz 120 --kostensatz 68        # Mitarbeitende aus dem Lohn
allkvitt leistungen satz --name "Inhaber Ben" --satz 140 --soll-woche 42 # Person ohne Lohn → X01
allkvitt leistungen produkt add --text "Dispersionsfarbe weiss" --preis 45 --einheit l
allkvitt leistungen produkt set --nummer P001 --kunde K0001 --kundenpreis 40
allkvitt leistungen projekt add --kunde K0001 --name "Fassade" --budget-stunden 40 --satz 110
allkvitt leistungen feiertage 2026-12-25 2026-12-26 --ab 2026-10-01

# Erfassen
allkvitt leistungen zeit --wer M0001 --stunden 3.5 --kunde K0001 --text "Wände gespachtelt"
allkvitt leistungen zeit --wer M0001 --stunden 8.4 --nicht-abrechenbar --kategorie Ferien
allkvitt leistungen material --kunde K0001 --produkt P001 --menge 12

# Abrechnen
allkvitt leistungen abrechnen                                 # Übersicht: was ist offen, je Kunde/Projekt
allkvitt leistungen abrechnen --kunde K0001 --vorschau
allkvitt leistungen abrechnen --kunde K0001 --bis 2026-10-31  # Rechnung + Leistungsrapport (PDF)

# Offerten
allkvitt leistungen offerte create --kunde K0002 --titel "Treppenhaus" \
    --pos "stunden=24:Malerarbeiten:95" --pos "produkt=P001:15" --pos "frei=Abdeckmaterial;1;80;Pauschal"
allkvitt leistungen offerte status --nummer O-2026-0001 --status angenommen    # → Projekt P0001
allkvitt leistungen offerte rechnung --nummer O-2026-0001 --anteil 40          # Teilrechnung
allkvitt leistungen offerte rechnung --nummer O-2026-0001 --schluss            # Rest, Teilrechnungen abgezogen

# Kontrolle
allkvitt leistungen kontrolle --monat 2026-10                 # Soll, Ist, Saldo je Person
allkvitt leistungen projekt status --nummer P0001             # Budget, Wert, Kosten, verrechnet, Deckungsbeitrag
allkvitt leistungen lohn --monat 2026-10                      # Stunden der Stundenlöhner in den Lohnlauf
```

Berichte (allkvitt → Berichte, Gruppe «Leistungen», oder `allkvitt bericht leistungen_personen|leistungen_produkte|leistungen_projekte`):
Auslastung je Person (Stunden, abrechenbar-Quote, Wert, Kosten, noch offen), Umsatz nach Produkt, Projekte mit
Budget gegen Ist und Deckungsbeitrag.

## Wie es rechnet

- **Stundensatz**, beim Erfassen festgehalten: angegeben › Projekt › Kunde (`stundensatz`) › Person. Der Lohn
  (`stundenlohn` im Personal) ist etwas anderes und bleibt unberührt; der **Kostensatz** dient nur dem
  Deckungsbeitrag.
- **Produktpreis**: Kundenpreis › Produktpreis. Ein neuer Preis gilt für neue Einträge.
- **Nicht abrechenbar** braucht eine Kategorie (Intern, Administration, Weiterbildung, Garantie, Ferien, Krank,
  Feiertag) und zählt in der Stundenkontrolle als Ist.
- **Abgerechnet** heisst: der Eintrag trägt eine Rechnungsnummer, und die Rechnung ist nicht storniert. Wird sie
  storniert, sind die Einträge wieder offen. Abgerechnete Einträge lassen sich nicht ändern; `allkvitt check` warnt,
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

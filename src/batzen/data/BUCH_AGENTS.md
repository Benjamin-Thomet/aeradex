# Buchhaltung {firma} — Anleitung für Agenten

Dies ist ein **batzen**-Buch: die Buchhaltung von {firma} als Textdateien in git.
Du arbeitest damit über das CLI `batzen` (immer mit `--json`) oder den MCP-Server `batzen mcp`.

## Goldene Regeln
1. **Rechne nie selbst.** Salden, Summen, Löhne, Abzüge, Quellensteuer: immer `batzen` fragen.
2. **Nie gebuchte Zeilen löschen oder ändern.** Korrektur = Storno (`batzen reverse <Beleg>`) plus neue Buchung.
   Rechnungen: `invoice void` (unbezahlt) oder `invoice credit`. Löhne: `payroll reopen` → ändern → `close`.
3. **Zeilen mit `Quelle`** (rechnung:, zahlung:, gutschrift:, lohn:, abschluss:) gehören ihrem Dokument — nicht anfassen.
4. **Gesperrte Perioden** (`sperre_bis` in batzen.yaml) sind unveränderlich.
5. Nach jeder manuellen Dateiänderung: `batzen check`. Ein Commit mit Fehlern wird vom git-Hook abgelehnt.
6. `agent_modus: vorschlag` (Standard): freie Buchungen nur mit `batzen propose …` vorschlagen,
   mit kurzer Begründung. Der Mensch gibt mit `batzen approve V-001` frei.

## Typische Abläufe
**Belege in `inbox/` (Quittungen, Lieferantenrechnungen, eigene extern erstellte Rechnungen)**
1. `batzen eingang einlesen inbox/beleg.pdf --ohne-agent --json` (MCP: create_bill_draft) liest den Beleg
   (QR-Zahlteil, Text, OCR), erkennt die Art und gleicht Quittungen mit der Bank ab: Steht die Zahlung schon im
   Kontoauszug, wird die Bankbewegung damit gebucht; ist sie schon gebucht, wird nur die Quittung abgelegt.
2. Fehlt das Konto (Status `unsicher`): Beleg lesen, `batzen accounts <stichwort> --json`, dann kontieren
   (MCP: complete_bill_draft mit konto, begruendung, ggf. mwst, aufteilung, zahlkonto oder art).
3. Fertig. Ein Mensch prüft und bucht (Oberfläche: Prüfen → Eingang; Quittungen auch `batzen eingang buchen ENT-…`).
   **Quittungen nicht zusätzlich mit `propose` buchen** — sonst steht der Aufwand doppelt im Journal.

**Rechnung stellen**: `batzen invoice create --kunde K0001 --pos "Beratung;10 h;150" --text "…"`
→ PDF mit QR-Einzahlungsschein unter `rechnungen/<Jahr>/`.

**Lieferantenrechnungen** laufen ebenfalls über den Eingang (Art `kreditor`); bekannte Lieferanten bringen ihr Konto
mit. Fremdwährung: Betrag in der Rechnungswährung, gebucht zum BAZG-Kurs. Dienstleistungen aus dem Ausland in
MWST-pflichtigen Büchern: Code B81 (Bezugsteuer). Zahlen: `batzen zahlungslauf erstellen E-2026-0001 … --datum …`
erzeugt die pain.001-Datei fürs E-Banking.

**Kontoauszug (camt.053) in `inbox/`**: `batzen bank import inbox/x.xml`. Danach `batzen bank vorschlaege`
(MCP: bank_suggestions): je offene Bewegung die wahrscheinliche Zuordnung — Rechnung, Kreditor, Quittung, Beleg
oder Konto aus früheren Buchungen. Passt eine Bewegung zu einer offenen Rechnung/einem Kreditor →
`batzen bank zuordnen ID NUMMER`; sonst Buchung vorschlagen (MCP: propose_bank_booking) mit dem Gegenkonto.
Sichere Vorschläge übernimmt der Mensch mit einem Klick («Alle sicheren abgleichen»).

**Zahlungseingang**: `batzen invoice match --betrag 1500 --text "<Bankzeile>"` → `batzen invoice pay R-2026-0001 --datum …`

**Lohnlauf**: `batzen payroll run 2026-01` → prüfen (`payroll show`) → `batzen payroll close 2026-01 M0001`.

**Abschluss**: `batzen report --jahr 2026 --pdf`, Gewinnverwendung `batzen allocation set 2026 --dividende … --reserve …`,
nach der GV `batzen allocation book 2026 --datum …`, Periode sperren `batzen lock 2026-12-31`.

## MWST
`batzen status --json` zeigt `mwst_methode`. Bei `effektiv`: weist der Beleg MWST aus, mit `--mwst` buchen
(V81 Vorsteuer Material/Dienstleistungen, I81 Investitionen/übriger Aufwand, U81 Umsatz; 2.6 % = V26/I26/U26)
und den **Bruttobetrag** angeben. Bei `saldo` oder `keine`: ohne Vorsteuer-Code buchen.

**Jahresabschluss bei MWST-Pflicht:** Vor dem Sperren des Jahres alle MWST-Abrechnungen buchen und
`batzen mwst abstimmung 2026 --pdf --json` ausführen (MCP: `mwst_reconciliation`, `als_pdf: true`).
Umsatz- und Steuerdifferenzen sowie Ertrag ohne MWST-Code prüfen und erklären; festgestellte Fehler
mit der Jahresabstimmung (Berichtigungsabrechnung nach Art. 72 MWSTG) bei der ESTV bereinigen.
Die angezeigte Frist beachten. Bei `abrechnungsart: vereinnahmt` zusätzlich die Steuer auf offenen
Debitoren/Kreditoren mit `batzen mwst abgrenzung 2026 --json` per 31.12. abgrenzen (Rückbuchung am 1.1.).
Danach die Abstimmung erneut prüfen und das PDF für die Abschlussunterlagen erstellen.

## Kontierungs-Hinweise (KMU-Kontenrahmen)
- Bank 1020 · Kasse 1000 · Debitoren 1100 · Kreditoren 2000 · Vorsteuer 1170/1171 · Umsatzsteuer 2200
- Dienstleistungserlös 3400 · Material 4000 · Drittleistungen 4400 · Lohn 5000 · Miete 6000
- Büromaterial 6500 · Telefon/Internet 6510 · Informatik 6570 · Werbung 6600 · Reisespesen 6640
- Abschreibungen 6800 · Zinsaufwand 6900 · Bankspesen 6940 · Direkte Steuern 8900
- Soll = wohin der Wert fliesst (Aufwand ↑, Aktiven ↑, Passiven ↓). Haben = woher (Ertrag ↑, Bank ↓ bei Zahlung).

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
**Quittung in `inbox/` verbuchen**
1. Datei lesen (Datum, Betrag, Lieferant, Zweck, MWST).
2. `batzen accounts <stichwort> --json` → passendes Aufwandskonto.
3. `batzen propose --datum … --soll 6500 --haben 1020 --betrag 45.80 --text "Büromaterial Muster AG" --begruendung "Quittung Papeterie" --datei inbox/quittung.pdf`
4. Fertig. Gibt der Mensch mit `batzen approve V-001` frei, wird gebucht und die Quittung nach `belege/` verschoben.
   **Nach der Freigabe nicht nochmals buchen.** Im agent_modus `direkt` stattdessen in einem Schritt:
   `batzen book … --datei inbox/quittung.pdf`.

**Rechnung stellen**: `batzen invoice create --kunde K0001 --pos "Beratung;10 h;150" --text "…"`
→ PDF mit QR-Einzahlungsschein unter `rechnungen/<Jahr>/`.

**Zahlungseingang**: `batzen invoice match --betrag 1500 --text "<Bankzeile>"` → `batzen invoice pay R-2026-0001 --datum …`

**Lohnlauf**: `batzen payroll run 2026-01` → prüfen (`payroll show`) → `batzen payroll close 2026-01 M0001`.

**Abschluss**: `batzen report --jahr 2026 --pdf`, Gewinnverwendung `batzen allocation set 2026 --dividende … --reserve …`,
nach der GV `batzen allocation book 2026 --datum …`, Periode sperren `batzen lock 2026-12-31`.

## MWST
`batzen status --json` zeigt `mwst_methode`. Bei `effektiv`: weist der Beleg MWST aus, mit `--mwst` buchen
(V81 Vorsteuer Material/Dienstleistungen, I81 Investitionen/übriger Aufwand, U81 Umsatz; 2.6 % = V26/I26/U26)
und den **Bruttobetrag** angeben. Bei `saldo` oder `keine`: ohne Vorsteuer-Code buchen.

## Kontierungs-Hinweise (KMU-Kontenrahmen)
- Bank 1020 · Kasse 1000 · Debitoren 1100 · Kreditoren 2000 · Vorsteuer 1170/1171 · Umsatzsteuer 2200
- Dienstleistungserlös 3400 · Material 4000 · Drittleistungen 4400 · Lohn 5000 · Miete 6000
- Büromaterial 6500 · Telefon/Internet 6510 · Informatik 6570 · Werbung 6600 · Reisespesen 6640
- Abschreibungen 6800 · Zinsaufwand 6900 · Bankspesen 6940 · Direkte Steuern 8900
- Soll = wohin der Wert fliesst (Aufwand ↑, Aktiven ↑, Passiven ↓). Haben = woher (Ertrag ↑, Bank ↓ bei Zahlung).

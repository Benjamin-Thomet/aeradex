# Parallelbetrieb: batzen neben der bisherigen Buchhaltung

Bevor batzen die bisherige Buchhaltung ersetzt, läuft es ein Quartal daneben. Ziel: dieselben Zahlen auf den
Rappen — oder für jede Abweichung eine erklärte Ursache. Gearbeitet wird in einem eigenen Buch (Kopie, eigenes
git-Repository); die bisherige Buchhaltung bleibt massgebend.

## Vorbereitung

- [ ] Buch anlegen: `batzen init ~/buecher/firma-parallel --firma … --jahr 2027` (Kontenplan wie bisher anpassen:
      Kontonummern, Namen, Gruppen; Systemkonten in batzen.yaml prüfen).
- [ ] Eröffnungsbilanz aus dem letzten Abschluss übernehmen (`kontenplan.yaml → eroeffnung`, Vorjahr unter `vorjahr`);
      `batzen check` muss ohne Fehler sein, die Bilanz muss aufgehen.
- [ ] Offene Posten übernehmen: offene Kundenrechnungen als externe Rechnungen erfassen (Debitoren → Externe
      Rechnung), offene Lieferantenrechnungen als Kreditoren — beide mit dem Originaldatum.
- [ ] Mitarbeitende und Lohneinstellungen (`lohn/einstellungen.yaml`) nach den Policen und der Ausgleichskasse.
- [ ] MWST-Methode, Periode und Saldosteuersatz wie bewilligt; UID; Bankkonten (`bankkonten`, IBAN → Konto).

## Laufend (jeden Monat)

- [ ] Kontoauszüge (camt.053) importieren; Bankregeln für Wiederkehrendes anlegen.
- [ ] Belege über den Eingang erfassen, wie sie auch in der bisherigen Buchhaltung gebucht werden.
- [ ] Rechnungen in der bisherigen Software stellen und in batzen als externe Rechnung erfassen (oder umgekehrt).
- [ ] Lohnlauf rechnen und mit der bisherigen Abrechnung vergleichen: Brutto, jeder Abzug, Netto, AG-Beiträge.
- [ ] `batzen check` ohne Fehler; Hinweise ansehen.

## Vergleich am Monats- bzw. Quartalsende

| Was | batzen | Soll |
|---|---|---|
| Saldenliste je Konto | `batzen balance --periode q1` | gleich wie bisher |
| Bank | `batzen bank abstimmung` | Differenz 0.00 |
| Offene Posten Debitoren / Kreditoren | Debitoren → Offene Posten, `batzen kreditor offen` | gleiche Liste, gleiche Beträge |
| Lohn je Mitarbeiter/in und Monat | Lohn → Lohnkonto | gleich auf den Rappen |
| MWST-Abrechnung | `batzen mwst abrechnung 2027-Q1` | gleiche Ziffern, gleiche Zahllast |
| Erfolgsrechnung, Bilanz | `batzen report --jahr 2027` | gleich |

Jede Abweichung festhalten: Ursache (Erfassungsfehler, andere Kontierung, Rundung, Fehler in batzen), Lösung.
Fehler in batzen als Issue mit einem minimalen Beispiel melden — ohne echte Daten.

## Entscheid

Nach dem Quartal: Stimmen alle Zahlen oder sind alle Abweichungen erklärt, kann batzen übernehmen. Sonst: einen
weiteren Monat parallel. Den Jahresabschluss des ersten vollen Jahres von der Treuhand prüfen lassen.

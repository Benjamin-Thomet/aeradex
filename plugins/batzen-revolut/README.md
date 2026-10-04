# batzen-revolut

Revolut-Kontoauszüge (CSV) für den Bankimport von [batzen](../../README.md) — Business- und Privatkonto.

```bash
pip install batzen-revolut
batzen plugins ein revolut
batzen account-add 1022 "Revolut CHF"
batzen revolut-konto CHF 1022
batzen bank import ~/Downloads/revolut-statement.csv
```

- Nur abgeschlossene Transaktionen; ausstehende, abgelehnte und rückgängig gemachte werden übersprungen.
- Gebühren werden als eigene Bewegung importiert (zum Buchen auf Bankspesen 6940).
- Der Saldo der letzten Zeile dient als Schlusssaldo: `batzen bank abstimmung` vergleicht ihn mit der Buchhaltung.
- Dieselbe Datei zweimal importieren schadet nicht (Transaktions-ID von Revolut).
- Je Währung ein Konto. Fremdwährungskonten im Bankimport folgen mit batzen; bis dahin nur CHF-Konten zuordnen.

**Stand:** gebaut nach den Spalten der Revolut-Exporte (Business: «Date completed (UTC)», «Payment currency»,
«Amount», «Fee», «Balance»; Privat: «Completed Date», «Currency», «Amount», «Fee», «Balance»). Mit einem echten
Export prüfen und Abweichungen als Issue melden — am besten mit einer anonymisierten Beispielzeile.

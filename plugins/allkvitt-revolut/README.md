# allkvitt-revolut

Revolut-Kontoauszüge (CSV) für den Bankimport von [allkvitt](../../README.md) — Business- und Privatkonto.

```bash
pip install allkvitt-revolut
allkvitt plugins ein revolut
allkvitt account-add 1022 "Revolut CHF"
allkvitt revolut-konto CHF 1022
allkvitt bank import ~/Downloads/revolut-statement.csv
```

- Nur abgeschlossene Transaktionen; ausstehende, abgelehnte und rückgängig gemachte werden übersprungen.
- Gebühren werden als eigene Bewegung importiert (zum Buchen auf Bankspesen 6940).
- Der Saldo der letzten Zeile dient als Schlusssaldo: `allkvitt bank abstimmung` vergleicht ihn mit der Buchhaltung.
- Dieselbe Datei zweimal importieren schadet nicht (Transaktions-ID von Revolut).
- Je Währung ein Konto, in dessen Währung (z.B. `allkvitt account-add 1026 "Revolut EUR" --waehrung EUR`, dann `allkvitt revolut-konto EUR 1026`).

**Stand:** gebaut nach den Spalten der Revolut-Exporte (Business: «Date completed (UTC)», «Payment currency»,
«Amount», «Fee», «Balance»; Privat: «Completed Date», «Currency», «Amount», «Fee», «Balance»). Mit einem echten
Export prüfen und Abweichungen als Issue melden — am besten mit einer anonymisierten Beispielzeile.

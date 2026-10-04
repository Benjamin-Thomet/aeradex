# batzen-kontenplan-verein

Kontenplan für Schweizer Vereine als Datenplugin für [batzen](../../README.md).

```bash
pip install batzen-kontenplan-verein
batzen init ~/buecher/turnverein --firma "Turnverein Muster" --rechtsform Verein --kontenplan verein
```

- Angelehnt an den KMU-Kontenrahmen, damit Lohn, MWST, Kreditoren und Fremdwährungen ohne Anpassung laufen.
- Ertrag aus Mitgliederbeiträgen (3000), Spenden (3100), Sponsoring (3200), Beiträgen der öffentlichen Hand (3300).
- Eigenkapital als Vereinsvermögen (2800), zweckgebundene Fonds separat (2850).

Ein Datenplugin enthält keinen Code, der im Buch etwas tut — es bietet nur die Vorlage für `batzen init` an.

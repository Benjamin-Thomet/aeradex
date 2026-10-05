# Mitmachen bei allkvitt

Willkommen! allkvitt ist Open Source (AGPL-3.0-or-later) und soll von der Community getragen werden.
*English contributions are welcome too — code and comments are in English, user-facing text is Swiss German.*

## Wege, beizutragen

- **Ein Plugin bauen** — der schnellste Weg, ohne auf ein Release zu warten: [docs/plugins.md](docs/plugins.md),
  Ideen in [PLUGINS.md](PLUGINS.md).
- **Fehler melden** — am besten mit einem minimalen Buch (`allkvitt init` in einem Temp-Ordner) und den Befehlen,
  die zum Fehler führen. Keine echten Buchhaltungsdaten in Issues hochladen.
- **Fachwissen** — Sätze, Ziffern, Formulare und Wegleitungen ändern sich. Hinweise mit Quelle (ESTV, BSV,
  Kanton) sind so wertvoll wie Code.
- **Code** im Kern: neue Funktionen zuerst in `api.py`, dann CLI, MCP-Werkzeug und Oberfläche
  (siehe [AGENTS.md](AGENTS.md)).

## Regeln für Code

- Geld ist `Decimal`, nie `float`. Rappenrundung kaufmännisch (`files.CENT`).
- Jede Schreiboperation läuft durch `api._guard`/`_done` (bzw. `api.write` in Plugins): prüfen, schreiben, prüfen, committen.
- Lohn-, MWST- und Steuerlogik nur mit einem Test ändern, der die Quelle zitiert (KS 45, AHV-Merkblatt, MWST-Info …).
- Tests: `pip install -e ".[ui,mcp,dev]" && pytest`. Neue Funktionen bringen Tests mit.
- Texte für Benutzer: Deutsch, Schweizer Schreibweise (kein ß).

## Pull Requests und Sign-off

Jeder Commit trägt ein `Signed-off-by` nach dem [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "Revolut: Gebühren als eigene Bewegung"
```

Damit bestätigst du, dass du den Beitrag unter der Lizenz von allkvitt einbringen darfst. Es gibt keinen
CLA: das Urheberrecht bleibt bei dir, allkvitt bleibt dadurch für alle frei.

## Verhalten

Es gilt der [Verhaltenskodex](CODE_OF_CONDUCT.md).

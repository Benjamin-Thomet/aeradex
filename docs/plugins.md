# Plugins für batzen

batzen soll von der Community wachsen: eine Bank, ein Kanton, eine Branche, ein Export — ohne dass jemand
batzen forken oder auf ein Release warten muss. Ein Plugin ist ein gewöhnliches Python-Paket.

```bash
pip install batzen-revolut          # installieren (wie jedes Python-Paket)
batzen plugins                      # was ist installiert, was ist eingeschaltet?
batzen plugins ein revolut          # für dieses Buch einschalten (wird in batzen.yaml festgehalten)
batzen plugins aus revolut
```

In der Oberfläche: **Einstellungen → Plugins** (im Serverbetrieb nur für Admins).

## Die Regeln

1. **Installiert heisst nicht eingeschaltet.** Was ein Plugin *tut* (Bankformate, Dokumente mit Buchungen,
   Prüfregeln, Agenten-Werkzeuge, Befehle), wirkt nur in Büchern, die es unter `plugins:` in `batzen.yaml` führen.
   *Daten* (Kontenplan-Vorlagen, Quellensteuer-Tabellen) bietet jedes installierte Plugin an — sie werden gebraucht,
   bevor es ein Buch gibt.
2. **Plugins schreiben über batzen.** Änderungen laufen durch `api.write`: Buch vorher prüfen → schreiben →
   `batzen check` → git-Commit. Macht ein Plugin das Buch ungültig, wird die Änderung abgelehnt.
3. **Wem eine Buchung gehört, der muss sie erklären.** Ein Plugin, das Journalzeilen erzeugt, registriert eine
   Quelle (`Quelle anlage:A001:2026`) und eine Funktion, die diese Zeilen aus seinen Dokumenten neu erzeugt.
   `batzen check` vergleicht — wie bei Rechnungen und Lohn.
4. **Fehlt ein Plugin, ist das Buch nicht gültig.** Schaltet ein Buch ein Plugin ein, das auf diesem Rechner nicht
   installiert ist, meldet `check` einen Fehler und batzen schreibt nichts mehr, bis es installiert ist. Kein Plugin
   kann so seine Buchungen «verlieren». Ausschalten geht nur, solange ihm keine Journalzeilen mehr gehören.
5. **Bücher enthalten keinen Code.** Ein geklontes Buch führt nichts aus; es nennt nur die Plugins, die es braucht.
6. **Plugins sind Programmcode** mit denselben Rechten wie batzen. Nur aus vertrauenswürdigen Quellen installieren.
   Wo es geht, lieber ein Datenplugin bauen (siehe unten): das braucht kein Vertrauen.

## Ein Plugin bauen

```bash
cp -r plugins/vorlage ../batzen-meinplugin && cd ../batzen-meinplugin
grep -rl beispiel . | xargs sed -i 's/beispiel/meinplugin/g' && mv src/batzen_beispiel src/batzen_meinplugin
pip install -e ".[dev]" && pytest
```

Das Paket meldet sich über einen Entry Point an und nennt die Version der Plugin-API:

```toml
[project.entry-points."batzen.plugins"]
meinplugin = "batzen_meinplugin"
```

```python
"""Erste Zeile: erscheint in `batzen plugins`."""
from batzen.plugins import hookimpl

BATZEN_PLUGIN_API = 1

@hookimpl
def batzen_check(book, rows):
    ...
```

Die Hooks sind [pluggy](https://pluggy.readthedocs.io/)-Hooks (dasselbe System wie bei pytest). Jeder ist optional.

| Hook | gibt zurück | wofür | wirkt |
|---|---|---|---|
| `batzen_kontenplaene()` | `{"name": Path}` | Kontenplan-Vorlagen für `batzen init --kontenplan name` | immer (Daten) |
| `batzen_qst_tarife()` | `Path` | Ordner mit Quellensteuer-Tabellen `<KANTON>-<JAHR>.json` | immer (Daten) |
| `batzen_bank_formats()` | `[BankFormat]` | Kontoauszüge für `batzen bank import` | eingeschaltet |
| `batzen_beleg_leser()` | `[BelegLeser]` | Lieferantenrechnungen auslesen (z.B. E-Rechnungen ZUGFeRD/XRechnung) | eingeschaltet |
| `batzen_sources()` | `[Source]` | Dokumente, denen Journalzeilen gehören | eingeschaltet |
| `batzen_check(book, rows)` | `[Finding]` | zusätzliche Prüfregeln | eingeschaltet |
| `batzen_tools()` | `[Funktion]` | Werkzeuge für Agenten (MCP und Chat in der Oberfläche) | eingeschaltet |
| `batzen_instructions()` | `str` | kurzer Hinweis an den Agenten, wann er die Werkzeuge nutzt | eingeschaltet |
| `batzen_commands()` | `[Command]` | Befehle `batzen <name> …` | eingeschaltet |
| `batzen_pages()` | `[Page]` | eigene Seiten in der Oberfläche (Seitenleiste «Plugins») | eingeschaltet |

### Kontenplan-Vorlagen (Daten)

Eine YAML-Datei wie `src/batzen/data/kontenplaene/kmu.yaml`. Optional `systemkonten:`, wenn die Vorlage andere
Nummern für die Systemkonten nutzt (`ertrag`, `debitoren`, `gewinnvortrag`, `kursgewinn` …):

```yaml
systemkonten: {ertrag: "3000"}
konten:
- {nr: "1020", name: Bank, klasse: aktiv}
```

Beispiel: [`plugins/batzen-kontenplan-verein`](../plugins/batzen-kontenplan-verein).

### Bankformate

```python
from batzen.plugins import BankFormat

@hookimpl
def batzen_bank_formats():
    return [BankFormat(name="meinebank", label="Meine Bank (CSV)", suffixes=(".csv",),
                       detect=lambda dateiname, daten: ..., parse=lambda daten, book: [...])]
```

`detect` erkennt die Datei (Name und Inhalt), `parse` liefert Kontoauszüge in derselben Form wie der eingebaute
camt.053-Import (`batzen.bank.parse`): je Auszug `id`, `iban` *oder* `konto`, `schluss`/`schluss_datum` für die
Saldoabstimmung und `buchungen` mit `datum`, `betrag` (Decimal, positiv = Eingang), `gegenpartei`, `referenz`,
`text` und einer stabilen `bankref` (damit derselbe Auszug zweimal importiert keine Doppel erzeugt). Alles Weitere —
Zuordnung zu Rechnungen und Kreditoren, offene Posten, Abstimmung — macht batzen. Der camt-Import selbst ist auf
diese Weise eingebunden (`src/batzen/builtin.py`). Beispiel: [`plugins/batzen-revolut`](../plugins/batzen-revolut).

### Belegleser (Kreditoren)

```python
from batzen.plugins import BelegLeser

def lesen(book, pfad, text_bisher):
    ...                                   # {"betrag": "123.45", "rechnungsnr": "…", "iban": "…", "_text": "…"} oder None
    return felder

@hookimpl
def batzen_beleg_leser():
    return [BelegLeser("zugferd", "ZUGFeRD", 20, lesen)]
```

Leser laufen nach `prioritaet` (eingebaut: QR 10, PDF-Text 50, OCR 80); ein Feld behält den Wert des ersten
Lesers, der es gefunden hat, und zeigt dessen `label` als Quelle. Felder: siehe `batzen.erfassung.FIELDS`.

### Dokumente mit Buchungen

```python
from batzen.plugins import Source

def zeilen(book):          # {Quelle: [Row, …]} — aus den Dokumenten des Plugins, deterministisch
    ...

@hookimpl
def batzen_sources():
    return [Source(prefix="anlage", label="Anlage", rows=zeilen, link=lambda ref: None)]
```

Geschrieben wird mit `api.write`:

```python
from batzen import api, journal

def erfassen(book, ...):
    ...                                    # Dokument schreiben
    pfade = journal.post(book, zeilen(book)[quelle])
    return ergebnis, pfade + [dokument]

api.write(book, "Anlage A001 erfasst", erfassen, ...)
```

Ein vollständiges Beispiel mit Dokumenten, Prüfregel, Bankformat, Werkzeug und Befehl steht in
[`tests/plugin_sample.py`](../tests/plugin_sample.py).

### Prüfregeln

```python
from batzen.plugins import Finding

@hookimpl
def batzen_check(book, rows):
    return [Finding("hinweis", r.where, "…") for r in rows if ...]
```

`fehler` blockiert jedes Schreiben im Buch — nur für echte Widersprüche verwenden. `warnung` und `hinweis` informieren.

### Agenten-Werkzeuge

Gewöhnliche Funktionen mit Typen und Docstring (Args-Abschnitt) — das ist alles, was der Agent sieht. Mit
`batzen.tools.call` wird das Buch geholt und ein Fehler als `{"ok": False, "fehler": …}` zurückgegeben. Freie
Buchungen respektieren den `agent_modus` des Buchs: dafür `batzen.tools.direct` statt `call` verwenden.

### Befehle

```python
from batzen.plugins import Command

@hookimpl
def batzen_commands():
    return [Command("meinplugin-x", "Was der Befehl tut", run=lambda book, args: ..., setup=lambda parser: ...)]
```

Ein Name, den batzen schon verwendet, wird übersprungen; Plugin-Befehle tragen darum am besten das Plugin im Namen.

### Seiten in der Oberfläche

```python
from batzen.plugins import Page

@hookimpl
def batzen_pages():
    return [Page("anlagen", "Anlagen", "anlagen.html", context=lambda book, query: {...},
                 actions={"erfassen": lambda book, form: api.write(book, "…", erfassen, …)})]
```

Die Seite liegt unter `/p/<plugin>/<slug>`, die Vorlage im Ordner `templates/` des Plugin-Pakets (in
`package-data` aufnehmen). Sie darf `{% extends "base.html" %}` und die Makros aus `_macros.html` verwenden.
Formulare posten an `/p/<plugin>/<slug>/<aktion>`; die Aktion bekommt die Formularfelder als dict und schreibt über
`api.write`. Im Serverbetrieb dürfen nur Benutzer mit Schreibrecht posten. Beispiel:
[`plugins/batzen-anlagen`](../plugins/batzen-anlagen).

## Testen

```python
from batzen.testing import make_book, assert_clean

def test_mein_plugin(tmp_path):
    book = make_book(tmp_path, plugins=["meinplugin"], eroeffnung={"1020": 1000, "2800": -1000})
    ...
    assert_clean(book)        # keine Fehler und Warnungen in batzen check
```

## Versionen

`BATZEN_PLUGIN_API` ist die Version der Hooks. Ändert sich ein Hook unverträglich, erhöht batzen die Version;
Plugins mit einer anderen Version werden nicht geladen und in `batzen plugins` mit dem Grund angezeigt — nie halb.
Neue, optionale Hooks erhöhen die Version nicht.

## Veröffentlichen

- Paketname `batzen-<name>`, Entry-Point-Name `<name>` (das steht dann in `batzen.yaml`).
- Lizenz: AGPL-3.0-or-later empfohlen (wie batzen); jede damit verträgliche Open-Source-Lizenz geht.
- Auf PyPI veröffentlichen und in [PLUGINS.md](../PLUGINS.md) per Pull Request eintragen.

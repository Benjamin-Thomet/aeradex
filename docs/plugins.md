# Plugins für allkvitt

allkvitt soll von der Community wachsen: eine Bank, ein Kanton, eine Branche, ein Export — ohne dass jemand
allkvitt forken oder auf ein Release warten muss. Ein Plugin ist ein gewöhnliches Python-Paket.

```bash
pip install allkvitt-revolut          # installieren (wie jedes Python-Paket)
allkvitt plugins                      # was ist installiert, was ist eingeschaltet?
allkvitt plugins ein revolut          # für dieses Buch einschalten (wird in allkvitt.yaml festgehalten)
allkvitt plugins aus revolut
```

In der Oberfläche: **Einstellungen → Plugins** (im Serverbetrieb nur für Admins).

## Die Regeln

1. **Installiert heisst nicht eingeschaltet.** Was ein Plugin *tut* (Bankformate, Dokumente mit Buchungen,
   Prüfregeln, Agenten-Werkzeuge, Befehle), wirkt nur in Büchern, die es unter `plugins:` in `allkvitt.yaml` führen.
   *Daten* (Kontenplan-Vorlagen, Quellensteuer-Tabellen) bietet jedes installierte Plugin an — sie werden gebraucht,
   bevor es ein Buch gibt.
2. **Plugins schreiben über allkvitt.** Änderungen laufen durch `api.write`: Buch vorher prüfen → schreiben →
   `allkvitt check` → git-Commit. Macht ein Plugin das Buch ungültig, wird die Änderung abgelehnt.
3. **Wem eine Buchung gehört, der muss sie erklären.** Ein Plugin, das Journalzeilen erzeugt, registriert eine
   Quelle (`Quelle anlage:A001:2026`) und eine Funktion, die diese Zeilen aus seinen Dokumenten neu erzeugt.
   `allkvitt check` vergleicht — wie bei Rechnungen und Lohn.
4. **Fehlt ein Plugin, ist das Buch nicht gültig.** Schaltet ein Buch ein Plugin ein, das auf diesem Rechner nicht
   installiert ist, meldet `check` einen Fehler und allkvitt schreibt nichts mehr, bis es installiert ist. Kein Plugin
   kann so seine Buchungen «verlieren». Ausschalten geht nur, solange ihm keine Journalzeilen mehr gehören.
5. **Bücher enthalten keinen Code.** Ein geklontes Buch führt nichts aus; es nennt nur die Plugins, die es braucht.
6. **Plugins sind Programmcode** mit denselben Rechten wie allkvitt. Nur aus vertrauenswürdigen Quellen installieren.
   Wo es geht, lieber ein Datenplugin bauen (siehe unten): das braucht kein Vertrauen.

## Ein Plugin bauen

```bash
cp -r plugins/vorlage ../allkvitt-meinplugin && cd ../allkvitt-meinplugin
grep -rl beispiel . | xargs sed -i 's/beispiel/meinplugin/g' && mv src/allkvitt_beispiel src/allkvitt_meinplugin
pip install -e ".[dev]" && pytest
```

Das Paket meldet sich über einen Entry Point an und nennt die Version der Plugin-API:

```toml
[project.entry-points."allkvitt.plugins"]
meinplugin = "allkvitt_meinplugin"
```

```python
"""Erste Zeile: erscheint in `allkvitt plugins`."""
from allkvitt.plugins import hookimpl

ALLKVITT_PLUGIN_API = 1

@hookimpl
def allkvitt_check(book, rows):
    ...
```

Die Hooks sind [pluggy](https://pluggy.readthedocs.io/)-Hooks (dasselbe System wie bei pytest). Jeder ist optional.

| Hook | gibt zurück | wofür | wirkt |
|---|---|---|---|
| `allkvitt_kontenplaene()` | `{"name": Path}` | Kontenplan-Vorlagen für `allkvitt init --kontenplan name` | immer (Daten) |
| `allkvitt_qst_tarife()` | `Path` | Ordner mit Quellensteuer-Tabellen `<KANTON>-<JAHR>.json` | immer (Daten) |
| `allkvitt_bank_formats()` | `[BankFormat]` | Kontoauszüge für `allkvitt bank import` | eingeschaltet |
| `allkvitt_beleg_leser()` | `[BelegLeser]` | Lieferantenrechnungen auslesen (z.B. E-Rechnungen ZUGFeRD/XRechnung) | eingeschaltet |
| `allkvitt_sources()` | `[Source]` | Dokumente, denen Journalzeilen gehören | eingeschaltet |
| `allkvitt_check(book, rows)` | `[Finding]` | zusätzliche Prüfregeln | eingeschaltet |
| `allkvitt_tools()` | `[Funktion]` | Werkzeuge für Agenten (MCP und Chat in der Oberfläche) | eingeschaltet |
| `allkvitt_instructions()` | `str` | kurzer Hinweis an den Agenten, wann er die Werkzeuge nutzt | eingeschaltet |
| `allkvitt_commands()` | `[Command]` | Befehle `allkvitt <name> …` | eingeschaltet |
| `allkvitt_pages()` | `[Page]` | eigene Seiten in der Oberfläche (Reiter eines Bereichs oder unter «Mehr») | eingeschaltet |
| `allkvitt_reports()` | `[Report]` | weitere Berichte (Berichte, `allkvitt bericht`, Agent): `Report(name, label, build, params, gruppe)`, `build(book, params)` liefert die Tabelle wie `allkvitt.reports` | eingeschaltet |
| `allkvitt_dossier_teile()` | `[DossierTeil]` | weitere Teile der Abschlussunterlagen (`allkvitt dossier`), z.B. ein Anlagenspiegel: `build(book, jahr, format)` → `[(Dateiname, bytes)]` | eingeschaltet |

### Kontenplan-Vorlagen (Daten)

Eine YAML-Datei wie `src/allkvitt/data/kontenplaene/kmu.yaml`. Optional `systemkonten:`, wenn die Vorlage andere
Nummern für die Systemkonten nutzt (`ertrag`, `debitoren`, `gewinnvortrag`, `kursgewinn` …):

```yaml
systemkonten: {ertrag: "3000"}
konten:
- {nr: "1020", name: Bank, klasse: aktiv}
```

Der Hook gibt `{name: pfad}` zurück; `allkvitt init --kontenplan name` nutzt die Vorlage. Mitgeliefert sind
`kmu` (AG, GmbH), `einzelfirma` und `verein`; eine Vorlage aus einem Plugin mit gleichem Namen ersetzt sie nicht.

### Bankformate

```python
from allkvitt.plugins import BankFormat

@hookimpl
def allkvitt_bank_formats():
    return [BankFormat(name="meinebank", label="Meine Bank (CSV)", suffixes=(".csv",),
                       detect=lambda dateiname, daten: ..., parse=lambda daten, book: [...])]
```

`detect` erkennt die Datei (Name und Inhalt), `parse` liefert Kontoauszüge in derselben Form wie der eingebaute
camt.053-Import (`allkvitt.bank.parse`): je Auszug `id`, `iban` *oder* `konto`, `schluss`/`schluss_datum` für die
Saldoabstimmung und `buchungen` mit `datum`, `betrag` (Decimal, positiv = Eingang), `gegenpartei`, `referenz`,
`text` und einer stabilen `bankref` (damit derselbe Auszug zweimal importiert keine Doppel erzeugt). Alles Weitere —
Zuordnung zu Rechnungen und Kreditoren, offene Posten, Abstimmung — macht allkvitt. Der camt-Import selbst ist auf
diese Weise eingebunden (`src/allkvitt/builtin.py`). Beispiel: [`plugins/allkvitt-revolut`](../plugins/allkvitt-revolut).

### Belegleser (Kreditoren)

```python
from allkvitt.plugins import BelegLeser

def lesen(book, pfad, text_bisher):
    ...                                   # {"betrag": "123.45", "rechnungsnr": "…", "iban": "…", "_text": "…"} oder None
    return felder

@hookimpl
def allkvitt_beleg_leser():
    return [BelegLeser("zugferd", "ZUGFeRD", 20, lesen)]
```

Leser laufen nach `prioritaet` (eingebaut: QR 10, PDF-Text 50, OCR 80); ein Feld behält den Wert des ersten
Lesers, der es gefunden hat, und zeigt dessen `label` als Quelle. Felder: siehe `allkvitt.erfassung.FIELDS`.

### Dokumente mit Buchungen

```python
from allkvitt.plugins import Source

def zeilen(book):          # {Quelle: [Row, …]} — aus den Dokumenten des Plugins, deterministisch
    ...

@hookimpl
def allkvitt_sources():
    return [Source(prefix="anlage", label="Anlage", rows=zeilen, link=lambda ref: None)]
```

Geschrieben wird mit `api.write`:

```python
from allkvitt import api, journal

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
from allkvitt.plugins import Finding

@hookimpl
def allkvitt_check(book, rows):
    return [Finding("hinweis", r.where, "…") for r in rows if ...]
```

`fehler` blockiert jedes Schreiben im Buch — nur für echte Widersprüche verwenden. `warnung` und `hinweis` informieren.

### Agenten-Werkzeuge

Gewöhnliche Funktionen mit Typen und Docstring (Args-Abschnitt) — das ist alles, was der Agent sieht. Mit
`allkvitt.tools.call` wird das Buch geholt und ein Fehler als `{"ok": False, "fehler": …}` zurückgegeben. Freie
Buchungen respektieren den `agent_modus` des Buchs: dafür `allkvitt.tools.direct` statt `call` verwenden.

### Befehle

```python
from allkvitt.plugins import Command

@hookimpl
def allkvitt_commands():
    return [Command("meinplugin-x", "Was der Befehl tut", run=lambda book, args: ..., setup=lambda parser: ...)]
```

Ein Name, den allkvitt schon verwendet, wird übersprungen; Plugin-Befehle tragen darum am besten das Plugin im Namen.

### Seiten in der Oberfläche

```python
from allkvitt.plugins import Page

@hookimpl
def allkvitt_pages():
    return [Page("anlagen", "Anlagen", "anlagen.html", context=lambda book, query: {...},
                 actions={"erfassen": lambda book, form: api.write(book, "…", erfassen, …)},
                 bereich="abschluss")]
```

`bereich` legt fest, wo die Seite in der Navigation erscheint: als Reiter eines Bereichs (`debitoren`,
`kreditoren`, `lohn`, `konten`, `mwst`, `abschluss`). Ohne `bereich` steht sie oben unter «Mehr».

Die Seite liegt unter `/p/<plugin>/<slug>`, die Vorlage im Ordner `templates/` des Plugin-Pakets (in
`package-data` aufnehmen). Sie darf `{% extends "base.html" %}` und die Makros aus `_macros.html` verwenden.
Formulare posten an `/p/<plugin>/<slug>/<aktion>`; die Aktion bekommt die Formularfelder als dict und schreibt über
`api.write`. Im Serverbetrieb dürfen nur Benutzer mit Schreibrecht posten. Beispiel:
[`plugins/allkvitt-anlagen`](../plugins/allkvitt-anlagen).

## Testen

```python
from allkvitt.testing import make_book, assert_clean

def test_mein_plugin(tmp_path):
    book = make_book(tmp_path, plugins=["meinplugin"], eroeffnung={"1020": 1000, "2800": -1000})
    ...
    assert_clean(book)        # keine Fehler und Warnungen in allkvitt check
```

## Versionen

`ALLKVITT_PLUGIN_API` ist die Version der Hooks. Ändert sich ein Hook unverträglich, erhöht allkvitt die Version;
Plugins mit einer anderen Version werden nicht geladen und in `allkvitt plugins` mit dem Grund angezeigt — nie halb.
Neue, optionale Hooks erhöhen die Version nicht.

## Katalog, Prüfung und Installation

Der **Katalog** (`src/allkvitt/data/plugin_katalog.json`, später eine öffentliche https-Adresse über
`ALLKVITT_PLUGIN_KATALOG`) listet die Plugins mit Beschreibung, Paket, Version und den Rechten in Klartext
(«liest Kontoauszüge ein», «bucht eigene Dokumente ins Journal» …). Oberfläche: Einstellungen → Plugins → Katalog;
Kommandozeile: `allkvitt plugins katalog`.

**Geprüft** heisst: ein Maintainer hat den Code gelesen, die Tests des Plugins laufen, und der Fingerabdruck
(SHA-256 über alle Quelldateien) ist im Katalog festgehalten:

```bash
allkvitt plugins installieren revolut --ungeprueft      # zum Prüfen installieren
# Code lesen: Was tut das Plugin, schreibt es nur über api.write, sendet es Daten nach aussen?
allkvitt plugins pruefen revolut --von "Anna Muster"  # Tests laufen, Fingerabdruck in den Katalog
allkvitt plugins zurueckziehen revolut                  # Prüfung zurücknehmen
```

allkvitt vergleicht den Fingerabdruck laufend mit dem installierten Code. Weicht er ab, zeigt die Oberfläche
«Code verändert» und `allkvitt check` warnt bei Büchern, die das Plugin eingeschaltet haben.

**Installieren:** lokal mit einem Klick (nach Bestätigung; ungeprüfte Plugins nur mit dem ausdrücklichen Häkchen
«Ich habe den Code selbst geprüft»), danach startet allkvitt auf Knopfdruck neu. Im Serverbetrieb zeigt allkvitt nur den
Befehl — Programmcode wird dort nicht aus dem Browser installiert.

## Veröffentlichen

- Paketname `allkvitt-<name>`, Entry-Point-Name `<name>` (das steht dann in `allkvitt.yaml`).
- Lizenz: AGPL-3.0-or-later empfohlen (wie allkvitt); jede damit verträgliche Open-Source-Lizenz geht.
- Auf PyPI veröffentlichen und per Pull Request in den Katalog eintragen (`status: ungeprüft`); ein Maintainer prüft.

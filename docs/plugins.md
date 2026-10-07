# Plugins für aeradex

aeradex soll von der Community wachsen: eine Bank, ein Kanton, eine Branche, ein Export — ohne dass jemand
aeradex forken oder auf ein Release warten muss. Ein Plugin ist ein gewöhnliches Python-Paket.

```bash
pip install aeradex-revolut          # installieren (wie jedes Python-Paket)
aeradex plugins                      # was ist installiert, was ist eingeschaltet?
aeradex plugins ein revolut          # für dieses Buch einschalten (wird in aeradex.yaml festgehalten)
aeradex plugins aus revolut
```

In der Oberfläche: **Einstellungen → Plugins** (im Serverbetrieb nur für Admins).
Die Seiten eingeschalteter Erweiterungen stehen unter dem Hauptmenüpunkt **Plugins**.

## Die Regeln

1. **Installiert heisst nicht eingeschaltet.** Was ein Plugin *tut* (Bankformate, Dokumente mit Buchungen,
   Prüfregeln, Agenten-Werkzeuge, Befehle), wirkt nur in Büchern, die es unter `plugins:` in `aeradex.yaml` führen.
   *Daten* (Kontenplan-Vorlagen, Quellensteuer-Tabellen) bietet jedes installierte Plugin an — sie werden gebraucht,
   bevor es ein Buch gibt.
2. **Plugins schreiben über aeradex.** Änderungen laufen durch `api.write`: Buch vorher prüfen → schreiben →
   `aeradex check` → git-Commit. Macht ein Plugin das Buch ungültig, wird die Änderung abgelehnt.
3. **Wem eine Buchung gehört, der muss sie erklären.** Ein Plugin, das Journalzeilen erzeugt, registriert eine
   Quelle (`Quelle anlage:A001:2026`) und eine Funktion, die diese Zeilen aus seinen Dokumenten neu erzeugt.
   `aeradex check` vergleicht — wie bei Rechnungen und Lohn.
4. **Fehlt ein Plugin, ist das Buch nicht gültig.** Schaltet ein Buch ein Plugin ein, das auf diesem Rechner nicht
   installiert ist, meldet `check` einen Fehler und aeradex schreibt nichts mehr, bis es installiert ist. Kein Plugin
   kann so seine Buchungen «verlieren». Ausschalten geht nur, solange ihm keine Journalzeilen mehr gehören.
5. **Bücher enthalten keinen Code.** Ein geklontes Buch führt nichts aus; es nennt nur die Plugins, die es braucht.
6. **Plugins sind Programmcode** mit denselben Rechten wie aeradex. Nur aus vertrauenswürdigen Quellen installieren.
   Wo es geht, lieber ein Datenplugin bauen (siehe unten): das braucht kein Vertrauen.

## Ein Plugin bauen

```bash
cp -r plugins/vorlage ../aeradex-meinplugin && cd ../aeradex-meinplugin
grep -rl beispiel . | xargs sed -i 's/beispiel/meinplugin/g' && mv src/aeradex_beispiel src/aeradex_meinplugin
pip install -e ".[dev]" && pytest
```

Das Paket meldet sich über einen Entry Point an und nennt die Version der Plugin-API:

```toml
[project.entry-points."aeradex.plugins"]
meinplugin = "aeradex_meinplugin"
```

```python
"""Erste Zeile: erscheint in `aeradex plugins`."""
from aeradex.plugins import hookimpl

AERADEX_PLUGIN_API = 1

@hookimpl
def aeradex_check(book, rows):
    ...
```

Die Hooks sind [pluggy](https://pluggy.readthedocs.io/)-Hooks (dasselbe System wie bei pytest). Jeder ist optional.

| Hook | gibt zurück | wofür | wirkt |
|---|---|---|---|
| `aeradex_kontenplaene()` | `{"name": Path}` | Kontenplan-Vorlagen für `aeradex init --kontenplan name` | immer (Daten) |
| `aeradex_qst_tarife()` | `Path` | Ordner mit Quellensteuer-Tabellen `<KANTON>-<JAHR>.json` | immer (Daten) |
| `aeradex_bank_formats()` | `[BankFormat]` | Kontoauszüge für `aeradex bank import` | eingeschaltet |
| `aeradex_beleg_leser()` | `[BelegLeser]` | Lieferantenrechnungen auslesen (z.B. E-Rechnungen ZUGFeRD/XRechnung) | eingeschaltet |
| `aeradex_sources()` | `[Source]` | Dokumente, denen Journalzeilen gehören | eingeschaltet |
| `aeradex_check(book, rows)` | `[Finding]` | zusätzliche Prüfregeln | eingeschaltet |
| `aeradex_tools()` | `[Funktion]` | Werkzeuge für Agenten (MCP und Chat in der Oberfläche) | eingeschaltet |
| `aeradex_instructions()` | `str` | kurzer Hinweis an den Agenten, wann er die Werkzeuge nutzt | eingeschaltet |
| `aeradex_commands()` | `[Command]` | Befehle `aeradex <name> …` | eingeschaltet |
| `aeradex_pages()` | `[Page]` | eigene Seiten in der Oberfläche unter «Plugins» | eingeschaltet |
| `aeradex_reports()` | `[Report]` | weitere Berichte (Berichte, `aeradex bericht`, Agent): `Report(name, label, build, params, gruppe)`, `build(book, params)` liefert die Tabelle wie `aeradex.reports` | eingeschaltet |
| `aeradex_dossier_teile()` | `[DossierTeil]` | weitere Teile der Abschlussunterlagen (`aeradex dossier`), z.B. ein Anlagenspiegel: `build(book, jahr, format)` → `[(Dateiname, bytes)]` | eingeschaltet |

### Kontenplan-Vorlagen (Daten)

Eine YAML-Datei wie `src/aeradex/data/kontenplaene/kmu.yaml`. Optional `systemkonten:`, wenn die Vorlage andere
Nummern für die Systemkonten nutzt (`ertrag`, `debitoren`, `gewinnvortrag`, `kursgewinn` …):

```yaml
systemkonten: {ertrag: "3000"}
konten:
- {nr: "1020", name: Bank, klasse: aktiv}
```

Der Hook gibt `{name: pfad}` zurück; `aeradex init --kontenplan name` nutzt die Vorlage. Mitgeliefert sind
`kmu` (AG, GmbH), `einzelfirma` und `verein`; eine Vorlage aus einem Plugin mit gleichem Namen ersetzt sie nicht.

### Bankformate

```python
from aeradex.plugins import BankFormat

@hookimpl
def aeradex_bank_formats():
    return [BankFormat(name="meinebank", label="Meine Bank (CSV)", suffixes=(".csv",),
                       detect=lambda dateiname, daten: ..., parse=lambda daten, book: [...])]
```

`detect` erkennt die Datei (Name und Inhalt), `parse` liefert Kontoauszüge in derselben Form wie der eingebaute
camt.053-Import (`aeradex.bank.parse`): je Auszug `id`, `iban` *oder* `konto`, `schluss`/`schluss_datum` für die
Saldoabstimmung und `buchungen` mit `datum`, `betrag` (Decimal, positiv = Eingang), `gegenpartei`, `referenz`,
`text` und einer stabilen `bankref` (damit derselbe Auszug zweimal importiert keine Doppel erzeugt). Alles Weitere —
Zuordnung zu Rechnungen und Kreditoren, offene Posten, Abstimmung — macht aeradex. Der camt-Import selbst ist auf
diese Weise eingebunden (`src/aeradex/builtin.py`). Beispiel: [`plugins/aeradex-revolut`](../plugins/aeradex-revolut).

### Belegleser (Kreditoren)

```python
from aeradex.plugins import BelegLeser

def lesen(book, pfad, text_bisher):
    ...                                   # {"betrag": "123.45", "rechnungsnr": "…", "iban": "…", "_text": "…"} oder None
    return felder

@hookimpl
def aeradex_beleg_leser():
    return [BelegLeser("zugferd", "ZUGFeRD", 20, lesen)]
```

Leser laufen nach `prioritaet` (eingebaut: QR 10, PDF-Text 50, OCR 80); ein Feld behält den Wert des ersten
Lesers, der es gefunden hat, und zeigt dessen `label` als Quelle. Felder: siehe `aeradex.erfassung.FIELDS`.

### Dokumente mit Buchungen

```python
from aeradex.plugins import Source

def zeilen(book):          # {Quelle: [Row, …]} — aus den Dokumenten des Plugins, deterministisch
    ...

@hookimpl
def aeradex_sources():
    return [Source(prefix="anlage", label="Anlage", rows=zeilen, link=lambda ref: None)]
```

Geschrieben wird mit `api.write`:

```python
from aeradex import api, journal

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
from aeradex.plugins import Finding

@hookimpl
def aeradex_check(book, rows):
    return [Finding("hinweis", r.where, "…") for r in rows if ...]
```

`fehler` blockiert jedes Schreiben im Buch — nur für echte Widersprüche verwenden. `warnung` und `hinweis` informieren.

### Agenten-Werkzeuge

Gewöhnliche Funktionen mit Typen und Docstring (Args-Abschnitt) — das ist alles, was der Agent sieht. Mit
`aeradex.tools.call` wird das Buch geholt und ein Fehler als `{"ok": False, "fehler": …}` zurückgegeben. Freie
Buchungen respektieren den `agent_modus` des Buchs: dafür `aeradex.tools.direct` statt `call` verwenden.

### Befehle

```python
from aeradex.plugins import Command

@hookimpl
def aeradex_commands():
    return [Command("meinplugin-x", "Was der Befehl tut", run=lambda book, args: ..., setup=lambda parser: ...)]
```

Ein Name, den aeradex schon verwendet, wird übersprungen; Plugin-Befehle tragen darum am besten das Plugin im Namen.

### Seiten in der Oberfläche

```python
from aeradex.plugins import Page

@hookimpl
def aeradex_pages():
    return [Page("anlagen", "Anlagen", "anlagen.html", context=lambda book, query: {...},
                 actions={"erfassen": lambda book, form: api.write(book, "…", erfassen, …)})]
```

Alle Seiten eingeschalteter Plugins erscheinen als Reiter unter **Plugins** (z.B. Leistungen,
Offerten, Anlagen). Der Menüpunkt erscheint, sobald eine Erweiterung Seiten beiträgt.
Das frühere Feld `bereich` bleibt für bestehende Plugins erhalten, beeinflusst die Navigation aber nicht mehr.

Die Seite liegt unter `/p/<plugin>/<slug>`, die Vorlage im Ordner `templates/` des Plugin-Pakets (in
`package-data` aufnehmen). Sie darf `{% extends "base.html" %}` und die Makros aus `_macros.html` verwenden.
Formulare posten an `/p/<plugin>/<slug>/<aktion>`; die Aktion bekommt die Formularfelder als dict und schreibt über
`api.write`. Im Serverbetrieb dürfen nur Benutzer mit Schreibrecht posten. Beispiel:
[`plugins/aeradex-anlagen`](../plugins/aeradex-anlagen).

## Testen

```python
from aeradex.testing import make_book, assert_clean

def test_mein_plugin(tmp_path):
    book = make_book(tmp_path, plugins=["meinplugin"], eroeffnung={"1020": 1000, "2800": -1000})
    ...
    assert_clean(book)        # keine Fehler und Warnungen in aeradex check
```

## Versionen

`AERADEX_PLUGIN_API` ist die Version der Hooks. Ändert sich ein Hook unverträglich, erhöht aeradex die Version;
Plugins mit einer anderen Version werden nicht geladen und in `aeradex plugins` mit dem Grund angezeigt — nie halb.
Neue, optionale Hooks erhöhen die Version nicht.

## Katalog, Prüfung und Installation

Der **Katalog** (`src/aeradex/data/plugin_katalog.json`, später eine öffentliche https-Adresse über
`AERADEX_PLUGIN_KATALOG`) listet die Plugins mit Beschreibung, Paket, Version und den Rechten in Klartext
(«liest Kontoauszüge ein», «bucht eigene Dokumente ins Journal» …). Oberfläche: Einstellungen → Plugins → Katalog;
Kommandozeile: `aeradex plugins katalog`.

**Geprüft** heisst: ein Maintainer hat den Code gelesen, die Tests des Plugins laufen, und der Fingerabdruck
(SHA-256 über alle Quelldateien) ist im Katalog festgehalten:

```bash
aeradex plugins installieren revolut --ungeprueft      # zum Prüfen installieren
# Code lesen: Was tut das Plugin, schreibt es nur über api.write, sendet es Daten nach aussen?
aeradex plugins pruefen revolut --von "Anna Muster"  # Tests laufen, Fingerabdruck in den Katalog
aeradex plugins zurueckziehen revolut                  # Prüfung zurücknehmen
```

aeradex vergleicht den Fingerabdruck laufend mit dem installierten Code. Weicht er ab, zeigt die Oberfläche
«Code verändert» und `aeradex check` warnt bei Büchern, die das Plugin eingeschaltet haben.

**Installieren:** lokal mit einem Klick (nach Bestätigung; ungeprüfte Plugins nur mit dem ausdrücklichen Häkchen
«Ich habe den Code selbst geprüft»), danach startet aeradex auf Knopfdruck neu. Im Serverbetrieb zeigt aeradex nur den
Befehl — Programmcode wird dort nicht aus dem Browser installiert.

## Veröffentlichen

- Paketname `aeradex-<name>`, Entry-Point-Name `<name>` (das steht dann in `aeradex.yaml`).
- Lizenz: AGPL-3.0-or-later empfohlen (wie aeradex); jede damit verträgliche Open-Source-Lizenz geht.
- Auf PyPI veröffentlichen und per Pull Request in den Katalog eintragen (`status: ungeprüft`); ein Maintainer prüft.

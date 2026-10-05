# Veröffentlichen: Checkliste

Nichts davon ist erledigt, bevor es hier abgehakt ist. Veröffentlicht wird erst nach dem Parallelbetrieb
(siehe [parallelbetrieb.md](parallelbetrieb.md)).

## Namen (Stand 5. Oktober 2026)

Bis 5. Oktober 2026 hiess das Projekt «batzen».

| Wo | Name | Stand |
|---|---|---|
| PyPI | `allkvitt`, `allkvitt-anlagen`, `allkvitt-leistungen`, `allkvitt-revolut` | frei |
| GitHub | `allkvitt`, `allkvitt-ch`, `allkvitt-app`, `allkvitthq`, `getallkvitt` | frei |
| Domain | `allkvitt.ch`, `allkvitt.com`, `allkvitt.io`, `allkvitt.org`, `allkvitt.li`, `getallkvitt.ch` | frei (nicht registriert!) |
| Domain | `allquitt.ch`, `allquitt.com` (gleich gesprochen, als Weiterleitung) | frei |
| Domain | `allkvitt.app`, `allkvitt.dev` | nicht geprüft |

Im Handelsregister (Zefix) gibt es keine Firma «Allkvitt» oder «Allquitt». Ähnlicher Name im selben Umfeld:
Quitt (quitt.ch, Zürich) für Lohn- und HR-Administration von Haushalten und KMU, gesprochen nah an «…kvitt».
Das ersetzt keine Markenrecherche: im Markenregister des IGE (swissreg.ch) und bei der EUIPO nach «allkvitt»
**und «quitt»** in den Klassen 9 (Software), 35, 36 (Finanzwesen) und 42 (Software-Dienstleistungen) suchen.

Vorschlag: Domains `allkvitt.ch`, `allkvitt.com`, `allquitt.ch` sofort registrieren; GitHub-Organisation
`allkvitt-ch` (oder `allkvitt`), PyPI `allkvitt`.

## Vor dem ersten Push

- [ ] Markenrecherche (IGE/EUIPO) — oder bewusst darauf verzichten.
- [ ] Commit-Adresse: die bisherigen Commits tragen eine private E-Mail-Adresse. Entweder so lassen, oder vor dem
      ersten Push auf die GitHub-Adresse `…@users.noreply.github.com` umschreiben (`git filter-repo --mailmap`) und
      künftig `git config user.email` entsprechend setzen.
- [ ] Kontaktadresse in `CODE_OF_CONDUCT.md` eintragen.
- [ ] README → «Herkunft»: so lassen, anpassen oder streichen.
- [ ] Die Beispiel-Plugins prüfen: `allkvitt plugins pruefen NAME --von "…"`.
- [ ] Eine erzeugte QR-Rechnung im Validator von SIX prüfen (validation.iso-payments.ch); MWST-Ziffern (inkl.
      Bezugsteuer 382/383) mit dem aktuellen ESTV-Formular abgleichen; Abschreibungssätze mit dem ESTV-Merkblatt.
- [ ] Repository-URL in `pyproject.toml` (`[project.urls]`) und im Katalog (`quelle`) eintragen.

## GitHub einrichten

- [ ] Organisation anlegen, Repository `allkvitt` (öffentlich), Branch-Schutz für `main` (Tests müssen grün sein).
- [ ] Environment `pypi` mit dir als «Required reviewer» — der Release-Ablauf wartet auf deine Freigabe.
- [ ] Issues mit Vorlagen für Fehler (ohne echte Daten!) und Plugin-Einträge.

## PyPI einrichten

- [ ] Konto mit Zwei-Faktor-Anmeldung.
- [ ] «Trusted Publishing» für jedes Paket: Repository `allkvitt-ch/allkvitt`, Workflow `release.yml`, Environment `pypi`
      (für noch nicht existierende Pakete als «pending publisher»).
- [ ] Erste Veröffentlichung: Actions → Release → Paket wählen → nach Freigabe auf PyPI.

## Katalog öffentlich

- [ ] Katalog bleibt im Repository (`src/allkvitt/data/plugin_katalog.json`); die öffentliche Adresse ist die
      Raw-URL der Datei auf GitHub. Als Standard eintragen, sobald das Repository öffentlich ist.
- [ ] Ablauf für Community-Plugins: Pull Request auf den Katalog (`status: ungeprüft`) → Code lesen → installieren →
      `allkvitt plugins pruefen` → Pull Request mit Fingerabdruck mergen.

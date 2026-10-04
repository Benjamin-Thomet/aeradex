# Veröffentlichen: Checkliste

Nichts davon ist erledigt, bevor es hier abgehakt ist. Veröffentlicht wird erst nach dem Parallelbetrieb
(siehe [parallelbetrieb.md](parallelbetrieb.md)).

## Namen (Stand 4. Oktober 2026)

| Wo | Name | Stand |
|---|---|---|
| PyPI | `batzen`, `batzen-anlagen`, `batzen-revolut`, `batzen-kontenplan-verein` | frei |
| GitHub | `batzen` | belegt (Privatkonto seit 2012) |
| GitHub | `batzen-ch`, `batzen-app`, `batzenhq`, `getbatzen`, `batzen-buchhaltung` | frei |
| Domain | `batzen.ch`, `batzen.com`, `batzen.io`, `batzen.org`, `batzen.app`, `batzen.dev`, `batzen-app.ch` | registriert |
| Domain | `batzen.li`, `getbatzen.ch`, `batzenbuch.ch`, `batzen-buchhaltung.ch` | frei |

Ähnlicher Name im selben Umfeld: die Budget-App «Batzi» (batzi.ch). Eine Marke «batzen» ist bei der Websuche
nicht aufgefallen — das ersetzt keine Markenrecherche: im Markenregister des IGE (swissreg.ch) und bei der EUIPO
nach «batzen» in den Klassen 9 (Software), 36 (Finanzwesen) und 42 (Software-Dienstleistungen) suchen.

Vorschlag: GitHub-Organisation `batzen-ch`, PyPI `batzen`, Domain `getbatzen.ch` oder `batzen.li`.

## Vor dem ersten Push

- [ ] Markenrecherche (IGE/EUIPO) — oder bewusst darauf verzichten.
- [ ] Commit-Adresse: die bisherigen Commits tragen eine private E-Mail-Adresse. Entweder so lassen, oder vor dem
      ersten Push auf die GitHub-Adresse `…@users.noreply.github.com` umschreiben (`git filter-repo --mailmap`) und
      künftig `git config user.email` entsprechend setzen.
- [ ] Kontaktadresse in `CODE_OF_CONDUCT.md` eintragen.
- [ ] README → «Herkunft»: so lassen, anpassen oder streichen.
- [ ] Die Beispiel-Plugins prüfen: `batzen plugins pruefen NAME --von "…"`.
- [ ] Eine erzeugte QR-Rechnung im Validator von SIX prüfen (validation.iso-payments.ch); MWST-Ziffern (inkl.
      Bezugsteuer 382/383) mit dem aktuellen ESTV-Formular abgleichen; Abschreibungssätze mit dem ESTV-Merkblatt.
- [ ] Repository-URL in `pyproject.toml` (`[project.urls]`) und im Katalog (`quelle`) eintragen.

## GitHub einrichten

- [ ] Organisation anlegen, Repository `batzen` (öffentlich), Branch-Schutz für `main` (Tests müssen grün sein).
- [ ] Environment `pypi` mit dir als «Required reviewer» — der Release-Ablauf wartet auf deine Freigabe.
- [ ] Issues mit Vorlagen für Fehler (ohne echte Daten!) und Plugin-Einträge.

## PyPI einrichten

- [ ] Konto mit Zwei-Faktor-Anmeldung.
- [ ] «Trusted Publishing» für jedes Paket: Repository `batzen-ch/batzen`, Workflow `release.yml`, Environment `pypi`
      (für noch nicht existierende Pakete als «pending publisher»).
- [ ] Erste Veröffentlichung: Actions → Release → Paket wählen → nach Freigabe auf PyPI.

## Katalog öffentlich

- [ ] Katalog bleibt im Repository (`src/batzen/data/plugin_katalog.json`); die öffentliche Adresse ist die
      Raw-URL der Datei auf GitHub. Als Standard eintragen, sobald das Repository öffentlich ist.
- [ ] Ablauf für Community-Plugins: Pull Request auf den Katalog (`status: ungeprüft`) → Code lesen → installieren →
      `batzen plugins pruefen` → Pull Request mit Fingerabdruck mergen.

# Veröffentlichen: Checkliste

Nichts davon ist erledigt, bevor es hier abgehakt ist. Veröffentlicht wird erst nach dem Parallelbetrieb
(siehe [parallelbetrieb.md](parallelbetrieb.md)).

## Namen (Stand 7. Oktober 2026)

Das Projekt hiess bis 5. Oktober 2026 «batzen», danach bis 7. Oktober 2026 «allkvitt». Der Name «aeradex» verbindet
lat. *aera* («Rechenmarken, daher auch die einzelnen Posten einer berechneten Summe», Lewis & Short, s. v. *aes*)
mit *index* (Verzeichnis).

| Wo | Name | Stand |
|---|---|---|
| PyPI | `aeradex`, `aeradex-anlagen`, `aeradex-leistungen`, `aeradex-revolut` | frei |
| GitHub | Organisation `aeradex` | gehört uns (angelegt 30.06.2026) |
| Domain | `aeradex.com` | gehört uns (DNS bei Cloudflare) |
| Domain | `aeradex.ch`, `aeradex.li`, `aeradex.io`, `aeradex.org`, `aeradex.app`, `aeradex.dev`, `getaeradex.ch` | im DNS nicht registriert (nicht gekauft!) |

Handelsregister (Zefix) und Markenregister sind für «aeradex» noch nicht geprüft. Im Markenregister des IGE
(swissreg.ch) und bei der EUIPO nach «aeradex» in den Klassen 9 (Software), 35, 36 (Finanzwesen) und 42
(Software-Dienstleistungen) suchen.

Vorschlag: `aeradex.ch` sofort registrieren und auf `aeradex.com` weiterleiten; Repository `aeradex/aeradex`,
PyPI `aeradex`.

## Vor dem ersten Push

- [ ] Markenrecherche (IGE/EUIPO) — oder bewusst darauf verzichten.
- [ ] Commit-Adresse: die bisherigen Commits tragen eine private E-Mail-Adresse. Entweder so lassen, oder vor dem
      ersten Push auf die GitHub-Adresse `…@users.noreply.github.com` umschreiben (`git filter-repo --mailmap`) und
      künftig `git config user.email` entsprechend setzen.
- [ ] Kontaktadresse in `CODE_OF_CONDUCT.md` eintragen.
- [ ] README → «Herkunft»: so lassen, anpassen oder streichen.
- [ ] Die Beispiel-Plugins prüfen: `aeradex plugins pruefen NAME --von "…"`.
- [ ] Eine erzeugte QR-Rechnung im Validator von SIX prüfen (validation.iso-payments.ch); MWST-Ziffern (inkl.
      Bezugsteuer 382/383) mit dem aktuellen ESTV-Formular abgleichen; Abschreibungssätze mit dem ESTV-Merkblatt.
- [ ] Repository-URL in `pyproject.toml` (`[project.urls]`) und im Katalog (`quelle`) eintragen.

## GitHub einrichten

- [ ] Organisation anlegen, Repository `aeradex` (öffentlich), Branch-Schutz für `main` (Tests müssen grün sein).
- [ ] Environment `pypi` mit dir als «Required reviewer» — der Release-Ablauf wartet auf deine Freigabe.
- [ ] Issues mit Vorlagen für Fehler (ohne echte Daten!) und Plugin-Einträge.

## PyPI einrichten

- [ ] Konto mit Zwei-Faktor-Anmeldung.
- [ ] «Trusted Publishing» für jedes Paket: Repository `aeradex/aeradex`, Workflow `release.yml`, Environment `pypi`
      (für noch nicht existierende Pakete als «pending publisher»).
- [ ] Erste Veröffentlichung: Actions → Release → Paket wählen → nach Freigabe auf PyPI.

## Katalog öffentlich

- [ ] Katalog bleibt im Repository (`src/aeradex/data/plugin_katalog.json`); die öffentliche Adresse ist die
      Raw-URL der Datei auf GitHub. Als Standard eintragen, sobald das Repository öffentlich ist.
- [ ] Ablauf für Community-Plugins: Pull Request auf den Katalog (`status: ungeprüft`) → Code lesen → installieren →
      `aeradex plugins pruefen` → Pull Request mit Fingerabdruck mergen.

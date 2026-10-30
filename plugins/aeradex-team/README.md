# aeradex-team

Team-App für [aeradex](../../README.md): Mitarbeitende erfassen Zeit, Material und Abwesenheiten auf dem Handy und
sehen ihre Überstunden, Ferien und – Ende-zu-Ende verschlüsselt – ihre Lohnabrechnungen. Dieses Plugin verbindet
das Buch mit dem Portal [aeradex-team-portal](../aeradex-team-portal); Einträge aus der App werden normale
Leistungen im Plugin [aeradex-leistungen](../aeradex-leistungen).

```
Handys ──https──► Portal (/app, Briefkasten + Abbild) ◄── aeradex team sync ──► Buch (git)
```

Das Buch verlässt nie seinen Rechner bzw. Server. Das Portal hält nur, was die Handys brauchen, und nichts davon
ohne das Buch: Einträge warten dort, bis der Abgleich sie übernimmt. Protokoll: [PROTOCOL.md](PROTOCOL.md).

## Was die App kann

- **Erfassen:** Arbeitszeit (Stunden oder von–bis, Kunde oder intern, Projekt optional, Leistungsart, Text),
  Material (Kunde, Projekt, Produkt, Menge), zuletzt verwendete Kombinationen. Ohne Empfang bleibt alles auf dem
  Handy und wird später gesendet.
- **Woche:** eigene Einträge mit Status *auf dem Handy → gesendet → übernommen* bzw. *abgelehnt (mit Grund)*.
  Eigene Einträge lassen sich ändern und löschen, auch nach der Übernahme, bis das Büro den Zeitraum abschliesst oder
  sie verrechnet. Das Buch entscheidet, nicht das Handy.
- **Abwesend:** Ferien, Kompensation, Weiterbildung, Unbezahlt als Antrag (Bewilligung im Büro); Krank und Unfall
  direkt.
- **Konto:** Überstundensaldo (Monat, Jahr), Ferien (Anspruch, Vortrag, bezogen, geplant, Rest), Passwort.
- **Lohn:** Lohnabrechnungen, Lohnausweise, Arbeitszeitnachweise (Art. 73 ArGV 1), verschlüsselt mit dem Schlüssel
  der Person: auf dem Portal liegt nur Chiffretext, entschlüsselt wird im Browser.

- **Kunden:** Suche nach Name, Strasse, Ort oder Objekt; Adresse mit Link zur Karte (Apple Karten auf dem iPhone,
  sonst Google Maps), Objekte des Kunden, von dort direkt Zeit oder Material erfassen. Beim Erfassen schlägt das
  Kundenfeld beim Tippen passende Kunden vor.

Mitarbeitende sehen nie Preise, Stundensätze, Löhne anderer, E-Mail und Notizen der Kunden oder das Buch.

## Im Büro (Lohn › Team-App)

- **Zu prüfen:** neue und geänderte Einträge aus der App mit Hinweisen (über 10 h am Tag, Wochenende, Feiertag,
  ohne Stundensatz, Projekt abgeschlossen …); «geprüft» einzeln oder alle.
- **Team:** Pensum (mit Verlauf), Saldo Monat und Jahr, Ferien, App-Status; Einladen (Einmal-Code), Sperren.
- **Abschliessen** bis Datum (die App sperrt Einträge bis dahin), **Pensum ab Datum**, Anträge bewilligen/ablehnen.
- **Rechtschreibung und Stil:** Texte aus der App korrigiert der Agent des Buchs vor dem Buchen (ein Aufruf pro
  Abgleich, ohne Werkzeuge). Zahlen und Masse bleiben, sonst gilt das Original. Ohne Agent bleibt alles, wie es ist.
- Jeder übernommene Eintrag ist ein eigener geprüfter git-Commit mit Autor «Name (App)».
- Der Agent kennt `team_status`, `team_requests`, `decide_request`, `team_sync`.

## Einrichten

```bash
pip install -e plugins/aeradex-leistungen -e plugins/aeradex-team
aeradex --buch <buch> plugins ein leistungen
aeradex --buch <buch> plugins ein team
aeradex --buch <buch> team verbinden --url https://team.example.ch --token <Sync-Token des Portals>
aeradex --buch <buch> team sync                      # oder als Dienst: team sync --wiederholen 300
```

Portal und Abgleich auf einem Server richtet [deploy/](../../deploy) ein. Mitarbeitende einladen: Lohn › Team-App ›
*Einladen* (oder `aeradex team einladen M0001`), Adresse und Code weitergeben.

Einstellungen in `team/einstellungen.yaml` (im Buch, in git), neben Konten und Abschluss:

| Schlüssel | Bedeutung |
|---|---|
| `leistungsart` | eine feste Leistungsart für jede Kundenstunde; die App fragt dann nicht (auch in der Oberfläche) |
| `textkorrektur` | `false` schaltet die Korrektur ab (auch in der Oberfläche) |
| `branche` | für die Korrektur, z.B. «Schreinerei» (Standard «Handwerksbetrieb») |
| `text_beispiel` | Beispiel eines guten Rapporttexts: Platzhalter in der App und Stilvorlage der Korrektur |
| `arztzeugnis_ab_tag` | Hinweis in der App bei Krankheit, z.B. `3`; `0` = kein Hinweis |
| `stunden_in_app` | `false`: Stunden und Material kommen auf Papier (gescannte Wochen-, Regie- und Material-Rapporte, Plugin leistungen); die App zeigt nur noch Abwesenheiten, Konto, Kunden und Lohn |

## Tests

```bash
pip install -e "plugins/aeradex-team-portal[dev]" -e "plugins/aeradex-team[dev]"
pytest plugins/aeradex-team
```

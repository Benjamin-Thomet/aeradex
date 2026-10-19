# aeradex-anlagen

Anlagenbuchhaltung für [aeradex](../../README.md): Anschaffungen aus dem Belegeingang als Anlage erfassen, Bestand
übernehmen, Abschreibungen buchen (linear oder degressiv, im ersten Jahr anteilig nach Monaten), Abgänge mit
Verkaufserlös, Anlagenspiegel. Mit eigener Seite (Buchhaltung → Anlagen), als Bericht und in den Abschlussunterlagen.

```bash
pip install aeradex-anlagen && aeradex plugins ein anlagen
aeradex anlagen kandidaten                                   # gebuchte Anschaffungen, die noch keine Anlage sind
aeradex anlagen add --beleg E-2026-0003 --konto 1500 --bezeichnung "Airless-Spritzgerät"
aeradex anlagen add --bezeichnung "Laptop Lea" --datum 2026-03-15 --wert 2400 --kategorie edv
aeradex anlagen uebernehmen --bezeichnung "Lieferwagen" --datum 2022-03-01 --wert 38000 --buchwert 15000 \
    --kategorie fahrzeuge --einlage                          # Einzelfirma: Einlage an Eigenkapital
aeradex anlagen abschreiben --jahr 2026                      # bucht per 31.12. 6800 an die Anlagekonten
aeradex anlagen list --jahr 2026                             # Anlagenspiegel
aeradex anlagen abgang --nummer A0001 --datum 2027-06-30 --wert 800
```

- **Aus dem Belegeingang:** Eine Lieferantenrechnung oder Quittung auf ein Anlagekonto (15xx–17xx) gebucht, und die
  Oberfläche geht gleich weiter zu «Anschaffungen ohne Anlage». Datum, Konto und Netto-Betrag (nach Abzug der
  Vorsteuer) kommen aus der Buchung. Eine Rechnung mit mehreren Geräten wird in Teilbeträgen erfasst. Was keine Anlage
  ist (z.B. eine Umbuchung), wird als «keine Anlage» markiert. Bis dahin weist `aeradex check` darauf hin.
- **Standard linear:** Die Nutzungsdauer ergibt den Satz (100 / Jahre). Ohne Angabe gilt der halbe ESTV-Höchstsatz
  der Kategorie (Merkblatt A 1995, Geschäftsbetriebe), degressiv der ganze. Die Kategorien sind:

  | Kategorie | Höchstsatz degressiv | Konto |
  |---|---|---|
  | Mobiliar | 25 % | 1510 |
  | Büromaschinen | 40 % | 1520 |
  | EDV | 40 % | 1520 |
  | Fahrzeuge | 40 % | 1530 |
  | Maschinen | 30 % | 1500 |
  | Werkzeuge | 45 % | 1540 |
  | Gerüste/Leitern | 45 % | 1540 |

  Vor Gebrauch prüfen. Liegenschaften und eigene Sätze: Satz von Hand angeben.
- **Abschreiben:** Pro Anlage ist ein anderer Betrag möglich (tiefer oder zusätzlich, höchstens Buchwert minus
  Restwert). Steuerlich gelten die ESTV-Sätze als Obergrenze.
- **Bestand übernehmen:** Was vor dem ersten Buchjahr schon da war, kommt mit dem Wert per 1. Januar ins Buch. Entweder
  steht es schon in der Eröffnungsbilanz, oder es wird als Einlage gebucht (Einzelfirma: an Eigenkapital).
- **Prüfung:** Die Abschreibungen, Einlagen und Abgänge gehören der Anlage (`Quelle abschreibung:A0001:2026`).
  `aeradex check` prüft sie und vergleicht die Anlagekonten mit dem Anlagenspiegel.
- **Bearbeiten:** Eine falsch erfasste Anlage lässt sich löschen, solange nichts abgeschrieben ist. Bezeichnung,
  Nutzungsdauer, Restwert und Notiz lassen sich jederzeit ändern. Sie gelten für die noch nicht gebuchten Jahre.
- **Agent:** `fixed_assets`, `asset_candidates`, `add_asset_from_booking`, `add_existing_asset`. Der Agent erfasst nur
  Anlagen. Abschreibungen, Einlagen und Abgänge bucht ein Mensch.

# aeradex-anlagen

Anlagenbuchhaltung für [aeradex](../../README.md): Anlagen erfassen, Abschreibungen buchen (linear oder degressiv,
im Anschaffungsjahr anteilig nach Monaten), Abgänge mit Verkaufserlös, Anlagenspiegel. Mit eigener Seite in der
Oberfläche (Plugins → Anlagen).

```bash
pip install aeradex-anlagen && aeradex plugins ein anlagen
aeradex anlagen add --bezeichnung "Laptop Lea" --datum 2026-03-15 --wert 2400 --kategorie edv
aeradex anlagen abschreiben --jahr 2026     # bucht per 31.12. 6800 an 1520
aeradex anlagen list --jahr 2026            # Anlagenspiegel
aeradex anlagen abgang --nummer A0001 --datum 2027-06-30 --wert 800
```

- Kategorien mit den Höchstsätzen der ESTV (Merkblatt A 1995, Geschäftsbetriebe): Mobiliar 25 %, Büromaschinen 40 %,
  EDV 40 %, Fahrzeuge 40 %, Maschinen 30 %, Werkzeuge 45 % — degressiv; linear die Hälfte. Vor Gebrauch prüfen.
  Liegenschaften und eigene Sätze: Satz von Hand angeben.
- Die Abschreibungen gehören der Anlage (`Quelle abschreibung:A0001:2026`); `aeradex check` prüft sie und vergleicht
  die Anlagekonten mit dem Anlagenspiegel.

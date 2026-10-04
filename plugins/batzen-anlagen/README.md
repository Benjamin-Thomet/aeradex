# batzen-anlagen

Anlagenbuchhaltung für [batzen](../../README.md): Anlagen erfassen, Abschreibungen buchen (linear oder degressiv,
im Anschaffungsjahr anteilig nach Monaten), Abgänge mit Verkaufserlös, Anlagenspiegel. Mit eigener Seite in der
Oberfläche (Plugins → Anlagen).

```bash
pip install batzen-anlagen && batzen plugins ein anlagen
batzen anlagen add --bezeichnung "Laptop Lea" --datum 2026-03-15 --wert 2400 --kategorie edv
batzen anlagen abschreiben --jahr 2026     # bucht per 31.12. 6800 an 1520
batzen anlagen list --jahr 2026            # Anlagenspiegel
batzen anlagen abgang --nummer A0001 --datum 2027-06-30 --wert 800
```

- Kategorien mit den Höchstsätzen der ESTV (Merkblatt A 1995, Geschäftsbetriebe): Mobiliar 25 %, Büromaschinen 40 %,
  EDV 40 %, Fahrzeuge 40 %, Maschinen 30 %, Werkzeuge 45 % — degressiv; linear die Hälfte. Vor Gebrauch prüfen.
  Liegenschaften und eigene Sätze: Satz von Hand angeben.
- Die Abschreibungen gehören der Anlage (`Quelle abschreibung:A0001:2026`); `batzen check` prüft sie und vergleicht
  die Anlagekonten mit dem Anlagenspiegel.

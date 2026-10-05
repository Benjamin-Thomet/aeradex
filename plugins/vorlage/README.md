# Vorlage für allkvitt-Plugins

Kopieren, umbenennen, loslegen:

```bash
cp -r plugins/vorlage ../allkvitt-meinplugin && cd ../allkvitt-meinplugin
grep -rl beispiel . | xargs sed -i 's/beispiel/meinplugin/g'
mv src/allkvitt_beispiel src/allkvitt_meinplugin
pip install -e ".[dev]" && pytest
allkvitt plugins                      # erscheint jetzt als installiert
allkvitt plugins ein meinplugin       # für ein Buch einschalten
```

Was ein Plugin darf und wie die Hooks funktionieren: [docs/plugins.md](../../docs/plugins.md).

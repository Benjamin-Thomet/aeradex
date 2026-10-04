# Vorlage für batzen-Plugins

Kopieren, umbenennen, loslegen:

```bash
cp -r plugins/vorlage ../batzen-meinplugin && cd ../batzen-meinplugin
grep -rl beispiel . | xargs sed -i 's/beispiel/meinplugin/g'
mv src/batzen_beispiel src/batzen_meinplugin
pip install -e ".[dev]" && pytest
batzen plugins                      # erscheint jetzt als installiert
batzen plugins ein meinplugin       # für ein Buch einschalten
```

Was ein Plugin darf und wie die Hooks funktionieren: [docs/plugins.md](../../docs/plugins.md).

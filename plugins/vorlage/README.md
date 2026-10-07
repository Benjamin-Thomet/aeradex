# Vorlage für aeradex-Plugins

Kopieren, umbenennen, loslegen:

```bash
cp -r plugins/vorlage ../aeradex-meinplugin && cd ../aeradex-meinplugin
grep -rl beispiel . | xargs sed -i 's/beispiel/meinplugin/g'
mv src/aeradex_beispiel src/aeradex_meinplugin
pip install -e ".[dev]" && pytest
aeradex plugins                      # erscheint jetzt als installiert
aeradex plugins ein meinplugin       # für ein Buch einschalten
```

Was ein Plugin darf und wie die Hooks funktionieren: [docs/plugins.md](../../docs/plugins.md).

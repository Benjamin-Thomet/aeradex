# Buch mit Team-App aus einer YAML-Datei

`einrichten.py` legt ein neues Buch an: Firma, Plugins leistungen und team, Leistungsarten, Material, Kategorien,
Mitarbeitende mit App-Konten und die Texte der Team-App. `muster.yaml` ist eine fiktive Malerei; für eine echte
Firma eine Kopie anpassen (und nicht ins Repo legen).

```bash
pip install -e ".[ui]" -e plugins/aeradex-leistungen -e plugins/aeradex-team
python examples/team-einrichtung/einrichten.py ~/buchhaltung/muster
python examples/team-einrichtung/einrichten.py ~/buchhaltung/firma --daten ~/firma.yaml
```

"""Build src/aeradex/data/plz_kanton.json from the official swisstopo locality directory.

Source: Amtliches Ortschaftenverzeichnis mit Postleitzahl und Perimeter (swisstopo, open data),
https://data.geo.admin.ch/ch.swisstopo-vd.ortschaftenverzeichnis_plz/ortschaftenverzeichnis_plz/ortschaftenverzeichnis_plz_2056.csv.zip

    python tools/plz_kanton.py AMTOVZ_CSV_LV95.csv

Output: {"quelle": …, "stand": …, "plz": {"3600": {"BE": {"g": [Gemeinden], "o": [Ortschaften]}}, …}} — every
canton a postcode reaches with its municipalities and localities, so a postcode spanning cantons can be resolved
by the place (municipality names first).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "aeradex" / "data" / "plz_kanton.json"


def main(source: str) -> None:
    table: dict[str, dict[str, set[str]]] = {}
    with open(source, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            plz, kanton = row["PLZ4"].strip(), row["Kantonskürzel"].strip()
            if not (plz.isdigit() and kanton):
                continue
            entry = table.setdefault(plz, {}).setdefault(kanton, {"g": set(), "o": set()})
            entry["g"].add(row["Gemeindename"].strip())
            entry["o"].add(row["Ortschaftsname"].strip())
    data = {"quelle": "swisstopo, Amtliches Ortschaftenverzeichnis (AMTOVZ)", "stand": date.today().isoformat(),
            "plz": {p: {k: {"g": sorted(v["g"] - {""}), "o": sorted(v["o"] - {""})} for k, v in sorted(c.items())}
                    for p, c in sorted(table.items())}}
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    multi = sum(1 for c in table.values() if len(c) > 1)
    print(f"{len(table)} Postleitzahlen, {multi} über mehrere Kantone → {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])

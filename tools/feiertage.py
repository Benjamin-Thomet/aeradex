"""Build plugins/aeradex-leistungen/src/aeradex_leistungen/data/feiertage.json: the public holidays of every
canton for 2024–2045, from the `holidays` package (https://pypi.org/project/holidays/, which models the legal
holidays per canton). Only needed to regenerate the file; the plugin does not depend on the package.

    pip install holidays && python tools/feiertage.py
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import holidays

CANTONS = "AG AI AR BE BL BS FR GE GL GR JU LU NE NW OW SG SH SO SZ TG TI UR VD VS ZG ZH".split()
YEARS = range(2024, 2046)
OUT = (Path(__file__).resolve().parent.parent / "plugins" / "aeradex-leistungen" / "src" / "aeradex_leistungen"
       / "data" / "feiertage.json")


def main() -> None:
    data = {"quelle": f"holidays {holidays.__version__} (gesetzliche Feiertage je Kanton)",
            "stand": date.today().isoformat(), "kantone": {}}
    for k in CANTONS:
        days = holidays.Switzerland(subdiv=k, years=YEARS, language="de")
        data["kantone"][k] = {d.isoformat(): name for d, name in sorted(days.items())}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT}: {sum(len(v) for v in data['kantone'].values())} Feiertage")


if __name__ == "__main__":
    main()

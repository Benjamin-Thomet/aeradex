"""Postcode → canton, from the official locality directory (swisstopo AMTOVZ, data/plz_kanton.json;
rebuilt with tools/plz_kanton.py).

A postcode can reach into two cantons (169 do, e.g. 1290 GE/VD): then the place name decides. A postcode
that is not an address postcode (3000 Bern, PO boxes) falls back to the place name alone."""
from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "plz_kanton.json"


def _norm(name: str) -> str:
    text = unicodedata.normalize("NFKD", (name or "").strip().lower())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("-", " ").replace(".", "")


@lru_cache(maxsize=1)
def _data() -> tuple[dict, dict[str, set[str]]]:
    table = json.loads(DATA.read_text(encoding="utf-8"))["plz"]
    by_name: dict[str, set[str]] = {}
    for cantons in table.values():
        for kanton, names in cantons.items():
            for n in names["g"] + names["o"]:
                by_name.setdefault(_norm(n), set()).add(kanton)
    return table, by_name


def kanton(plz: str, ort: str = "") -> str | None:
    """The canton of a Swiss address, or None when it cannot be told (unknown, or ambiguous)."""
    table, by_name = _data()
    plz = str(plz or "").strip()
    name = _norm(ort)
    cantons = table.get(plz)
    if cantons:
        if len(cantons) == 1:
            return next(iter(cantons))
        for kind in ("g", "o"):                    # a municipality name is the stronger hint
            hits = [k for k, names in cantons.items() if name and any(_norm(n) == name for n in names[kind])]
            if len(hits) == 1:
                return hits[0]
        return None
    found = by_name.get(name) or set()
    return next(iter(found)) if len(found) == 1 else None


def choices(plz: str) -> list[str]:
    """The cantons a postcode reaches (for asking when it is ambiguous)."""
    return sorted(_data()[0].get(str(plz or "").strip(), {}))

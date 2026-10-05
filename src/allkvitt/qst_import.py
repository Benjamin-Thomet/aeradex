"""Import Quellensteuer tariff tables from a cantonal Wegleitung PDF.

Offline tool, not imported by the app. It turns the tariff tables at the back of
a Wegleitung into one JSON file per canton and year under `data/qst_tarife/`,
which `qst.py` then reads at runtime.

    allkvitt qst-import "Wegleitung Quellensteuer BS 2026.pdf" --kanton BS --jahr 2026

The tables are stored per family and church-tax variant, not per code: one
`C`/`Y` table holds all six children columns, so `C0Y` and `C3Y` are two columns
of the same table. That keeps the file at a few hundred KB instead of a few MB.

Page ranges are detected from the "Quellensteuertarif X mit/ohne Kirchensteuer"
headings, so a new edition with shifted pages imports without changes here.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

# "3’651 – 3’700 5.30 2.83 0.51 0.00 0.00 0.00" — the apostrophe is U+2019 and
# the dash an en dash; both are matched loosely so other cantons parse too.
ROW = re.compile(r"([\d’']+)\s*[–—-]\s*([\d’']+)((?:\s+\d+\.\d\d)+)")
HEADING = re.compile(r"Quellensteuertarif ([ABCH])\s+(mit|ohne)\s+Kirchensteuer")

# Which children columns each family publishes. H (Alleinerziehende) has no
# childless column — its first column is one child.
FIRST_CHILD_COLUMN = {"A": 0, "B": 0, "C": 0, "H": 1}
FAMILY_LABEL = {
    "A": "Alleinstehende Personen",
    "B": "Verheiratete Personen / Alleinverdiener",
    "C": "Verheiratete Personen / Doppelverdiener",
    "H": "Alleinerziehende Personen",
}


def _num(text: str) -> int:
    return int(text.replace("’", "").replace("'", ""))


def parse(pdf_path: Path) -> dict:
    """Return {family: {"Y"|"N": [[from, to, rate...], ...]}} from a Wegleitung."""
    reader = PdfReader(str(pdf_path))
    pages = [(page.extract_text() or "") for page in reader.pages]

    # Every tariff page repeats its own heading, including continuation pages, so
    # each page is assigned on its own. Pages without an A/B/C/H heading are
    # skipped outright — that keeps the G, Q, L/M/N/P and E tables, which have a
    # different column layout, from leaking into the table above them.
    tables: dict[tuple[str, str], list] = defaultdict(list)
    for text in pages:
        heading = HEADING.search(text)
        if heading is None:
            continue
        key = (heading.group(1), "Y" if heading.group(2) == "mit" else "N")
        for low, high, rates in ROW.findall(text):
            tables[key].append([_num(low), _num(high)] + [float(r) for r in rates.split()])

    out: dict[str, dict[str, list]] = defaultdict(dict)
    for (family, kist), rows in tables.items():
        rows.sort()
        _validate(family, kist, rows)
        out[family][kist] = rows
    return dict(out)


def _validate(family: str, kist: str, rows: list) -> None:
    """Fail loudly on a table that would silently produce wrong deductions."""
    where = f"Tarif {family}{kist}"
    if not rows:
        raise ValueError(f"{where}: keine Tarifstufen gefunden")

    widths = {len(r) - 2 for r in rows}
    if len(widths) != 1:
        raise ValueError(f"{where}: uneinheitliche Spaltenzahl {sorted(widths)}")
    expected = 6 - FIRST_CHILD_COLUMN[family]
    if widths != {expected}:
        raise ValueError(f"{where}: {widths.pop()} Kinderspalten erwartet wurden {expected}")

    if rows[0][0] != 1:
        raise ValueError(f"{where}: erste Stufe beginnt bei {rows[0][0]}, erwartet 1")
    for prev, nxt in zip(rows, rows[1:]):
        if nxt[0] != prev[1] + 1:
            raise ValueError(f"{where}: Lücke oder Überlappung bei {prev[1]} / {nxt[0]}")
    for row in rows:
        # Within a stufe the rate must fall as children are added, never rise.
        if any(b - a > 1e-9 for a, b in zip(row[2:], row[3:])):
            raise ValueError(f"{where}: Satz steigt mit der Kinderzahl bei Stufe {row[0]}–{row[1]}")


def build(pdf_path: Path, kanton: str, jahr: int) -> dict:
    tables = parse(pdf_path)
    missing = sorted(set(FIRST_CHILD_COLUMN) - set(tables))
    if missing:
        raise ValueError(f"Tariffamilien fehlen im PDF: {', '.join(missing)}")
    return {
        "kanton": kanton.upper(),
        "jahr": jahr,
        "quelle": pdf_path.name,
        "familien": {
            family: {
                "bezeichnung": FAMILY_LABEL[family],
                "erste_kinderspalte": FIRST_CHILD_COLUMN[family],
                "stufen": tables[family],
            }
            for family in sorted(tables)
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path, help="Wegleitung-PDF mit den Tariftabellen")
    ap.add_argument("--kanton", required=True, help="Kantonskürzel, z.B. BS")
    ap.add_argument("--jahr", required=True, type=int, help="Gültigkeitsjahr, z.B. 2026")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "data" / "qst_tarife")
    args = ap.parse_args()

    data = build(args.pdf, args.kanton, args.jahr)
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{data['kanton']}-{data['jahr']}.json"
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"{target}  ({target.stat().st_size / 1024:.0f} KB)")
    for family, block in data["familien"].items():
        counts = {k: len(v) for k, v in block["stufen"].items()}
        print(f"  Tarif {family}: {counts}  {block['bezeichnung']}")


if __name__ == "__main__":
    main()

"""Which documents each employee gets in the app: closed payslips, Lohnausweise, recent Arbeitszeitnachweise."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Callable

from aeradex.book import MONTHS_DE, Book
from aeradex.files import read_frontmatter
from aeradex_leistungen import abwesenheit

ZEIT_MONATE = 3


@dataclass
class Doc:
    person: str
    doc_id: str
    title: str
    kind: str
    period: str
    sha256: str
    content: Callable[[], bytes]


def _file(path) -> tuple[str, Callable[[], bytes]]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), lambda: data


def wanted(book: Book, person: str, today: date | None = None) -> list[Doc]:
    today = today or date.today()
    out = []
    folder = book.root / "lohn"
    for md in sorted(folder.glob(f"*/*/{person}.md")) if folder.exists() else []:
        meta, _ = read_frontmatter(md)
        pdf = md.with_suffix(".pdf")
        if meta.get("status") != "abgeschlossen" or not pdf.exists():
            continue
        y, m = int(md.parent.parent.name), int(md.parent.name)
        sha, content = _file(pdf)
        out.append(Doc(person, f"lohn-{y}-{m:02d}", f"Lohnabrechnung {MONTHS_DE[m]} {y}", "lohn", f"{y}-{m:02d}",
                       sha, content))
    folder = book.root / "lohnausweise"
    for pdf in sorted(folder.glob(f"*/{person}.pdf")) if folder.exists() else []:
        y = int(pdf.parent.name)
        sha, content = _file(pdf)
        out.append(Doc(person, f"lohnausweis-{y}", f"Lohnausweis {y}", "lohnausweis", f"{y}-12", sha, content))
    y, m = today.year, today.month
    for _ in range(ZEIT_MONATE):
        record = abwesenheit.time_record(book, person, y, m)
        if any(d["ist"] for d in record["tage"]):
            fingerprint = hashlib.sha256(json.dumps(record["tage"], default=str, sort_keys=True).encode()).hexdigest()
            out.append(Doc(person, f"zeit-{y}-{m:02d}", f"Arbeitszeitnachweis {MONTHS_DE[m]} {y}", "zeit",
                           f"{y}-{m:02d}", fingerprint,
                           lambda y=y, m=m: abwesenheit.time_record_pdf(book, person, y, m)))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out

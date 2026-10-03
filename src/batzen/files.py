"""Plain-text storage: YAML files, Markdown with YAML frontmatter, Markdown tables.

Everything a book holds is one of these three shapes, so a person in an editor,
an LLM in a harness and this engine all read the same files. The writers here
are deterministic — the same data always produces the same bytes — which keeps
git diffs small and makes the lock hashes in `check.py` stable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml


class FormatError(ValueError):
    """A file that does not have the shape batzen expects."""


# ---------- YAML ----------

class _Dumper(yaml.SafeDumper):
    pass


def _decimal_representer(dumper, value: Decimal):
    # Money stays a plain number in the file ("45.80"), never a tagged object.
    return dumper.represent_scalar("tag:yaml.org,2002:float", f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else str(value))


def _str_representer(dumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(Decimal, _decimal_representer)
_Dumper.add_representer(str, _str_representer)


def dump_yaml(data) -> str:
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)


def read_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FormatError(f"{path}: kein gültiges YAML ({exc})") from exc


def write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(data), encoding="utf-8")


# ---------- Markdown with frontmatter ----------

_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def read_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = _FRONT.match(text)
    if not match:
        raise FormatError(f"{path}: Frontmatter (--- … ---) fehlt")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise FormatError(f"{path}: Frontmatter ist kein gültiges YAML ({exc})") from exc
    if not isinstance(meta, dict):
        raise FormatError(f"{path}: Frontmatter muss eine Zuordnung (key: value) sein")
    return meta, match.group(2)


def write_frontmatter(path: Path, meta: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body.strip("\n")
    path.write_text("---\n" + dump_yaml(meta) + "---\n" + (f"\n{body}\n" if body else ""),
                    encoding="utf-8")


# ---------- Markdown tables ----------

@dataclass
class MdTable:
    """A Markdown document whose payload is one pipe table.

    Text before and after the table is kept verbatim, so a heading or a note
    above the table survives every rewrite."""
    columns: list[str]
    rows: list[dict[str, str]]
    head: str = ""
    tail: str = ""
    align: dict[str, str] = field(default_factory=dict)


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells, buf, i = [], "", 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if ch == "|":
            cells.append(buf.strip())
            buf = ""
        else:
            buf += ch
        i += 1
    cells.append(buf.strip())
    return cells


_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def parse_table(text: str, source: str = "") -> MdTable:
    lines = text.splitlines()
    start = None
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith("|") and _SEPARATOR.match(lines[i + 1]):
            start = i
            break
    if start is None:
        return MdTable(columns=[], rows=[], head=text.rstrip("\n"))
    columns = _split_row(lines[start])
    align = {}
    for col, spec in zip(columns, _split_row(lines[start + 1])):
        spec = spec.strip()
        if spec.endswith(":") and not spec.startswith(":"):
            align[col] = "right"
    rows, end = [], start + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        cells = _split_row(lines[end])
        if len(cells) != len(columns):
            raise FormatError(f"{source}:{end + 1}: {len(cells)} Spalten statt {len(columns)}")
        rows.append(dict(zip(columns, cells)))
        end += 1
    return MdTable(columns=columns, rows=rows,
                   head="\n".join(lines[:start]).rstrip("\n"),
                   tail="\n".join(lines[end:]).strip("\n"), align=align)


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_table(table: MdTable) -> str:
    cols = table.columns
    data = [[_cell(r.get(c, "")) for c in cols] for r in table.rows]
    widths = [max([len(c)] + [len(row[i]) for row in data]) for i, c in enumerate(cols)]

    def fmt(cells):
        out = []
        for i, (c, cell) in enumerate(zip(cols, cells)):
            out.append(cell.rjust(widths[i]) if table.align.get(c) == "right" else cell.ljust(widths[i]))
        return "| " + " | ".join(out) + " |"

    sep = "| " + " | ".join(("-" * (widths[i] - 1) + ":") if table.align.get(c) == "right"
                            else "-" * widths[i] for i, c in enumerate(cols)) + " |"
    parts = []
    if table.head:
        parts.append(table.head + "\n")
    parts.append("\n".join([fmt(cols), sep] + [fmt(r) for r in data]))
    if table.tail:
        parts.append("\n" + table.tail)
    return "\n".join(parts) + "\n"


def read_table(path: Path) -> MdTable:
    return parse_table(path.read_text(encoding="utf-8"), str(path))


def write_table(path: Path, table: MdTable) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_table(table), encoding="utf-8")


# ---------- Values ----------

CENT = Decimal("0.01")


def parse_amount(raw, where: str = "") -> Decimal:
    """'1'234.50', '1234.5', 1234.5 → Decimal('1234.50'). Swiss apostrophes allowed."""
    if isinstance(raw, Decimal):
        return raw
    text = str(raw if raw is not None else "").strip().replace("'", "").replace("’", "").replace(" ", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise FormatError(f"{where}: '{raw}' ist kein Betrag") from None


def parse_date(raw, where: str = "") -> date:
    if isinstance(raw, date):
        return raw
    text = str(raw or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        raise FormatError(f"{where}: '{raw}' ist kein Datum (JJJJ-MM-TT)") from None


def fmt_amount(value: Decimal) -> str:
    """Machine format for files: 1234.50 (no grouping, always two decimals)."""
    return f"{Decimal(value).quantize(CENT):.2f}"


def chf(value) -> str:
    """Human format: 1'234.50."""
    return f"{Decimal(value).quantize(CENT):,.2f}".replace(",", "'")


def slug(text: str) -> str:
    text = (text or "").strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("é", "e"), ("è", "e"), ("à", "a"), ("ç", "c")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "x"

"""Plain-text storage: YAML files, Markdown with YAML frontmatter, Markdown tables.

Everything a book holds is one of these three shapes, so a person in an editor,
an LLM in a harness and this engine all read the same files. The writers here
are deterministic — the same data always produces the same bytes — which keeps
git diffs small and makes the lock hashes in `check.py` stable.
"""
from __future__ import annotations

import re
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml


class FormatError(ValueError):
    """A file that does not have the shape aeradex expects."""


# ---------- YAML ----------

class _Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Reject ambiguous mappings and construct decimal scalars without floats."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:
                raise yaml.constructor.ConstructorError(None, None, "ungültiger Schlüssel", key_node.start_mark)
            if duplicate:
                raise yaml.constructor.ConstructorError(None, None, f"doppelter Schlüssel: {key}", key_node.start_mark)
        return super().construct_mapping(node, deep=deep)


def _decimal_constructor(loader, node):
    value = loader.construct_scalar(node).replace("_", "")
    try:
        if ":" in value:
            raise InvalidOperation
        number = Decimal(value)
        if not number.is_finite():
            raise InvalidOperation
        return number
    except InvalidOperation:
        raise yaml.constructor.ConstructorError(None, None, f"ungültige Dezimalzahl: {value}", node.start_mark)


_Loader.add_constructor("tag:yaml.org,2002:float", _decimal_constructor)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Atomically replace one file, syncing its content and directory entries."""
    path = Path(path)
    missing, parent = [], path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        fsync_directory(directory.parent)
    if path.is_symlink():
        raise FormatError(f"{path}: Schreiben durch einen symbolischen Link ist nicht erlaubt")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".aeradex-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write(path, text.encode("utf-8"))


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
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
        return {} if data is None else data
    except yaml.YAMLError as exc:
        raise FormatError(f"{path}: kein gültiges YAML ({exc})") from exc


def write_yaml(path: Path, data) -> None:
    atomic_write_text(path, dump_yaml(data))


# ---------- Markdown with frontmatter ----------

_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def read_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = _FRONT.match(text)
    if not match:
        raise FormatError(f"{path}: Frontmatter (--- … ---) fehlt")
    try:
        meta = yaml.load(match.group(1), Loader=_Loader)
        meta = {} if meta is None else meta
    except yaml.YAMLError as exc:
        raise FormatError(f"{path}: Frontmatter ist kein gültiges YAML ({exc})") from exc
    if not isinstance(meta, dict):
        raise FormatError(f"{path}: Frontmatter muss eine Zuordnung (key: value) sein")
    return meta, match.group(2)


def write_frontmatter(path: Path, meta: dict, body: str = "") -> None:
    body = body.strip("\n")
    atomic_write_text(path, "---\n" + dump_yaml(meta) + "---\n" + (f"\n{body}\n" if body else ""))


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
    if any(not c for c in columns) or len(set(columns)) != len(columns):
        raise FormatError(f"{source}:{start + 1}: leere oder doppelte Spaltennamen")
    specs = _split_row(lines[start + 1])
    if len(specs) != len(columns):
        raise FormatError(f"{source}:{start + 2}: Trennzeile hat {len(specs)} statt {len(columns)} Spalten")
    align = {}
    for col, spec in zip(columns, specs):
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
    widths = [max(3, len(c)) for c in cols]  # "---" minimum

    def fmt(cells):
        out = []
        for i, (c, cell) in enumerate(zip(cols, cells)):
            out.append(cell.rjust(widths[i]) if table.align.get(c) == "right" else cell.ljust(widths[i]))
        return "| " + " | ".join(out) + " |"

    sep = "| " + " | ".join(("-" * max(3, widths[i] - 1) + ":") if table.align.get(c) == "right"
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
    atomic_write_text(path, render_table(table))


# ---------- Values ----------

CENT = Decimal("0.01")


def parse_amount(raw, where: str = "") -> Decimal:
    """'1'234.50', '1234.5', 1234.5 → Decimal('1234.50'). Swiss apostrophes allowed."""
    text = str(raw if raw is not None else "").strip().replace("'", "").replace("’", "").replace(" ", "")
    if not text:
        return Decimal("0")
    try:
        amount = raw if isinstance(raw, Decimal) else Decimal(text)
        if not amount.is_finite():
            raise InvalidOperation
        return amount
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

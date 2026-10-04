"""A book (one Mandant) as typed objects, loaded from and written to its folder.

Layout of a book folder:

    batzen.yaml               company, bank, system accounts, posting lock
    kontenplan.yaml           chart of accounts
    journal/<JJJJ>/<JJJJ-MM>.md   the journal, one Markdown table per month
    vorschlaege.md            bookings proposed by an agent, awaiting approval
    belege/<JJJJ>/            receipts, file name starts with the Beleg number
    inbox/                    unprocessed receipts and statements
    kunden/K0001-<name>.md    customers
    rechnungen/<JJJJ>/R-….md  issued invoices (frozen)
    personal/M0001-<name>.md  employees
    lohn/einstellungen.yaml   payroll rates and accounts
    lohn/<JJJJ>/<MM>/M0001.md payslips
    abschluss/<JJJJ>/         Anhang, Gewinnverwendung
    .batzen/locks.yaml        hashes of the locked period

Nothing here does arithmetic beyond parsing; balances live in `ledger.py`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .files import (FormatError, MdTable, fmt_amount, parse_amount, parse_date,
                    read_frontmatter, read_table, read_yaml, write_table, write_yaml)

ZERO = Decimal("0")

JOURNAL_COLUMNS = ["Datum", "Beleg", "Text", "Soll", "Haben", "Betrag", "FW", "Kurs", "MWST", "Quelle"]
PROPOSAL_COLUMNS = ["ID", "Datum", "Beleg", "Text", "Soll", "Haben", "Betrag", "MWST", "Begründung", "Datei", "Bank", "FW", "Kurs"]

KLASSEN = ("aktiv", "passiv", "aufwand", "ertrag")

MONTHS_DE = ["", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
             "August", "September", "Oktober", "November", "Dezember"]

# System accounts a book relies on; batzen.yaml may point them elsewhere.
DEFAULT_SYSTEM_ACCOUNTS = {
    "debitoren": "1100",
    "ertrag": "3400",
    "gewinnvortrag": "2970",
    "jahresergebnis": "2979",
    "dividende": "2261",
    "reserve": "2950",
    "gutschrift": "3400",
    "bank": "1020",
    "kreditoren": "2000",
    "kursgewinn": "6952",
    "kursverlust": "6942",
    "verrechnungssteuer": "2206",
}


class BookError(Exception):
    """Something the user asked for that the book's rules refuse."""


@dataclass
class Account:
    nr: str
    name: str
    klasse: str                    # aktiv | passiv | aufwand | ertrag
    gruppe: str = ""               # statement group code (see statements.GROUPS)
    eroeffnung: Decimal = ZERO     # signed opening balance of the first year
    vorjahr: Decimal = ZERO        # signed prior-year figure for the first year
    gruppe_negativ: str = ""       # presentation-only reclassification when the sign flips
    aktiv_: bool = True
    waehrung: str = "CHF"          # account currency; foreign-currency rows carry FW and Kurs
    eroeffnung_fw: Decimal = ZERO  # signed opening balance in the account currency (first year)

    @property
    def is_foreign(self) -> bool:
        return self.waehrung != "CHF"

    @property
    def is_balance_sheet(self) -> bool:
        return self.klasse in ("aktiv", "passiv")

    @property
    def is_pl(self) -> bool:
        return self.klasse in ("aufwand", "ertrag")


@dataclass
class Row:
    """One journal line. Either account may be empty on a split booking; the
    Beleg as a whole must then balance."""
    datum: date
    beleg: str
    text: str
    soll: str
    haben: str
    betrag: Decimal
    quelle: str = ""
    file: str = ""        # relative path of the month file it came from
    line: int = 0         # 1-based row index within that file's table
    mwst: str = ""        # MWST code (see mwst.py), empty = no VAT relevance
    waehrung: str = ""    # foreign currency of the row ("EUR"), empty = CHF only
    fw: Decimal | None = None    # amount in that currency (positive)
    kurs: Decimal | None = None  # CHF per 1 unit used for Betrag

    def as_cells(self) -> dict[str, str]:
        return {"Datum": self.datum.isoformat(), "Beleg": self.beleg, "Text": self.text,
                "Soll": self.soll, "Haben": self.haben, "Betrag": fmt_amount(self.betrag),
                "FW": f"{self.waehrung} {self.fw:.2f}" if self.waehrung and self.fw is not None else "",
                "Kurs": format(self.kurs.normalize(), "f") if self.kurs is not None else "",
                "MWST": self.mwst, "Quelle": self.quelle}

    @property
    def where(self) -> str:
        return f"{self.file} Zeile {self.line}" if self.file else self.beleg


@dataclass
class Settings:
    firma: str
    data: dict = field(default_factory=dict)

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def erstes_jahr(self) -> int:
        return int(self.data.get("erstes_jahr") or date.today().year)

    @property
    def sperre_bis(self) -> date | None:
        raw = self.data.get("sperre_bis")
        return parse_date(raw, "batzen.yaml sperre_bis") if raw else None

    def konto(self, role: str) -> str:
        return str((self.data.get("konten") or {}).get(role) or DEFAULT_SYSTEM_ACCOUNTS[role])

    @property
    def adresse(self) -> dict:
        return self.data.get("adresse") or {}

    @property
    def address_lines(self) -> list[str]:
        a = self.adresse
        lines = []
        street = " ".join(str(p) for p in (a.get("strasse"), a.get("nr")) if p)
        if street:
            lines.append(street)
        town = " ".join(str(p) for p in (a.get("plz"), a.get("ort")) if p)
        if town:
            lines.append(town)
        if a.get("land") and a.get("land") != "CH":
            lines.append(str(a["land"]))
        return lines


def find_root(start: Path | None = None) -> Path:
    """The book folder: $BATZEN_BUCH, else the nearest folder upward with batzen.yaml."""
    env = os.environ.get("BATZEN_BUCH")
    if env and start is None:
        start = Path(env)
    here = (start or Path.cwd()).resolve()
    for folder in [here, *here.parents]:
        if (folder / "batzen.yaml").exists():
            return folder
    raise BookError(f"Kein Buch gefunden (batzen.yaml) in {here} oder darüber. "
                    "Mit `batzen init <ordner>` anlegen oder --buch angeben.")


class Book:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not (self.root / "batzen.yaml").exists():
            raise BookError(f"{self.root}: keine batzen.yaml — ist das ein batzen-Buch?")
        self._settings: Settings | None = None
        self._accounts: dict[str, Account] | None = None
        self._rows: list[Row] | None = None

    # ---- settings ----
    @property
    def settings(self) -> Settings:
        if self._settings is None:
            data = read_yaml(self.root / "batzen.yaml")
            self._settings = Settings(firma=str(data.get("firma") or ""), data=data)
        return self._settings

    def save_settings(self) -> None:
        write_yaml(self.root / "batzen.yaml", self.settings.data)

    # ---- chart of accounts ----
    @property
    def accounts(self) -> dict[str, Account]:
        if self._accounts is None:
            self._accounts = load_accounts(self.root / "kontenplan.yaml")
        return self._accounts

    def account(self, nr: str) -> Account:
        acct = self.accounts.get(str(nr))
        if acct is None:
            raise BookError(f"Konto {nr} existiert nicht im Kontenplan")
        return acct

    def save_accounts(self) -> None:
        save_accounts(self.root / "kontenplan.yaml", self.accounts)

    # ---- journal ----
    def journal_files(self) -> list[Path]:
        return sorted((self.root / "journal").glob("*/*.md"))

    def month_file(self, d: date) -> Path:
        return self.root / "journal" / f"{d.year}" / f"{d.year}-{d.month:02d}.md"

    @property
    def rows(self) -> list[Row]:
        if self._rows is None:
            rows = []
            for path in self.journal_files():
                rows.extend(load_journal_file(path, self.root))
            self._rows = rows
        return self._rows

    def rows_in(self, start: date | None = None, end: date | None = None) -> list[Row]:
        return [r for r in self.rows
                if (start is None or r.datum >= start) and (end is None or r.datum <= end)]

    def years(self) -> list[int]:
        found = {r.datum.year for r in self.rows}
        found.add(self.settings.erstes_jahr)
        first = self.settings.erstes_jahr
        return list(range(first, max(found) + 1))

    def add_rows(self, rows: list[Row]) -> list[Path]:
        """Insert rows into their month files, keeping each file in date order.
        A new row lands after every existing row of the same date, so the order
        of entry within a day is kept."""
        touched: dict[Path, MdTable] = {}
        for row in rows:
            path = self.month_file(row.datum)
            table = touched.get(path) or _open_month(path, row.datum)
            cells = row.as_cells()
            pos = len(table.rows)
            for i, existing in enumerate(table.rows):
                if existing["Datum"] > cells["Datum"]:
                    pos = i
                    break
            table.rows.insert(pos, cells)
            touched[path] = table
        for path, table in touched.items():
            write_table(path, table)
        self._rows = None
        return list(touched)

    def remove_rows(self, predicate) -> list[Path]:
        """Drop every journal row for which predicate(row) is true."""
        changed = []
        for path in self.journal_files():
            table = read_table(path)
            rel = str(path.relative_to(self.root))
            keep = []
            for i, cells in enumerate(table.rows, 1):
                row = _row_from_cells(cells, rel, i)
                if not predicate(row):
                    keep.append(cells)
            if len(keep) != len(table.rows):
                table.rows = keep
                write_table(path, table)
                changed.append(path)
        self._rows = None
        return changed

    def reload(self) -> None:
        self._settings = self._accounts = self._rows = None

    def rel(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.root))


def _open_month(path: Path, d: date) -> MdTable:
    if path.exists():
        table = read_table(path)
        if table.columns:
            for i, col in enumerate(JOURNAL_COLUMNS):
                if col not in table.columns:       # older files: insert at the canonical place
                    table.columns.insert(min(i, len(table.columns)), col)
            table.align["Betrag"] = "right"
            return table
    return MdTable(columns=list(JOURNAL_COLUMNS), rows=[],
                   head=f"# Journal {MONTHS_DE[d.month]} {d.year}", align={"Betrag": "right"})


def _row_from_cells(cells: dict, rel: str, line: int) -> Row:
    where = f"{rel} Zeile {line}"
    return Row(
        datum=parse_date(cells.get("Datum"), where),
        beleg=cells.get("Beleg", "").strip(),
        text=cells.get("Text", "").strip(),
        soll=cells.get("Soll", "").strip(),
        haben=cells.get("Haben", "").strip(),
        betrag=parse_amount(cells.get("Betrag"), where),
        quelle=cells.get("Quelle", "").strip(),
        file=rel, line=line,
        mwst=cells.get("MWST", "").strip().upper(),
        **_fw_from_cells(cells, where),
    )


def _fw_from_cells(cells: dict, where: str) -> dict:
    raw = (cells.get("FW") or "").strip()
    if not raw:
        return {}
    parts = raw.split()
    if len(parts) != 2 or len(parts[0]) != 3 or not parts[0].isalpha():
        raise FormatError(f"{where}: FW '{raw}' — erwartet z.B. 'EUR 100.00'")
    kurs_raw = (cells.get("Kurs") or "").strip()
    return {"waehrung": parts[0].upper(), "fw": parse_amount(parts[1], where),
            "kurs": parse_amount(kurs_raw, where) if kurs_raw else None}


def load_journal_file(path: Path, root: Path) -> list[Row]:
    table = read_table(path)
    rel = str(path.relative_to(root))
    if not table.columns:
        return []
    missing = [c for c in ("Datum", "Beleg", "Soll", "Haben", "Betrag") if c not in table.columns]
    if missing:
        raise FormatError(f"{rel}: Spalte(n) fehlen: {', '.join(missing)}")
    return [_row_from_cells(cells, rel, i) for i, cells in enumerate(table.rows, 1)]


def load_accounts(path: Path) -> dict[str, Account]:
    raw = read_yaml(path)
    items = raw.get("konten", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise FormatError(f"{path}: erwartet eine Liste 'konten:'")
    from .statements import default_group   # late import: statements imports book

    accounts: dict[str, Account] = {}
    for i, item in enumerate(items, 1):
        where = f"{path.name} Eintrag {i}"
        nr = str(item.get("nr", "")).strip()
        if not nr:
            raise FormatError(f"{where}: 'nr' fehlt")
        if nr in accounts:
            raise FormatError(f"{where}: Konto {nr} doppelt")
        klasse = str(item.get("klasse") or "").strip().lower() or _klasse_for(nr)
        if klasse not in KLASSEN:
            raise FormatError(f"{where}: klasse '{klasse}' — erlaubt: {', '.join(KLASSEN)}")
        accounts[nr] = Account(
            nr=nr, name=str(item.get("name") or ""), klasse=klasse,
            gruppe=str(item.get("gruppe") or default_group(nr, klasse)),
            eroeffnung=parse_amount(item.get("eroeffnung", 0), where),
            vorjahr=parse_amount(item.get("vorjahr", 0), where),
            gruppe_negativ=str(item.get("gruppe_negativ") or ""),
            aktiv_=bool(item.get("aktiv", True)),
            waehrung=str(item.get("waehrung") or "CHF").upper(),
            eroeffnung_fw=parse_amount(item.get("eroeffnung_fw", 0), where),
        )
    return accounts


def save_accounts(path: Path, accounts: dict[str, Account]) -> None:
    from .statements import default_group

    out = []
    for a in sorted(accounts.values(), key=lambda a: a.nr):
        item = {"nr": a.nr, "name": a.name, "klasse": a.klasse}
        if a.gruppe != default_group(a.nr, a.klasse):
            item["gruppe"] = a.gruppe
        if a.eroeffnung:
            item["eroeffnung"] = a.eroeffnung
        if a.vorjahr:
            item["vorjahr"] = a.vorjahr
        if a.gruppe_negativ:
            item["gruppe_negativ"] = a.gruppe_negativ
        if not a.aktiv_:
            item["aktiv"] = False
        if a.waehrung != "CHF":
            item["waehrung"] = a.waehrung
        if a.eroeffnung_fw:
            item["eroeffnung_fw"] = a.eroeffnung_fw
        out.append(item)
    write_yaml(path, {"konten": out})


def _klasse_for(nr: str) -> str:
    first = nr[:1]
    return {"1": "aktiv", "2": "passiv", "3": "ertrag"}.get(first, "aufwand")


def load_md_records(folder: Path) -> list[tuple[Path, dict, str]]:
    if not folder.exists():
        return []
    return [(p, *read_frontmatter(p)) for p in sorted(folder.rglob("*.md"))]

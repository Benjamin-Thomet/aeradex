"""Sample plugin used by the tests: one of every hook.

Documents: spenden/<nr>.yaml (a donation), owning the journal row `Quelle spende:<nr>`.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from allkvitt import api, journal
from allkvitt.book import Row
from allkvitt.files import parse_date, read_yaml, write_yaml
from allkvitt.plugins import BankFormat, Command, Finding, Source, hookimpl

ALLKVITT_PLUGIN_API = 1
HERE = Path(__file__).parent


def _docs(book):
    folder = book.root / "spenden"
    return {p.stem: read_yaml(p) for p in sorted(folder.glob("*.yaml"))} if folder.exists() else {}


def _rows(book):
    return {f"spende:{nr}": [Row(parse_date(d["datum"]), f"SP-{nr}", f"Spende {d['von']}", "1020", "3600",
                                 Decimal(str(d["betrag"])), f"spende:{nr}")]
            for nr, d in _docs(book).items()}


def record(book, von: str, betrag: str, datum: str):
    """Write the document and its rows; called through api.write."""
    nr = f"{len(_docs(book)) + 1:04d}"
    path = book.root / "spenden" / f"{nr}.yaml"
    write_yaml(path, {"von": von, "betrag": betrag, "datum": datum})
    touched = journal.post(book, _rows(book)[f"spende:{nr}"])
    return nr, touched + [path]


@hookimpl
def allkvitt_sources():
    return [Source("spende", "Spende", _rows, link=lambda ref: f"/spenden/{ref}")]


@hookimpl
def allkvitt_check(book, rows):
    big = [r for r in rows if r.quelle.startswith("spende:") and r.betrag > 10000]
    return [Finding("hinweis", r.where, "Grossspende: Spendenbestätigung prüfen") for r in big]


def _detect(name, data):
    return name.endswith(".csv") and data.startswith(b"Datum;Betrag;Text")


def _parse(data, book):
    lines = data.decode().strip().splitlines()[1:]
    entries = []
    for i, line in enumerate(lines, 1):
        datum, betrag, text = line.split(";")
        entries.append({"datum": parse_date(datum), "betrag": Decimal(betrag), "gegenpartei": "", "referenz_typ": "",
                        "referenz": "", "endtoend": "", "text": text, "bankref": f"x{i}", "position": str(i)})
    return [{"id": "CSV-" + lines[0].split(";")[0], "konto": "1020", "von": "", "bis": entries[-1]["datum"].isoformat(),
             "eroeffnung": None, "schluss": None, "schluss_datum": None, "buchungen": entries}]


@hookimpl
def allkvitt_bank_formats():
    return [BankFormat("testcsv", "Test-CSV", (".csv",), _detect, _parse)]


@hookimpl
def allkvitt_kontenplaene():
    return {"testplan": HERE / "testplan.yaml"}


def spende_erfassen(von: str, betrag: str, datum: str) -> dict:
    """Spende erfassen.

    Args:
        von: Name der spendenden Person.
        betrag: Betrag in CHF.
        datum: JJJJ-MM-TT.
    """
    from allkvitt.tools import call
    return call(api.write, f"Spende von {von}", record, von, betrag, datum)


@hookimpl
def allkvitt_tools():
    return [spende_erfassen]


@hookimpl
def allkvitt_instructions():
    return "- Spenden: spende_erfassen (nie als freie Buchung)."


def _setup(parser):
    parser.add_argument("von")
    parser.add_argument("betrag")
    parser.add_argument("--datum", required=True)


@hookimpl
def allkvitt_commands():
    return [Command("spende", "Spende erfassen", lambda book, a: api.write(book, f"Spende von {a.von}", record,
                                                                          a.von, a.betrag, a.datum), _setup)]

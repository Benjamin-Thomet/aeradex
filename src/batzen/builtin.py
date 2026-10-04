"""What batzen brings itself, offered through the same hooks as any plugin.

The camt.053 import is the reference implementation of a bank format: a
plugin for another bank works exactly the same way.
"""
from __future__ import annotations

from pathlib import Path

from .plugins import BankFormat, hookimpl

BATZEN_PLUGIN_API = 1
DATA = Path(__file__).parent / "data"


@hookimpl
def batzen_kontenplaene():
    return {p.stem: p for p in sorted((DATA / "kontenplaene").glob("*.yaml"))}


@hookimpl
def batzen_qst_tarife():
    return DATA / "qst_tarife"


def _is_camt(filename: str, data: bytes) -> bool:
    head = data[:4096]
    return b"<" in head[:200] and (b"camt.053" in head or b"BkToCstmrStmt" in data[:20000])


def _parse_camt(data: bytes, book):
    from . import bank
    return bank.parse(data)


@hookimpl
def batzen_bank_formats():
    return [BankFormat(name="camt053", label="ISO 20022 camt.053 (XML)", suffixes=(".xml",),
                       detect=_is_camt, parse=_parse_camt)]

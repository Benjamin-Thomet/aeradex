"""Kontenplan für Schweizer Vereine (Mitgliederbeiträge, Spenden, Fonds, Vereinsvermögen)."""
from pathlib import Path

from batzen.plugins import hookimpl

BATZEN_PLUGIN_API = 1
__version__ = "0.1.0"


@hookimpl
def batzen_kontenplaene():
    return {"verein": Path(__file__).parent / "verein.yaml"}

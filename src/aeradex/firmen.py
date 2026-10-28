"""The companies (books) a person works with on this computer: a list of book folders outside every book,
in `<config>/firmen.yaml` (config = $AERADEX_CONFIG or ~/.config/aeradex). `aeradex ui` starts with this list
when it is not opened inside a book, and switches between them. Removing a company from the list never
touches its folder. Server mode (`aeradex serve`) has one book and does not use the list."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .book import Book, BookError
from .files import read_yaml, slug, write_yaml


def config_dir() -> Path:
    return Path(os.environ.get("AERADEX_CONFIG") or Path.home() / ".config" / "aeradex")


def _path() -> Path:
    return config_dir() / "firmen.yaml"


def _load() -> list[dict]:
    path = _path()
    data = read_yaml(path) if path.exists() else None
    return [e for e in (data or {}).get("firmen") or [] if isinstance(e, dict) and e.get("ordner")]


def _save(entries: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, {"firmen": entries})


def base_folder() -> Path:
    """Where new books go unless the person names a folder ($AERADEX_FIRMEN_ORDNER or ~/aeradex)."""
    return Path(os.environ.get("AERADEX_FIRMEN_ORDNER") or Path.home() / "aeradex")


def default_folder(firma: str) -> Path:
    """The folder for a new company's book: <base>/<name>, numbered if taken."""
    base = base_folder()
    name = slug(firma)
    target, n = base / name, 2
    while target.exists() and any(target.iterdir()):
        target, n = base / f"{name}-{n}", n + 1
    return target


def liste() -> list[dict]:
    """The known companies, last opened first, each with what its book says (firma, ort, rechtsform)."""
    out = []
    for e in _load():
        root = Path(e["ordner"])
        item = {"ordner": str(root), "zuletzt": e.get("zuletzt") or "", "firma": root.name, "ort": "",
                "rechtsform": "", "fehlt": False}
        try:
            s = Book(root).settings
            adresse = s.get("adresse") or {}
            item.update(firma=s.firma, ort=str(adresse.get("ort") or "") if isinstance(adresse, dict) else "",
                        rechtsform=str(s.get("rechtsform") or ""))
        except (BookError, OSError, ValueError):
            item["fehlt"] = True
        out.append(item)
    return out


def merken(root: Path) -> None:
    """Add a book to the list (or mark it as just opened): the list keeps the last opened first."""
    root = Path(root).resolve()
    Book(root)        # refuses a folder without aeradex.yaml
    entries = [e for e in _load() if Path(e["ordner"]).resolve() != root]
    _save([{"ordner": str(root), "zuletzt": datetime.now().isoformat(timespec="seconds")}, *entries])


def entfernen(root: Path) -> None:
    """Take a book off the list; its folder stays as it is."""
    root = Path(root).resolve()
    entries = _load()
    rest = [e for e in entries if Path(e["ordner"]).resolve() != root]
    if len(rest) == len(entries):
        raise BookError(f"{root} ist nicht in der Firmenliste")
    _save(rest)


def bekannt(root: Path) -> bool:
    root = Path(root).resolve()
    return any(Path(e["ordner"]).resolve() == root for e in _load())

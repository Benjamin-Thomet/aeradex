"""Explicit, backwards-compatible book format versions."""
from .files import FormatError, atomic_write_text, read_yaml, write_yaml

CURRENT_VERSION = 1


def validate_version(data):
    if not isinstance(data, dict):
        raise FormatError("aeradex.yaml: erwartet eine Zuordnung")
    version = data.get("format_version", 0)
    if type(version) is not int or version < 0 or version > CURRENT_VERSION:
        raise FormatError(f"aeradex.yaml: nicht unterstützte format_version {version!r} (aktuell: {CURRENT_VERSION})")
    if "firma" in data and not isinstance(data["firma"], str):
        raise FormatError("aeradex.yaml: firma muss Text sein")
    for key in ("konten", "adresse", "mwst", "bankkonten"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise FormatError(f"aeradex.yaml: {key} muss eine Zuordnung sein")
    if "plugins" in data and (not isinstance(data["plugins"], list)
                              or any(not isinstance(p, str) for p in data["plugins"])):
        raise FormatError("aeradex.yaml: plugins muss eine Liste von Namen sein")
    if data.get("erstes_jahr") is not None:
        try:
            year = data["erstes_jahr"]
            if isinstance(year, bool) or str(int(year)) != str(year) or not 1 <= int(year) <= 9999:
                raise ValueError
        except (TypeError, ValueError):
            raise FormatError("aeradex.yaml: erstes_jahr muss eine gültige Jahreszahl sein") from None
    return version


def migrate(book):
    """Version 0 has the same data model; version 1 declares the format explicitly.

    Run only inside a write transaction. Reading a legacy book never rewrites it.
    """
    path = book.root / "aeradex.yaml"
    data = read_yaml(path)
    if validate_version(data) == 0:
        data["format_version"] = CURRENT_VERSION
        write_yaml(path, data)
        book.reload()
    ignore = book.root / ".gitignore"
    text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    missing = [p for p in (".aeradex/transaction/", "**/.aeradex-tmp-*") if p not in text.splitlines()]
    if missing:
        atomic_write_text(ignore, text.rstrip("\n") + "\n" + "\n".join(missing) + "\n")

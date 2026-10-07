"""Plugin marketplace: a catalog of plugins, reviewed by a maintainer, installable from the UI.

The catalog is a JSON file. Until there is a public place for it, aeradex ships one
(`data/plugin_katalog.json`); `AERADEX_PLUGIN_KATALOG` points to another file or an
https URL (cached under ~/.cache/aeradex).

    {"version": 1, "plugins": [{
        "name": "revolut", "paket": "aeradex-revolut", "version": "0.1.0",
        "beschreibung": "…", "autor": "…", "lizenz": "AGPL-3.0-or-later", "quelle": "<repo URL>",
        "repo_pfad": "plugins/aeradex-revolut",          # in a aeradex checkout (development)
        "aeradex": ">=0.6", "art": "code" | "daten",
        "status": "geprüft" | "ungeprüft",
        "pruefung": {"von": "…", "am": "JJJJ-MM-TT", "version": "0.1.0", "sha256": "…"}}]}

A review (`aeradex plugins pruefen NAME`) records a SHA-256 over the plugin's source
files. aeradex recomputes it for the installed plugin: «geprüft» only holds while the
code is exactly what was reviewed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

from .book import Book, BookError

BUNDLED = Path(__file__).parent / "data" / "plugin_katalog.json"
RECHTE = {
    "kontenplaene": "Kontenplan-Vorlagen", "qst_tarife": "Quellensteuer-Tabellen",
    "bank_formats": "liest Kontoauszüge ein", "beleg_leser": "liest Belege aus",
    "sources": "bucht eigene Dokumente ins Journal", "check": "eigene Prüfregeln",
    "tools": "Werkzeuge für Agenten", "instructions": "Hinweise an Agenten",
    "commands": "Befehle in der Kommandozeile", "pages": "eigene Seiten in der Oberfläche",
}
DATA_ONLY = {"kontenplaene", "qst_tarife"}
SOURCE_SUFFIXES = {".py", ".html", ".yaml", ".yml", ".json", ".csv", ".txt", ".md", ".js", ".css"}


def source() -> str:
    return os.environ.get("AERADEX_PLUGIN_KATALOG") or str(BUNDLED)


def load(fetch=None) -> dict:
    src = source()
    if src.startswith("https://"):
        cache = Path.home() / ".cache" / "aeradex" / "plugin_katalog.json"
        try:
            data = (fetch or _get)(src)
            json.loads(data)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        except Exception:
            if not cache.exists():
                raise BookError(f"Plugin-Katalog nicht erreichbar: {src}") from None
            data = cache.read_bytes()
        return json.loads(data)
    if src.startswith("http://"):
        raise BookError("Der Plugin-Katalog muss über https geladen werden")
    path = Path(src)
    if not path.exists():
        return {"version": 1, "plugins": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read()


def tree_hash(folder: Path) -> str:
    """SHA-256 over a plugin package's source files (paths and contents), independent of caches."""
    h = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES
                       and "__pycache__" not in p.parts):
        h.update(str(path.relative_to(folder)).encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def package_folder(name: str) -> Path | None:
    from . import plugins
    plugin = plugins.installed().get(name)
    if plugin is None or not plugin.ok or not getattr(plugin.module, "__file__", None):
        return None
    return Path(plugin.module.__file__).parent


def repo_root() -> Path | None:
    """The aeradex checkout this package runs from (development), if any."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "plugins").is_dir() and (root / "pyproject.toml").exists() else None


def entries(book: Book | None = None) -> list[dict]:
    """The catalog merged with what is installed here: status, whether the code is the reviewed code."""
    from . import plugins
    installed = plugins.installed()
    enabled = set(plugins.enabled_names(book))
    out = []
    for e in load().get("plugins") or []:
        name = e["name"]
        here = installed.get(name)
        folder = package_folder(name)
        review = e.get("pruefung") or {}
        reviewed = e.get("status") == "geprüft" and review.get("sha256")
        same = bool(folder and reviewed and tree_hash(folder) == review["sha256"])
        hooks = e.get("hooks") or []
        out.append({**e, "installiert": here.version if here else "", "eingeschaltet": name in enabled,
                    "rechte": [RECHTE.get(h, h) for h in hooks if h in RECHTE],
                    "nur_daten": bool(hooks) and set(hooks) <= DATA_ONLY,
                    "geprueft": bool(reviewed),
                    "code": ("unverändert" if same else "verändert") if (folder and reviewed) else "",
                    "befehl": " ".join(install_args(e, display=True))})
    return out


def entry(name: str) -> dict:
    found = next((e for e in load().get("plugins") or [] if e["name"] == name), None)
    if found is None:
        raise BookError(f"Plugin '{name}' steht nicht im Katalog")
    return found


def install_args(e: dict, display: bool = False) -> list[str]:
    """pip arguments: the plugin from this checkout (development), else the reviewed version from the index."""
    root = repo_root()
    if root and e.get("repo_pfad") and (root / e["repo_pfad"]).is_dir():
        target = ["-e", str(root / e["repo_pfad"])]
    else:
        target = [f"{e['paket']}=={e['version']}" if e.get("version") else e["paket"]]
    head = ["pip", "install"] if display else [sys.executable, "-m", "pip", "install"]
    return head + target


def install(name: str, ungeprueft_ok: bool = False) -> str:
    """Install a catalog plugin into this Python environment (needs a restart to load)."""
    e = entry(name)
    if e.get("status") != "geprüft" and not ungeprueft_ok:
        raise BookError(f"Plugin '{name}' ist nicht geprüft — nur mit ausdrücklicher Bestätigung installieren")
    proc = subprocess.run(install_args(e), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise BookError(f"Installation fehlgeschlagen: {(proc.stderr or proc.stdout)[-600:]}")
    from . import plugins
    plugins._reset()
    return (proc.stdout or "")[-400:]


def review(name: str, von: str, katalog: Path | None = None, tests: bool = True) -> dict:
    """Maintainer: record that the installed code of `name` was reviewed (and its tests pass)."""
    from . import plugins
    path = Path(katalog or source())
    if str(path).startswith("http"):
        raise BookError("Prüfungen werden in der Katalog-Datei festgehalten, nicht über eine URL")
    folder = package_folder(name)
    if folder is None:
        raise BookError(f"Plugin '{name}' ist hier nicht installiert — zuerst installieren, dann prüfen")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "plugins": []}
    e = next((x for x in data["plugins"] if x["name"] == name), None)
    if e is None:
        raise BookError(f"Plugin '{name}' steht nicht im Katalog {path}")
    if tests:
        root = repo_root()
        test_dir = (root / e["repo_pfad"] / "tests") if root and e.get("repo_pfad") else None
        if test_dir and test_dir.is_dir():
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_dir)],
                                  capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                raise BookError(f"Die Tests von '{name}' schlagen fehl:\n{proc.stdout[-800:]}")
    plugin = plugins.installed()[name]
    hooks = sorted(h[len("aeradex_"):] for h in dir(plugin.module) if h.startswith("aeradex_")
                   and callable(getattr(plugin.module, h)))
    e.update({"status": "geprüft", "version": plugin.version or e.get("version"), "hooks": hooks,
              "pruefung": {"von": von, "am": date.today().isoformat(), "version": plugin.version or e.get("version"),
                           "sha256": tree_hash(folder)}})
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return e


def revoke(name: str, katalog: Path | None = None) -> dict:
    path = Path(katalog or source())
    data = json.loads(path.read_text(encoding="utf-8"))
    e = next((x for x in data["plugins"] if x["name"] == name), None)
    if e is None:
        raise BookError(f"Plugin '{name}' steht nicht im Katalog")
    e["status"] = "ungeprüft"
    e.pop("pruefung", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return e

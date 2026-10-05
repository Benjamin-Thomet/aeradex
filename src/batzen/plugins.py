"""Plugins: extend batzen without forking it.

A plugin is an ordinary Python package that declares an entry point in the
group ``batzen.plugins``::

    [project.entry-points."batzen.plugins"]
    revolut = "batzen_revolut"

The module sets ``BATZEN_PLUGIN_API = 1`` and implements any of the hooks in
`Spec` with ``@hookimpl`` (pluggy, the plugin system of pytest).

Two rules keep a book trustworthy no matter which plugins run:

* **Installed is not enabled.** Behaviour (bank formats, owned journal rows,
  check rules, agent tools, commands) only runs for books that list the plugin
  in ``batzen.yaml → plugins``. Data (chart templates, withholding-tax tables)
  is offered by every installed plugin, because it is needed before a book exists.
* **Plugins write through the core.** They change files only inside
  ``api.write`` (guard → write → check → git commit), and rows they own
  (``Quelle <prefix>:…``) are regenerated and compared by ``batzen check``.
  A book that needs a plugin that is not installed fails ``check`` instead of
  silently losing those rows.

Books never carry plugin code: a cloned book cannot execute anything.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pluggy

from .book import Book, BookError, Row

PLUGIN_API = 1
GROUP = "batzen.plugins"

hookspec = pluggy.HookspecMarker("batzen")
hookimpl = pluggy.HookimplMarker("batzen")


# ---------- what a plugin can contribute ----------

@dataclass
class BankFormat:
    """A bank statement format for `batzen bank import`.

    ``parse(data, book)`` returns statements shaped like ``bank.parse`` (camt):
    ``{"id", "iban" or "konto", "von", "bis", "eroeffnung", "schluss", "schluss_datum",
    "buchungen": [{"datum", "betrag", "gegenpartei", "referenz_typ", "referenz",
    "endtoend", "text", "bankref", "position"}]}``. Amounts are Decimal in the
    account's currency, positive = money in.
    """
    name: str
    label: str
    suffixes: tuple[str, ...]
    detect: Callable[[str, bytes], bool]
    parse: Callable[[bytes, Book], list[dict]]


@dataclass
class BelegLeser:
    """Reads a supplier bill (PDF, photo) into fields for a Kreditoren draft.

    ``read(book, path, text_so_far)`` returns a dict with any of
    ``erfassung.FIELDS`` (strings) and optionally ``_text`` (the document's text,
    for later readers, Jev and the agent) — or None. Readers run by ascending
    ``prioritaet``; a field keeps the value of the first reader that found it.
    Built in: QR-bill (10), PDF text layer (50), Tesseract OCR (80).
    """
    name: str
    label: str
    prioritaet: int
    read: Callable[[Book, Path, str], dict | None]


@dataclass
class Source:
    """A kind of document that owns journal rows (``Quelle <prefix>:<ref>``).

    ``rows(book)`` returns every row the plugin's documents should have in the
    journal, keyed by their Quelle; ``check`` compares them with the journal.
    ``link(ref)`` optionally points the UI to the document.
    """
    prefix: str
    label: str
    rows: Callable[[Book], dict[str, list[Row]]]
    link: Callable[[str], str | None] | None = None


@dataclass
class Command:
    """A CLI command: ``batzen <name> …``. ``run(book, args)`` returns what the CLI prints."""
    name: str
    help: str
    run: Callable[[Book, argparse.Namespace], Any]
    setup: Callable[[argparse.ArgumentParser], None] = lambda parser: None
    needs_book: bool = True


@dataclass
class Page:
    """A page in the web UI: ``/p/<plugin>/<slug>``, listed in the sidebar under «Plugins».

    ``template`` is a Jinja file in the plugin package's ``templates/`` folder (it may
    ``{% extends "base.html" %}`` and use the core macros); ``context(book, query)``
    returns its variables. ``actions`` are form posts to ``/p/<plugin>/<slug>/<name>``:
    ``fn(book, form) -> dict`` — write through ``api.write`` so the book is checked and
    committed. A result may carry ``"weiter": "<url>"`` to go elsewhere afterwards.
    """
    slug: str
    label: str
    template: str
    context: Callable[[Book, dict], dict] = lambda book, query: {}
    actions: dict[str, Callable[[Book, dict], Any]] = field(default_factory=dict)


@dataclass
class Finding:
    """A `check` result from a plugin rule. level: fehler | warnung | hinweis."""
    level: str
    where: str
    message: str


class Spec:
    """The hooks (plugin API version 1). Implement any subset."""

    @hookspec
    def batzen_kontenplaene(self) -> dict[str, Path]:
        """Chart-of-accounts templates for `batzen init --kontenplan NAME` (data)."""

    @hookspec
    def batzen_qst_tarife(self) -> Path:
        """A folder with withholding-tax tables `<KANTON>-<JAHR>.json` (data)."""

    @hookspec
    def batzen_bank_formats(self) -> list[BankFormat]:
        """Statement formats for the bank import."""

    @hookspec
    def batzen_beleg_leser(self) -> list[BelegLeser]:
        """Readers for supplier bills (e.g. e-invoices: ZUGFeRD, XRechnung)."""

    @hookspec
    def batzen_sources(self) -> list[Source]:
        """Documents that own journal rows."""

    @hookspec
    def batzen_check(self, book: Book, rows: list[Row]) -> list[Finding]:
        """Extra rules for `batzen check`."""

    @hookspec
    def batzen_tools(self) -> list[Callable]:
        """Agent tools (MCP and chat panel); functions documented like those in `batzen.tools`."""

    @hookspec
    def batzen_instructions(self) -> str:
        """A short paragraph for the agent on when to use this plugin's tools."""

    @hookspec
    def batzen_commands(self) -> list[Command]:
        """CLI commands."""

    @hookspec
    def batzen_pages(self) -> list[Page]:
        """Pages in the web UI (templates in the plugin package's templates/ folder)."""


# ---------- discovery ----------

@dataclass
class Installed:
    name: str
    module: ModuleType | None
    version: str = ""
    beschreibung: str = ""
    api: int | None = None
    fehler: str = ""

    @property
    def ok(self) -> bool:
        return self.module is not None and not self.fehler


_EXTRA: dict[str, ModuleType] = {}       # registered without an entry point (tests, embedding)


def register(name: str, module: ModuleType) -> None:
    """Make a plugin available without installing a package (tests, embedding)."""
    _EXTRA[name] = module
    _reset()


def unregister(name: str) -> None:
    _EXTRA.pop(name, None)
    _reset()


def _reset() -> None:
    installed.cache_clear()
    _manager.cache_clear()


def _inspect(name: str, module: ModuleType | None, version: str = "", error: str = "") -> Installed:
    if module is None:
        return Installed(name, None, version, fehler=error)
    api = getattr(module, "BATZEN_PLUGIN_API", None)
    doc = (module.__doc__ or "").strip().splitlines()
    found = Installed(name, module, version or getattr(module, "__version__", ""), doc[0] if doc else "", api)
    if api != PLUGIN_API:
        found.fehler = (f"braucht Plugin-API {api}, batzen bietet {PLUGIN_API}" if api
                        else "BATZEN_PLUGIN_API fehlt im Modul")
    return found


@lru_cache(maxsize=1)
def installed() -> dict[str, Installed]:
    """Every plugin installed in this Python environment (loaded, not yet enabled)."""
    out: dict[str, Installed] = {}
    for ep in metadata.entry_points(group=GROUP):
        version = ep.dist.version if ep.dist else ""
        try:
            out[ep.name] = _inspect(ep.name, ep.load(), version)
        except Exception as exc:  # a broken plugin must not take batzen down
            out[ep.name] = _inspect(ep.name, None, version, f"lässt sich nicht laden: {type(exc).__name__}: {exc}")
    for name, module in _EXTRA.items():
        out[name] = _inspect(name, module)
    return out


def enabled_names(book: Book | None) -> tuple[str, ...]:
    if book is None:
        return ()
    raw = book.settings.get("plugins") or []
    return tuple(str(n) for n in raw)


def problems(book: Book) -> list[str]:
    """Plugins the book needs that cannot run here."""
    out = []
    for name in enabled_names(book):
        plugin = installed().get(name)
        if plugin is None:
            out.append(f"Plugin '{name}' ist eingeschaltet, aber nicht installiert (pip install batzen-{name})")
        elif not plugin.ok:
            out.append(f"Plugin '{name}' kann nicht laufen: {plugin.fehler}")
    return out


@lru_cache(maxsize=32)
def _manager(names: tuple[str, ...]) -> pluggy.PluginManager:
    from . import builtin
    pm = pluggy.PluginManager("batzen")
    pm.add_hookspecs(Spec)
    pm.register(builtin, "builtin")
    for name in names:
        plugin = installed().get(name)
        if plugin and plugin.ok:
            pm.register(plugin.module, name)
    return pm


def manager(book: Book | None) -> pluggy.PluginManager:
    """Hooks of the built-ins plus the plugins this book enables."""
    return _manager(enabled_names(book))


def data_manager() -> pluggy.PluginManager:
    """Hooks of the built-ins plus every installed plugin — for data only."""
    return _manager(tuple(sorted(n for n, p in installed().items() if p.ok)))


def _flat(results) -> list:
    return [item for result in results if result for item in result]


# ---------- what the core asks for ----------

def kontenplaene() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for result in reversed(data_manager().hook.batzen_kontenplaene()):   # built-in last → wins on clash
        out.update(result or {})
    return out


def qst_dirs() -> list[Path]:
    return [Path(p) for p in data_manager().hook.batzen_qst_tarife() if p]


def bank_formats(book: Book | None) -> list[BankFormat]:
    return _flat(manager(book).hook.batzen_bank_formats())


def bank_format_for(book: Book | None, filename: str, data: bytes) -> BankFormat:
    formats = bank_formats(book)
    for fmt in formats:
        try:
            if fmt.detect(filename, data):
                return fmt
        except Exception:
            continue
    from . import bankformat
    learned = bankformat.bank_format(book, filename, data)
    if learned is not None:
        return learned
    names = ", ".join(f.label for f in formats)
    suffix = Path(filename).suffix.lower()
    if suffix in bankformat.TEXT + bankformat.EXCEL:
        hint = (f"Das Format kann der Agent lernen: batzen bank format lernen {Path(filename).name} "
                "(danach bestätigen und importieren).")
    elif suffix == ".pdf":
        hint = (f"Eine Kreditkartenabrechnung liest der Agent ein: batzen bank karte {Path(filename).name} "
                "--konto <Kreditkartenkonto>.")
    else:
        hint = "Weitere Formate gibt es als Plugins (batzen plugins)."
    raise BookError(f"Unbekanntes Kontoauszugsformat ({Path(filename).name}). Unterstützt: {names}. {hint}")


def beleg_leser(book: Book | None) -> list[BelegLeser]:
    return _flat(manager(book).hook.batzen_beleg_leser())


def sources(book: Book | None) -> dict[str, Source]:
    return {s.prefix: s for s in _flat(manager(book).hook.batzen_sources())}


def check_findings(book: Book, rows: list[Row]) -> list[Finding]:
    out = []
    for item in _flat(manager(book).hook.batzen_check(book=book, rows=rows)):
        if isinstance(item, Finding):
            out.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            out.append(Finding(*item))
        elif isinstance(item, dict):
            out.append(Finding(item.get("level", "fehler"), item.get("where", ""), item.get("message", "")))
    return out


def agent_tools(book: Book | None) -> list[Callable]:
    return _flat(manager(book).hook.batzen_tools())


def agent_instructions(book: Book | None) -> str:
    parts = [p.strip() for p in manager(book).hook.batzen_instructions() if p and p.strip()]
    return "\n".join(parts)


def pages(book: Book | None) -> list[tuple[str, Page]]:
    """(plugin, page) for every page of the plugins this book enables."""
    out = []
    for impl in manager(book).hook.batzen_pages.get_hookimpls():
        try:
            for page in impl.function() or []:
                out.append((impl.plugin_name, page))
        except Exception:
            continue
    return out


def template_dir(name: str) -> Path | None:
    """The templates/ folder next to a plugin's module (convention)."""
    plugin = installed().get(name)
    if plugin is None or not plugin.ok or not getattr(plugin.module, "__file__", None):
        return None
    folder = Path(plugin.module.__file__).parent / "templates"
    return folder if folder.is_dir() else None


def commands() -> dict[str, tuple[str, Command]]:
    """CLI commands of every installed plugin, as name → (plugin, command)."""
    out: dict[str, tuple[str, Command]] = {}
    for name, plugin in sorted(installed().items()):
        if not plugin.ok or not hasattr(plugin.module, "batzen_commands"):
            continue
        try:
            for cmd in plugin.module.batzen_commands() or []:
                out.setdefault(cmd.name, (name, cmd))
        except Exception:
            continue
    return out


DATA_HOOKS = {"kontenplaene", "qst_tarife"}


def describe(book: Book | None) -> list[dict]:
    enabled = set(enabled_names(book))
    rows = []
    for name, plugin in sorted(installed().items()):
        hooks = sorted(h[len("batzen_"):] for h in dir(plugin.module or object) if h.startswith("batzen_")
                       and callable(getattr(plugin.module, h)))
        rows.append({"name": name, "version": plugin.version, "beschreibung": plugin.beschreibung,
                     "eingeschaltet": name in enabled, "ok": plugin.ok, "fehler": plugin.fehler, "hooks": hooks,
                     "nur_daten": bool(hooks) and set(hooks) <= DATA_HOOKS})
    for name in sorted(enabled - set(installed())):
        rows.append({"name": name, "version": "", "beschreibung": "", "eingeschaltet": True, "ok": False,
                     "fehler": "nicht installiert", "hooks": [], "nur_daten": False})
    return rows

"""Plugins: extend aeradex without forking it.

A plugin is an ordinary Python package that declares an entry point in the
group ``aeradex.plugins``::

    [project.entry-points."aeradex.plugins"]
    revolut = "aeradex_revolut"

The module sets ``AERADEX_PLUGIN_API = 1`` and implements any of the hooks in
`Spec` with ``@hookimpl`` (pluggy, the plugin system of pytest).

Two rules keep a book trustworthy no matter which plugins run:

* **Installed is not enabled.** Behaviour (bank formats, owned journal rows,
  check rules, agent tools, commands) only runs for books that list the plugin
  in ``aeradex.yaml → plugins``. Data (chart templates, withholding-tax tables)
  is offered by every installed plugin, because it is needed before a book exists.
* **Plugins write through the core.** They change files only inside
  ``api.write`` (guard → write → check → git commit), and rows they own
  (``Quelle <prefix>:…``) are regenerated and compared by ``aeradex check``.
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
GROUP = "aeradex.plugins"

hookspec = pluggy.HookspecMarker("aeradex")
hookimpl = pluggy.HookimplMarker("aeradex")


# ---------- what a plugin can contribute ----------

@dataclass
class BankFormat:
    """A bank statement format for `aeradex bank import`.

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
    """A CLI command: ``aeradex <name> …``. ``run(book, args)`` returns what the CLI prints."""
    name: str
    help: str
    run: Callable[[Book, argparse.Namespace], Any]
    setup: Callable[[argparse.ArgumentParser], None] = lambda parser: None
    needs_book: bool = True


@dataclass
class Page:
    """A page in the web UI: ``/p/<plugin>/<slug>``. ``bereich`` says where it lives:

    - a core section (``einkauf``, ``verkauf``, ``bank``, ``lohn``, ``buchhaltung``): a tab at its end;
    - ``einstellungen:<tab>`` (``firma``, ``buchhaltung``, ``lohn``, ``agent``, ``buch``): a card in that tab of
      the settings, no menu entry;
    - any other name: a section of its own, the pages with the same name are its tabs. ``bereich_titel`` is its
      label (default: the first page's label), ``nach`` the core section it follows (default: the end),
      ``kuerzel`` its keyboard shortcut;
    - empty: no menu entry; the page is linked next to its plugin in Einstellungen › Plugins.

    ``template`` is a Jinja file in the plugin package's ``templates/`` folder (it may
    ``{% extends "base.html" %}`` and use the core macros); ``context(book, query)``
    returns its variables. ``actions`` are form posts to ``/p/<plugin>/<slug>/<name>``:
    ``fn(book, form) -> dict`` — write through ``api.write`` so the book is checked and
    committed. A result may carry ``"weiter": "<url>"`` to go elsewhere afterwards.

    ``fragments`` are parts of the page a browser fetches on their own (``GET /p/<plugin>/<slug>/<name>``):
    ``name → (template, context(book, query))``. An action whose result carries ``"fragment": "<name>"`` (and
    optionally ``"query": {...}``) answers with that fragment instead of reloading the page — for fast edits
    (a timer, a grid cell, a quick entry). The result's ``meldung`` is shown as a toast.
    """
    slug: str
    label: str
    template: str
    context: Callable[[Book, dict], dict] = lambda book, query: {}
    actions: dict[str, Callable[[Book, dict], Any]] = field(default_factory=dict)
    bereich: str = ""
    fragments: dict[str, tuple[str, Callable[[Book, dict], dict]]] = field(default_factory=dict)
    bereich_titel: str = ""
    nach: str = ""
    kuerzel: str = ""


@dataclass
class DossierTeil:
    """A part of the Abschlussunterlagen (`aeradex dossier`), e.g. an Anlagenspiegel.

    ``build(book, year, fmt)`` returns ``[(file name, bytes)]`` for one of ``formate``
    ("pdf", "csv" …); a name may contain a folder ("Anlagen 2026/Spiegel.pdf").
    ``relevant(book, year)`` decides whether the part applies to a book; None = always.
    """
    key: str
    label: str
    formate: tuple[str, ...]
    build: Callable[[Book, int, str], list[tuple[str, bytes]]]
    relevant: Callable[[Book, int], bool] | None = None
    beschreibung: str = ""


@dataclass
class Datensatz:
    """A kind of data for the Datensicherung (backup as CSV/PDF), e.g. «Anlagen» or «Zeiterfassung».

    ``build(book, jahr, fmt)`` returns ``[(file name, bytes)]`` for one of ``formate`` ("csv", "pdf" …);
    ``jahr`` is None unless ``jahresweise``. ``pfade`` are globs of the book files it covers (relative to the book,
    e.g. "anlagen/**"): every file of a book must be covered by some Datensatz, so new data is never left out;
    the originals of these files are offered too («dateien»). ``anzahl(book, jahr)`` counts the records (None =
    unknown); a Datensatz with 0 is shown as empty. ``vertraulich``: personal data (wages, health …).
    """
    key: str
    gruppe: str
    label: str
    formate: tuple[str, ...]
    build: Callable[[Book, int | None, str], list[tuple[str, bytes]]]
    pfade: tuple[str, ...] = ()
    jahresweise: bool = False
    anzahl: Callable[[Book, int | None], int | None] | None = None
    beschreibung: str = ""
    vertraulich: bool = False


@dataclass
class Todo:
    """A line in the to-do list of the Übersicht: work waiting for a person, leading to where it is done.
    ``level`` info | warn | red."""
    titel: str
    href: str
    detail: str = ""
    tag: str = ""
    level: str = "info"


@dataclass
class AbschlussSchritt:
    """A step of the year-end checklist (Abschluss page). ``phase``: laufend | bewerten | buchen | kontrolle |
    abschliessen; ``status``: ok | offen | hinweis (a person ticks it off) | entfaellt (does not apply this year)."""
    key: str
    titel: str
    status: str
    link: str = ""
    text: str = ""
    phase: str = "buchen"


@dataclass
class Dienst:
    """A background service the local UI (`aeradex ui`) starts with itself and stops with itself, e.g. a call
    gateway or a chat bot. `args` are aeradex CLI arguments after `--buch <buch>`."""
    name: str
    args: list[str]


@dataclass
class KundeTeil:
    """A part of a customer's detail page (`/debitoren/kunden/<nr>`), e.g. the customer's objects or quotes.

    ``template`` is a Jinja file in the plugin's ``templates/`` folder, rendered with ``context`` plus ``book``
    and ``kunde``; empty = only ``zeitachse``/``kennzahlen``. ``zeitachse`` are events for the customer's timeline
    (``{datum, art, titel, text, link}``), ``kennzahlen`` figures for the top row (``{label, wert, meta, link}``),
    ``aktionen`` buttons in the page header (``(label, href)``)."""
    titel: str
    template: str = ""
    context: dict = field(default_factory=dict)
    zeitachse: list[dict] = field(default_factory=list)
    kennzahlen: list[dict] = field(default_factory=list)
    aktionen: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Finding:
    """A `check` result from a plugin rule. level: fehler | warnung | hinweis."""
    level: str
    where: str
    message: str


class Spec:
    """The hooks (plugin API version 1). Implement any subset."""

    @hookspec
    def aeradex_kontenplaene(self) -> dict[str, Path]:
        """Chart-of-accounts templates for `aeradex init --kontenplan NAME` (data)."""

    @hookspec
    def aeradex_qst_tarife(self) -> Path:
        """A folder with withholding-tax tables `<KANTON>-<JAHR>.json` (data)."""

    @hookspec
    def aeradex_bank_formats(self) -> list[BankFormat]:
        """Statement formats for the bank import."""

    @hookspec
    def aeradex_beleg_leser(self) -> list[BelegLeser]:
        """Readers for supplier bills (e.g. e-invoices: ZUGFeRD, XRechnung)."""

    @hookspec
    def aeradex_sources(self) -> list[Source]:
        """Documents that own journal rows."""

    @hookspec
    def aeradex_check(self, book: Book, rows: list[Row]) -> list[Finding]:
        """Extra rules for `aeradex check`."""

    @hookspec
    def aeradex_tools(self) -> list[Callable]:
        """Agent tools (MCP and chat panel); functions documented like those in `aeradex.tools`."""

    @hookspec
    def aeradex_instructions(self) -> str:
        """A short paragraph for the agent on when to use this plugin's tools."""

    @hookspec
    def aeradex_commands(self) -> list[Command]:
        """CLI commands."""

    @hookspec
    def aeradex_pages(self) -> list[Page]:
        """Pages in the web UI (templates in the plugin package's templates/ folder)."""

    @hookspec
    def aeradex_reports(self) -> list:
        """Reports (`aeradex.reports.Report`) for Berichte, `aeradex bericht` and the agent."""

    @hookspec
    def aeradex_dossier_teile(self) -> list[DossierTeil]:
        """Parts for the Abschlussunterlagen of a year."""

    @hookspec
    def aeradex_datensicherung(self, book: Book) -> list[Datensatz]:
        """Kinds of data this plugin keeps in the book, for the Datensicherung (CSV/PDF and originals)."""

    @hookspec
    def aeradex_sprache_zu_text(self, book: Book) -> Callable[[bytes, str], str] | None:
        """Speech to text for voice input in the browser (the agent's microphone): a function
        ``(audio, filename) -> text`` when this plugin is set up for it (e.g. a speech service key), else None."""

    @hookspec
    def aeradex_kunde_teile(self, book: Book, kunde: str) -> list[KundeTeil]:
        """Parts of a customer's detail page (objects, quotes, open work …)."""

    @hookspec
    def aeradex_todos(self, book: Book) -> list[Todo]:
        """Lines for the to-do list of the Übersicht (work waiting for a person)."""

    @hookspec
    def aeradex_abschluss_schritte(self, book: Book, jahr: int) -> list[AbschlussSchritt]:
        """Steps for the year-end checklist of `jahr` (e.g. book the depreciation)."""

    @hookspec
    def aeradex_abschluss_vorschlaege(self, book: Book, jahr: int, art: str) -> list[dict]:
        """Values to start a closing entry from (`jahresabschluss.ARTEN`: arbeiten, material, delkredere, ferien,
        privatanteile): ``{text, betrag, detail}``; for privatanteile also ``art`` (fahrzeug …), ``konto``,
        ``grundlage``. ``betrag`` None with ``info: True`` is a hint only."""

    @hookspec
    def aeradex_nach_beleg(self, book: Book, beleg: str) -> str | None:
        """Where the UI continues after a document from the inbox was booked as `beleg` (a supplier bill, a
        receipt): a URL for a follow-up the plugin offers (e.g. turn an asset purchase into an asset), or None."""

    @hookspec
    def aeradex_dienste(self, book: Book) -> list[Dienst]:
        """Background services the local UI starts for this book (`aeradex ui`, not the server mode)."""


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
    api = getattr(module, "AERADEX_PLUGIN_API", None)
    doc = (module.__doc__ or "").strip().splitlines()
    found = Installed(name, module, version or getattr(module, "__version__", ""), doc[0] if doc else "", api)
    if api != PLUGIN_API:
        found.fehler = (f"braucht Plugin-API {api}, aeradex bietet {PLUGIN_API}" if api
                        else "AERADEX_PLUGIN_API fehlt im Modul")
    return found


@lru_cache(maxsize=1)
def installed() -> dict[str, Installed]:
    """Every plugin installed in this Python environment (loaded, not yet enabled)."""
    out: dict[str, Installed] = {}
    for ep in metadata.entry_points(group=GROUP):
        version = ep.dist.version if ep.dist else ""
        try:
            out[ep.name] = _inspect(ep.name, ep.load(), version)
        except Exception as exc:  # a broken plugin must not take aeradex down
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
            out.append(f"Plugin '{name}' ist eingeschaltet, aber nicht installiert (pip install aeradex-{name})")
        elif not plugin.ok:
            out.append(f"Plugin '{name}' kann nicht laufen: {plugin.fehler}")
    return out


@lru_cache(maxsize=32)
def _manager(names: tuple[str, ...]) -> pluggy.PluginManager:
    from . import builtin
    pm = pluggy.PluginManager("aeradex")
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
    for result in reversed(data_manager().hook.aeradex_kontenplaene()):   # built-in last → wins on clash
        out.update(result or {})
    return out


def qst_dirs() -> list[Path]:
    return [Path(p) for p in data_manager().hook.aeradex_qst_tarife() if p]


def bank_formats(book: Book | None) -> list[BankFormat]:
    return _flat(manager(book).hook.aeradex_bank_formats())


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
        hint = (f"Das Format kann der Agent lernen: aeradex bank format lernen {Path(filename).name} "
                "(danach bestätigen und importieren).")
    elif suffix == ".pdf":
        hint = (f"Eine Kreditkartenabrechnung liest der Agent ein: aeradex bank karte {Path(filename).name} "
                "--konto <Kreditkartenkonto>.")
    else:
        hint = "Weitere Formate gibt es als Plugins (aeradex plugins)."
    raise BookError(f"Unbekanntes Kontoauszugsformat ({Path(filename).name}). Unterstützt: {names}. {hint}")


def beleg_leser(book: Book | None) -> list[BelegLeser]:
    return _flat(manager(book).hook.aeradex_beleg_leser())


def sources(book: Book | None) -> dict[str, Source]:
    return {s.prefix: s for s in _flat(manager(book).hook.aeradex_sources())}


def dossier_teile(book: Book | None) -> list[DossierTeil]:
    return list(_flat(manager(book).hook.aeradex_dossier_teile()))


def datensicherung_teile(book: Book) -> list[Datensatz]:
    return list(_flat(manager(book).hook.aeradex_datensicherung(book=book)))


def check_findings(book: Book, rows: list[Row]) -> list[Finding]:
    out = []
    for item in _flat(manager(book).hook.aeradex_check(book=book, rows=rows)):
        if isinstance(item, Finding):
            out.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            out.append(Finding(*item))
        elif isinstance(item, dict):
            out.append(Finding(item.get("level", "fehler"), item.get("where", ""), item.get("message", "")))
    return out


def agent_tools(book: Book | None) -> list[Callable]:
    return _flat(manager(book).hook.aeradex_tools())


def agent_instructions(book: Book | None) -> str:
    parts = [p.strip() for p in manager(book).hook.aeradex_instructions() if p and p.strip()]
    return "\n".join(parts)


def reports(book: Book | None) -> list:
    """Reports of the plugins this book enables; a broken plugin is skipped."""
    out = []
    for impl in manager(book).hook.aeradex_reports.get_hookimpls():
        try:
            out += list(impl.function() or [])
        except Exception:
            continue
    return out


def pages(book: Book | None) -> list[tuple[str, Page]]:
    """(plugin, page) for every page of the plugins this book enables."""
    out = []
    for impl in manager(book).hook.aeradex_pages.get_hookimpls():
        try:
            for page in impl.function() or []:
                out.append((impl.plugin_name, page))
        except Exception:
            continue
    return out


def dienste(book: Book) -> list[Dienst]:
    """Services of the plugins this book enables; a broken plugin is skipped."""
    out = []
    for impl in manager(book).hook.aeradex_dienste.get_hookimpls():
        try:
            kwargs = {"book": book} if "book" in impl.argnames else {}    # pluggy: an impl may take fewer args
            out += [d for d in impl.function(**kwargs) or [] if isinstance(d, Dienst)]
        except Exception:
            continue
    return out


def kunde_teile(book: Book, kunde: str) -> list[tuple[str, KundeTeil]]:
    """(plugin, part) for a customer's detail page; a broken plugin is skipped."""
    out = []
    for impl in manager(book).hook.aeradex_kunde_teile.get_hookimpls():
        try:
            out += [(impl.plugin_name, t) for t in impl.function(book=book, kunde=kunde) or [] if isinstance(t, KundeTeil)]
        except Exception:
            continue
    return out


def sprache_zu_text(book: Book) -> Callable[[bytes, str], str] | None:
    """The first speech-to-text a plugin offers for this book; a broken plugin is skipped."""
    for impl in manager(book).hook.aeradex_sprache_zu_text.get_hookimpls():
        try:
            fn = impl.function(book=book)
        except Exception:
            continue
        if callable(fn):
            return fn
    return None


def todos(book: Book) -> list[Todo]:
    """To-do lines of the plugins this book enables; a broken plugin is skipped."""
    out = []
    for impl in manager(book).hook.aeradex_todos.get_hookimpls():
        try:
            out += [t for t in impl.function(book=book) or [] if isinstance(t, Todo)]
        except Exception:
            continue
    return out


def abschluss_schritte(book: Book, jahr: int) -> list[AbschlussSchritt]:
    """Checklist steps of the plugins this book enables; a broken plugin is skipped."""
    out = []
    for impl in manager(book).hook.aeradex_abschluss_schritte.get_hookimpls():
        try:
            out += [s for s in impl.function(book=book, jahr=jahr) or [] if isinstance(s, AbschlussSchritt)]
        except Exception:
            continue
    return out


def abschluss_vorschlaege(book: Book, jahr: int, art: str) -> list[dict]:
    """Proposed values for a closing entry from the plugins; a broken plugin is skipped."""
    out = []
    for impl in manager(book).hook.aeradex_abschluss_vorschlaege.get_hookimpls():
        try:
            out += [dict(v) for v in impl.function(book=book, jahr=jahr, art=art) or [] if isinstance(v, dict)]
        except Exception:
            continue
    return out


def after_beleg(book: Book, beleg: str) -> str | None:
    """The first follow-up URL a plugin offers for a freshly booked Beleg; a broken plugin is skipped."""
    for impl in manager(book).hook.aeradex_nach_beleg.get_hookimpls():
        try:
            url = impl.function(book=book, beleg=beleg)
        except Exception:
            continue
        if url:
            return str(url)
    return None


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
        if not plugin.ok or not hasattr(plugin.module, "aeradex_commands"):
            continue
        try:
            for cmd in plugin.module.aeradex_commands() or []:
                out.setdefault(cmd.name, (name, cmd))
        except Exception:
            continue
    return out


DATA_HOOKS = {"kontenplaene", "qst_tarife"}


def describe(book: Book | None) -> list[dict]:
    enabled = set(enabled_names(book))
    rows = []
    for name, plugin in sorted(installed().items()):
        hooks = sorted(h[len("aeradex_"):] for h in dir(plugin.module or object) if h.startswith("aeradex_")
                       and callable(getattr(plugin.module, h)))
        module = plugin.module
        rows.append({"name": name, "version": plugin.version, "beschreibung": plugin.beschreibung,
                     "eingeschaltet": name in enabled, "ok": plugin.ok, "fehler": plugin.fehler, "hooks": hooks,
                     "nur_daten": bool(hooks) and set(hooks) <= DATA_HOOKS,
                     # How the settings show it as a function: AERADEX_TITEL/TEXT/TEIL_VON in the plugin module.
                     "titel": str(getattr(module, "AERADEX_TITEL", "") or name),
                     "text": str(getattr(module, "AERADEX_TEXT", "") or plugin.beschreibung),
                     "teil_von": str(getattr(module, "AERADEX_TEIL_VON", "") or ""),
                     "seiten": _page_list(name, module)})
    for name in sorted(enabled - set(installed())):
        rows.append({"name": name, "version": "", "beschreibung": "", "eingeschaltet": True, "ok": False,
                     "fehler": "nicht installiert", "hooks": [], "nur_daten": False, "titel": name, "text": "",
                     "teil_von": "", "seiten": []})
    return rows


def _page_list(name: str, module: ModuleType | None) -> list[dict]:
    """A plugin's pages (also when it is switched off), for «where to find it» in the settings."""
    try:
        found = module.aeradex_pages() if module is not None and hasattr(module, "aeradex_pages") else []
    except Exception:
        return []
    titles = {pg.bereich: pg.bereich_titel for pg in found or [] if pg.bereich_titel}
    return [{"url": f"/p/{name}/{pg.slug}", "label": pg.label, "bereich": pg.bereich,
             "bereich_titel": pg.bereich_titel or titles.get(pg.bereich, "")} for pg in found or []]

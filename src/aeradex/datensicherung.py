"""Datensicherung: every kind of data in the book as CSV (Excel) and PDF, one by one or as a ZIP.

The `.aeradex` file (`austausch.py`) is the backup to restore from; this is the backup to read and archive: the
Abschlussunterlagen per year (`dossier.TEILE` and plugin parts), plus master data and documents — customers,
invoices, suppliers, bills, bank movements, employees, payslips, expenses, settings — and the original files.

Every kind of data is a `plugins.Datensatz` with the book files it covers (`pfade`). `ungesichert(book)` lists files
no Datensatz covers: a test keeps it empty, so a new feature that stores data must add its Datensatz here or
through the plugin hook `aeradex_datensicherung`. In the ZIP, such files still travel in «Rohdaten».
"""
from __future__ import annotations

import fnmatch
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import __version__, dossier, gitlog
from . import pdf as pdfmod
from .book import Book, BookError
from .plugins import Datensatz

# Files that are not data: the repository, local state, generated output, agent notes.
INTERN = (".git/**", ".aeradex/**", "berichte/**", ".gitignore", ".gitattributes", "AGENTS.md", "CLAUDE.md",
          "**/.gitkeep", "**/__pycache__/**", "**/.DS_Store")
GRUPPEN = ("Buchhaltung", "MWST", "Verkauf", "Einkauf", "Bank", "Belege", "Lohn", "Einstellungen")
FORMAT_LABEL = {"csv": "CSV", "pdf": "PDF", "dateien": "Dateien", "xml": "XML"}


# ---------- tables from records ----------

def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, Decimal):
        return dossier.amount(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        if all(not isinstance(v, (dict, list)) for v in value):
            return ", ".join(_cell(v) for v in value)
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _pdf_cell(value) -> str:
    """Like the CSV, but Swiss dates and amounts with thousands separators."""
    if isinstance(value, Decimal):
        return pdfmod.chf(value)
    text = _cell(value)
    if len(text) >= 10 and text[4] == "-" and text[7] == "-" and text[:4].isdigit() and len(text) in (10, 19):
        return f"{text[8:10]}.{text[5:7]}.{text[:4]}"
    return text if len(text) <= 160 else text[:157] + "…"


def _flat(record: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in record.items():
        if str(k).startswith("_") or k in ("hash", "passwort"):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v and all(not isinstance(x, (dict, list)) for x in v.values()):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def tabelle(book: Book, items: list[dict], spalten: list[tuple[str, str]], titel: str, datei: str, fmt: str,
            untertitel: str = "", breit: bool = False) -> list[tuple[str, bytes]]:
    """Records as a table: CSV with the preferred columns first and every other field after them; PDF with the
    preferred columns that occur."""
    flat = [_flat(r) for r in items]
    if fmt == "csv":
        keys = [k for k, _ in spalten if any(k in r for r in flat)] if flat else [k for k, _ in spalten]
        extra = []
        for r in flat:
            extra += [k for k in r if k not in keys and k not in extra]
        head = [dict(spalten).get(k, k) for k in keys] + extra
        return [(f"{datei}.csv", dossier.csv_bytes(head, [[_cell(r.get(k)) for k in keys + extra] for r in flat]))]
    keys = [k for k, _ in spalten if any(r.get(k) not in (None, "") for r in flat)] or list(flat[0])[:6] if flat else []
    labels = dict(spalten)
    rows = [[_pdf_cell(r.get(k)) for k in keys] for r in flat]
    numeric = tuple(i for i, k in enumerate(keys)
                    if rows and all(isinstance(r.get(k), (Decimal, int, float)) or r.get(k) in (None, "") for r in flat))
    widths = tuple(1 / max(len(keys), 1) for _ in keys)
    blocks = [("table", [labels.get(k, k) for k in keys], rows, widths,
               {"right": numeric, "wrap": tuple(range(len(keys))), "zebra": True})] if rows else \
        [("text", "Keine Einträge.")]
    return [(f"{datei}.pdf", pdfmod.report_pdf(book, titel, blocks, subtitle=untertitel or
                                               f"{book.settings.firma} · Stand {date.today():%d.%m.%Y}",
                                               wide=breit or len(keys) > 6))]


def liste(key: str, gruppe: str, label: str, lesen, spalten: list[tuple[str, str]], pfade: tuple[str, ...] = (),
          jahresweise: bool = False, beschreibung: str = "", vertraulich: bool = False) -> Datensatz:
    """A Datensatz from a reader `lesen(book, jahr) -> list[dict]` (also for plugins)."""
    def build(book: Book, jahr: int | None, fmt: str) -> list[tuple[str, bytes]]:
        items = lesen(book, jahr)
        name = f"{label} {jahr}" if jahresweise and jahr else label
        return tabelle(book, items, spalten, name, name, fmt)
    return Datensatz(key, gruppe, label, ("csv", "pdf"), build, pfade, jahresweise,
                     lambda book, jahr: len(lesen(book, jahr)), beschreibung, vertraulich)


def _year_of(value) -> int | None:
    text = _cell(value)[:4]
    return int(text) if text.isdigit() else None


def _in_year(items: list[dict], jahr: int | None, field: str = "datum") -> list[dict]:
    return items if jahr is None else [r for r in items if _year_of(r.get(field)) == jahr]


# ---------- readers of the core ----------

def _kunden(book: Book, jahr=None) -> list[dict]:
    from . import invoices
    return [{**meta, "nummer": meta.get("nummer") or nr} for nr, meta in sorted(invoices.customers(book).items())]


def _rechnungen(book: Book, jahr=None) -> list[dict]:
    from . import invoices
    out = []
    paid = invoices.settlements(book)
    for nr, meta in sorted(invoices.invoices(book).items()):
        try:
            st = invoices.invoice_state(book, meta, paid.get(nr))
        except (BookError, KeyError, ValueError):
            st = {}
        out.append({**{k: v for k, v in st.items() if k not in ("referenz", "extern")},
                    **{k: v for k, v in meta.items() if k not in st}, "nummer": nr})
    return _in_year(out, jahr)


def _mahnungen(book: Book, jahr=None) -> list[dict]:
    from . import mahnungen
    folder = book.root / "mahnungen"
    out = []
    for path in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        for e in mahnungen.history(book, path.stem):
            out.append({"rechnung": path.stem, **e})
    return out


def _lieferanten(book: Book, jahr=None) -> list[dict]:
    from . import kreditoren
    return [{**meta, "nummer": meta.get("nummer") or nr} for nr, meta in sorted(kreditoren.suppliers(book).items())]


def _kreditoren(book: Book, jahr=None) -> list[dict]:
    from . import kreditoren
    out = []
    for nr, meta in sorted(kreditoren.bills(book).items()):
        try:
            st = kreditoren.state(book, meta)
        except (BookError, KeyError, ValueError):
            st = {}
        out.append({**st, **{k: v for k, v in meta.items() if k not in st}, "nummer": nr})
    return _in_year(out, jahr)


def _zahlungen(book: Book, jahr=None) -> list[dict]:
    from . import kreditoren
    return kreditoren.runs(book)


def _eingang(book: Book, jahr=None) -> list[dict]:
    from . import erfassung
    return [{"id": i, **meta} for i, meta in sorted(erfassung.drafts(book).items())]


def _bank(book: Book, jahr=None) -> list[dict]:
    from . import bank
    return _in_year([{k: v for k, v in tx.items() if not k.startswith("_")} for tx in bank.transactions(book)],
                    jahr, "Datum")


def _personal(book: Book, jahr=None) -> list[dict]:
    from . import payroll
    return [{**meta, "nummer": meta.get("nummer") or nr} for nr, meta in sorted(payroll.employees(book).items())]


def _spesen(book: Book, jahr=None) -> list[dict]:
    from . import spesen
    return _in_year([{"nummer": nr, **meta} for nr, meta in sorted(spesen.claims(book).items())], jahr)


def _vorschlaege(book: Book, jahr=None) -> list[dict]:
    from .files import read_table
    path = book.root / "vorschlaege.md"
    return read_table(path).rows if path.exists() else []


def _budget(book: Book, jahr: int) -> list[dict]:
    from . import budget
    if not budget.exists(book, jahr):
        return []
    return [{"konto": nr, "name": book.accounts[nr].name if nr in book.accounts else "",
             **{f"{m + 1:02d}": v for m, v in enumerate(months)}, "jahr": sum(months, Decimal(0))}
            for nr, months in sorted(budget.load(book, jahr).items())]


def _abschluss(book: Book, jahr: int) -> list[dict]:
    from .files import read_yaml
    out = []
    folder = book.root / "abschluss"
    for path in sorted(folder.rglob("*.yaml")) if folder.exists() else []:
        rel = path.relative_to(folder).as_posix()
        if str(jahr) not in rel:
            continue
        out.append({"datei": rel, "inhalt": read_yaml(path)})
    folder = book.root / "bewertung"
    for path in sorted(folder.glob(f"{jahr}-*.yaml")) if folder.exists() else []:
        out.append({"datei": f"bewertung/{path.name}", "inhalt": read_yaml(path)})
    return out


def _lohnabrechnungen(book: Book, jahr: int, fmt: str) -> list[tuple[str, bytes]]:
    from . import payroll
    slips = sorted(payroll.payslips(book, jahr), key=lambda p: (int(p["monat"]), p["mitarbeiter"]))
    if fmt == "csv":
        return tabelle(book, slips, [("monat", "Monat"), ("mitarbeiter", "Nr"), ("name", "Name"), ("status", "Status")],
                       f"Lohnabrechnungen {jahr}", f"Lohnabrechnungen {jahr}", "csv")
    emps = payroll.employees(book)
    out = []
    for p in slips:
        try:
            data = pdfmod.payslip_pdf(book, p, emps.get(p["mitarbeiter"], {}))
        except Exception:                       # an incomplete draft cannot be printed; the CSV has it
            continue
        out.append((f"Lohnabrechnungen {jahr}/{int(p['monat']):02d} {p['mitarbeiter']} {p.get('name') or ''}.pdf".replace("  ", " "),
                    data))
    return out


def _einstellungen(book: Book, jahr, fmt: str) -> list[tuple[str, bytes]]:
    s = book.settings.data

    def walk(d, prefix=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from walk(v, f"{prefix}{k}.")
            else:
                yield {"einstellung": f"{prefix}{k}", "wert": v}
    from . import payroll
    items = list(walk(s)) + [dict(r, einstellung="lohn." + r["einstellung"]) for r in walk(payroll.config(book))]
    return tabelle(book, items, [("einstellung", "Einstellung"), ("wert", "Wert")], "Einstellungen", "Einstellungen", fmt)


# ---------- the registry ----------

def _from_dossier(t: dossier.Teil, gruppe: str, pfade: tuple[str, ...] = (), vertraulich: bool = False) -> Datensatz:
    def build(book: Book, jahr: int | None, fmt: str) -> list[tuple[str, bytes]]:
        k = dossier.Kontext(book, jahr)
        return t.build(k, fmt) if t.relevant(k) else []

    def anzahl(book: Book, jahr: int | None) -> int | None:
        return None if t.relevant(dossier.Kontext(book, jahr)) else 0
    return Datensatz(t.key, gruppe, t.label, tuple(f for f in t.formate if f != "dateien"), build, pfade,
                     True, anzahl, t.beschreibung, vertraulich)


_DOSSIER = {  # dossier part → (gruppe, files it covers)
    "jahresrechnung": ("Buchhaltung", ()), "saldenliste": ("Buchhaltung", ()),
    "journal": ("Buchhaltung", ("journal/**",)), "kontoblaetter": ("Buchhaltung", ()),
    "belege": ("Belege", ("belege/**",)), "mwst": ("MWST", ("mwst/**",)),
    "offene_posten": ("Buchhaltung", ()), "lohn": ("Lohn", ("lohnausweise/**",)),
    "geldfluss": ("Buchhaltung", ()),
}


def _core() -> list[Datensatz]:
    out = []
    for t in dossier.TEILE:
        if t.key == "kontenplan":
            continue
        gruppe, pfade = _DOSSIER.get(t.key, ("Buchhaltung", ()))
        out.append(_from_dossier(t, gruppe, pfade, vertraulich=t.key == "lohn"))
    kontenplan = next(t for t in dossier.TEILE if t.key == "kontenplan")

    def kp_build(book, jahr, fmt):
        if fmt == "csv":
            accts = sorted(book.accounts.values(), key=lambda a: dossier.natural_key(a.nr))
            return [("Kontenplan.csv", dossier.csv_bytes(
                ["Konto", "Bezeichnung", "Klasse", "Gruppe", "Währung", "Aktiv", "Abschluss in", "Eröffnung",
                 "Vorjahr"], [[a.nr, a.name, a.klasse, a.gruppe, a.waehrung, "ja" if a.aktiv_ else "nein", a.abschluss,
                               a.eroeffnung, a.vorjahr] for a in accts]))]
        return kontenplan.build(dossier.Kontext(book, max(book.years())), fmt)
    out += [
        Datensatz("kontenplan", "Buchhaltung", "Kontenplan", ("csv", "pdf"), kp_build, ("kontenplan.yaml",),
                  anzahl=lambda book, jahr: len(book.accounts), beschreibung="Konten mit Eröffnung und Vorjahr"),
        liste("vorschlaege", "Buchhaltung", "Buchungsvorschläge", _vorschlaege,
              [("ID", "ID"), ("Datum", "Datum"), ("Text", "Text"), ("Soll", "Soll"), ("Haben", "Haben"),
               ("Betrag", "Betrag")], ("vorschlaege.md",), beschreibung="noch nicht freigegebene Buchungen"),
        liste("budget", "Buchhaltung", "Budget", _budget, [("konto", "Konto"), ("name", "Bezeichnung")] +
              [(f"{m:02d}", f"{m:02d}") for m in range(1, 13)] + [("jahr", "Jahr")], ("budget/**",), True),
        liste("abschluss", "Buchhaltung", "Abschlussbuchungen und Checkliste", _abschluss,
              [("datei", "Datei"), ("inhalt", "Inhalt")], ("abschluss/**", "bewertung/**"), True,
              "Bestände, Privatanteile, Abgrenzungen, Gewinnverwendung, Fremdwährungsbewertung, abgehakte Schritte"),
        liste("kunden", "Verkauf", "Kunden", _kunden,
              [("nummer", "Nr"), ("firma", "Firma"), ("name", "Name"), ("adresse.strasse", "Strasse"),
               ("adresse.nr", "Nr."), ("adresse.plz", "PLZ"), ("adresse.ort", "Ort"), ("email", "E-Mail"),
               ("telefon", "Telefon")], ("kunden/**",),
              beschreibung="Stammdaten, Kontakte und Wiedervorlagen"),
        liste("rechnungen", "Verkauf", "Rechnungen", _rechnungen,
              [("nummer", "Nummer"), ("datum", "Datum"), ("kunde", "Kunde"), ("name", "Name"), ("total", "Total"),
               ("waehrung", "Währung"), ("faellig", "Fällig"), ("bezahlt", "Bezahlt"), ("gutgeschrieben", "Gutschrift"),
               ("offen", "Offen"), ("status", "Status")],
              ("rechnungen/**",), True, "Rechnungen mit Positionen, Zahlungen und Gutschriften; PDFs bei «Dateien»"),
        liste("mahnungen", "Verkauf", "Mahnungen", _mahnungen,
              [("rechnung", "Rechnung"), ("stufe", "Stufe"), ("bezeichnung", "Bezeichnung"), ("datum", "Datum"),
               ("frist", "Frist"), ("offen", "Offen")],
              ("mahnungen/**",)),
        liste("lieferanten", "Einkauf", "Lieferanten", _lieferanten,
              [("nummer", "Nr"), ("name", "Name"), ("adresse.ort", "Ort"), ("iban", "IBAN"), ("konto", "Konto"),
               ("mwst", "MWST")],
              ("lieferanten/**",)),
        liste("kreditoren", "Einkauf", "Lieferantenrechnungen", _kreditoren,
              [("nummer", "Nummer"), ("datum", "Datum"), ("lieferant", "Lieferant"), ("name", "Name"),
               ("rechnungsnr", "Rechnungsnr."), ("total", "Total"), ("waehrung", "Währung"), ("faellig", "Fällig"),
               ("bezahlt", "Bezahlt"), ("offen", "Offen"), ("status", "Status")], ("kreditoren/**",), True, "Kreditoren mit Zahlungen; Originale bei «Dateien»"),
        liste("zahlungen", "Einkauf", "Zahlungsaufträge", _zahlungen,
              [("datei", "Datei"), ("ausfuehrung", "Ausführung"), ("anzahl", "Anzahl"), ("total", "Total")],
              ("zahlungen/**",), beschreibung="pain.001-Dateien an die Bank"),
        liste("eingang", "Einkauf", "Belegeingang", _eingang,
              [("id", "ID"), ("art", "Art"), ("datei", "Datei"), ("status", "Status"), ("lieferant", "Lieferant"),
               ("betrag", "Betrag"), ("datum", "Datum")], ("eingang/**", "inbox/**", "kreditoren/entwuerfe/**"),
              beschreibung="eingelesene, noch nicht gebuchte Belege und unverarbeitete Dateien"),
        liste("bank", "Bank", "Bankbewegungen", _bank,
              [("Datum", "Datum"), ("Konto", "Konto"), ("Text", "Text"), ("Gegenpartei", "Gegenpartei"),
               ("Betrag", "Betrag"), ("Status", "Status"), ("Beleg", "Beleg")], ("bank/**",), True,
              "aus den Kontoauszügen, mit Status und Beleg; Auszüge, Regeln und Formate bei «Dateien»"),
        liste("personal", "Lohn", "Mitarbeitende", _personal,
              [("nummer", "Nr"), ("vorname", "Vorname"), ("nachname", "Nachname"), ("adresse.ort", "Ort"),
               ("geburtsdatum", "Geburtsdatum"), ("ahv_nr", "AHV-Nr."), ("eintritt", "Eintritt"),
               ("austritt", "Austritt"), ("pensum", "Pensum"), ("monatslohn", "Monatslohn"),
               ("stundenlohn", "Stundenlohn")],
              ("personal/**",), beschreibung="Personalstammdaten", vertraulich=True),
        Datensatz("lohnabrechnungen", "Lohn", "Lohnabrechnungen", ("pdf", "csv"), _lohnabrechnungen, ("lohn/**",),
                  True, lambda book, jahr: len(__import__("aeradex.payroll", fromlist=["x"]).payslips(book, jahr)),
                  "jede Abrechnung als PDF", vertraulich=True),
        liste("spesen", "Lohn", "Spesen", _spesen,
              [("nummer", "Nummer"), ("datum", "Datum"), ("mitarbeiter", "Mitarbeiter"), ("text", "Text"),
               ("art", "Art"), ("konto", "Konto"), ("betrag", "Betrag"), ("status", "Status")], ("spesen/**",), True,
              vertraulich=True),
        Datensatz("einstellungen", "Einstellungen", "Einstellungen", ("csv", "pdf"), _einstellungen,
                  ("aeradex.yaml", "briefkopf/**", "auswertungen/**"), anzahl=lambda book, jahr: None,
                  beschreibung="Firma, MWST, Systemkonten, Lohnsätze; Logo und Berichtsvorlagen bei «Dateien»"),
    ]
    return out


def datensaetze(book: Book) -> list[Datensatz]:
    """Core kinds of data, the plugins' Abschlussunterlagen parts, and the plugins' own Datensätze."""
    from . import plugins
    out = _core()
    for t in plugins.dossier_teile(book):
        out.append(_from_dossier(dossier.Teil(t.key, "", t.label, tuple(t.formate),
                                              lambda kk, fmt, t=t: t.build(kk.book, kk.year, fmt),
                                              (lambda kk, t=t: t.relevant(kk.book, kk.year)) if t.relevant else
                                              (lambda kk: True), t.beschreibung), "Buchhaltung"))
    seen = {d.key for d in out}
    for d in plugins.datensicherung_teile(book):
        if d.key not in seen:
            out.append(d)
            seen.add(d.key)
    return out


def datensatz(book: Book, key: str) -> Datensatz:
    for d in datensaetze(book):
        if d.key == key:
            return d
    raise BookError(f"Unbekannte Daten «{key}»")


# ---------- coverage ----------

def _match(rel: str, patterns: tuple[str, ...]) -> bool:
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or (p.endswith("/**") and rel.startswith(p[:-2])):
            return True
    return False


def book_files(book: Book) -> list[str]:
    out = []
    for path in sorted(book.root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(book.root).as_posix()
            if not _match(rel, INTERN) and not any(part == ".git" for part in Path(rel).parts):
                out.append(rel)
    return out


def ungesichert(book: Book, sets: list[Datensatz] | None = None) -> list[str]:
    """Files of the book that no Datensatz covers (they still travel in «Rohdaten» and in the .aeradex file)."""
    patterns = tuple(p for d in (sets or datensaetze(book)) for p in d.pfade)
    return [rel for rel in book_files(book) if not _match(rel, patterns)]


def originale(book: Book, d: Datensatz, jahr: int | None = None) -> list[tuple[str, bytes]]:
    """The book's own files of a Datensatz (as they are on disk); per year where the path names the year."""
    out = []
    for rel in book_files(book):
        if not _match(rel, d.pfade):
            continue
        if jahr is not None and d.jahresweise and any(str(y) in rel for y in book.years()) and str(jahr) not in rel:
            continue
        out.append((rel, (book.root / rel).read_bytes()))
    return out


# ---------- building ----------

def formats(d: Datensatz) -> tuple[str, ...]:
    return tuple(d.formate) + (("dateien",) if d.pfade else ())


def overview(book: Book, jahr: int | None = None) -> dict:
    sets = datensaetze(book)
    rows = []
    for d in sets:
        years = [jahr] if (d.jahresweise and jahr) else (book.years() if d.jahresweise else [None])
        try:
            counts = [d.anzahl(book, y) if d.anzahl else None for y in years]
            n = None if any(c is None for c in counts) else sum(counts)
        except (BookError, KeyError, ValueError):
            n = None
        rows.append({"key": d.key, "gruppe": d.gruppe, "label": d.label, "beschreibung": d.beschreibung,
                     "formate": list(formats(d)), "jahresweise": d.jahresweise, "anzahl": n,
                     "vertraulich": d.vertraulich})
    order = {g: i for i, g in enumerate(GRUPPEN)}
    rows.sort(key=lambda r: order.get(r["gruppe"], len(GRUPPEN)))
    return {"jahr": jahr, "jahre": book.years(), "datensaetze": rows, "ungesichert": ungesichert(book, sets)}


def _files(book: Book, d: Datensatz, jahr: int | None, fmt: str) -> list[tuple[str, bytes]]:
    if fmt == "dateien":
        return originale(book, d, jahr)
    if fmt not in d.formate:
        raise BookError(f"{d.label}: Format «{fmt}» nicht verfügbar ({', '.join(formats(d))})")
    if not d.jahresweise:
        return d.build(book, None, fmt)
    out = []
    for y in ([jahr] if jahr else book.years()):
        if not jahr and _count(book, d, y) == 0:
            continue                                    # all years: leave out the empty ones
        try:
            files = d.build(book, y, fmt)
        except BookError:
            continue
        out += [(f"{y}/{name}" if not jahr else name, data) for name, data in files]
    return out


def _count(book: Book, d: Datensatz, jahr: int | None) -> int | None:
    try:
        return d.anzahl(book, jahr) if d.anzahl else None
    except (BookError, KeyError, ValueError):
        return None


def _zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def build_one(book: Book, key: str, fmt: str, jahr: int | None = None) -> tuple[str, bytes]:
    """One Datensatz in one format; several files come as a ZIP."""
    d = datensatz(book, key)
    files = _files(book, d, jahr, fmt)
    if not files:
        raise BookError(f"{d.label}{' ' + str(jahr) if jahr else ''}: nichts vorhanden")
    if len(files) == 1:
        return Path(files[0][0]).name, files[0][1]
    suffix = f" {jahr}" if jahr else ""
    return f"{book.settings.firma} {d.label}{suffix} {FORMAT_LABEL.get(fmt, fmt)}.zip", _zip(files)


def build(book: Book, keys: list[str] | None = None, jahr: int | None = None, formate: list[str] | None = None,
          originale_mit: bool = False) -> tuple[str, bytes, dict]:
    """The chosen Datensätze (default all) as one ZIP: «<Gruppe>/<Datensatz>/…», a contents PDF, «Rohdaten» for files
    no Datensatz covers, and manifest.json with the SHA-256 of every file."""
    from . import check as checks
    sets = datensaetze(book)
    if keys:
        unknown = set(keys) - {d.key for d in sets}
        if unknown:
            raise BookError(f"Unbekannte Daten: {', '.join(sorted(unknown))}")
    chosen = [d for d in sets if not keys or d.key in keys]
    formate = formate or ["csv", "pdf"]
    stamp = date.today().isoformat()
    folder = f"{book.settings.firma} Datensicherung {stamp}" + (f" {jahr}" if jahr else "")
    files: list[tuple[str, bytes]] = []
    contents = []
    for d in chosen:
        if not d.jahresweise and _count(book, d, None) == 0 or d.jahresweise and jahr and _count(book, d, jahr) == 0:
            continue                                    # nothing there: no empty files in the ZIP
        names = []
        for fmt in list(d.formate) + (["dateien"] if originale_mit and d.pfade else []):
            if fmt != "dateien" and fmt not in formate:
                continue
            for name, data in _files(book, d, jahr, fmt):
                sub = "Originale/" if fmt == "dateien" else ""
                path = f"{d.gruppe}/{d.label}/{sub}{name}"
                files.append((path, data))
                names.append(path)
        contents.append({"nr": d.gruppe, "label": d.label, "beschreibung": d.beschreibung, "dateien": names})
    loose = ungesichert(book, sets)
    if loose and not keys:
        for rel in loose:
            files.append((f"Rohdaten/{rel}", (book.root / rel).read_bytes()))
        contents.append({"nr": "", "label": "Rohdaten", "beschreibung": "Dateien ohne eigene Aufbereitung, im Original",
                         "dateien": [f"Rohdaten/{rel}" for rel in loose]})
    issues = checks.run(book)
    commit, dirty = dossier._commit(book)
    manifest = {
        "format": "aeradex-datensicherung", "version": 1, "aeradex": __version__,
        "firma": book.settings.firma, "jahr": jahr, "jahre": book.years(),
        "erstellt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commit": commit, "uncommittete_aenderungen": dirty,
        "pruefung": {"fehler": sum(i.level == "fehler" for i in issues),
                     "warnungen": sum(i.level == "warnung" for i in issues)},
        "datensaetze": [d.key for d in chosen],
        "dateien": {name: dossier.sha256(data) for name, data in files},
    }
    cover = _cover(book, contents, manifest)
    files.insert(0, ("00 Inhalt.pdf", cover))
    manifest["dateien"] = {"00 Inhalt.pdf": dossier.sha256(cover), **manifest["dateien"]}
    files.append(("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")))
    return f"{folder}.zip", _zip([(f"{folder}/{n}", d) for n, d in files]), manifest


def _cover(book: Book, contents: list[dict], manifest: dict) -> bytes:
    blocks = [("text", f"Erstellt {manifest['erstellt'][:16].replace('T', ' ')} mit aeradex {manifest['aeradex']}"
                       f"{' · Stand ' + manifest['commit'][:10] if manifest['commit'] else ''}"
                       f" · Prüfung: {manifest['pruefung']['fehler']} Fehler, {manifest['pruefung']['warnungen']} Warnungen."),
              ("small", "CSV-Dateien: Semikolon, UTF-8 — in Excel mit Doppelklick öffnen. Jede Datei ist in manifest.json "
                        "mit ihrer SHA-256-Prüfsumme verzeichnet. Zum Wiederherstellen des ganzen Buchs dient die "
                        ".aeradex-Datei (Einstellungen › Buch).")]
    rows = [[c["nr"], c["label"], str(len(c["dateien"]))] for c in contents]
    blocks.append(("table", ["Bereich", "Daten", "Dateien"], rows, (0.25, 0.6, 0.15), {"right": (2,), "zebra": True}))
    return pdfmod.report_pdf(book, "Datensicherung", blocks,
                             subtitle=f"{book.settings.firma} · {'Jahr ' + str(manifest['jahr']) if manifest['jahr'] else 'alle Jahre'}")

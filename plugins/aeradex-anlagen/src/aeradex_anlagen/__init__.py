"""Anlagenbuchhaltung: Abschreibungen (linear/degressiv, ESTV-Höchstsätze) und Anlagenspiegel."""
from __future__ import annotations

from datetime import date

from aeradex import api
from aeradex.files import parse_date
from aeradex.plugins import Command, Finding, Page, Source, hookimpl

from . import anlagen

AERADEX_PLUGIN_API = 1
__version__ = "0.1.0"


@hookimpl
def aeradex_sources():
    return [Source("abschreibung", "Anlage", anlagen.rows, link=lambda ref: "/p/anlagen/anlagen")]


@hookimpl
def aeradex_check(book, rows):
    """Each fixed-asset account holding assets should equal their book values (at the last year-end)."""
    from aeradex.ledger import BalanceEngine
    items = anlagen.assets(book)
    if not items or not book.years():
        return []
    year = max(book.years())
    sched = anlagen.schedule(book, year)
    per = {}
    for line in sched["anlagen"]:
        per[line["konto"]] = per.get(line["konto"], anlagen.ZERO) + line["ende"]
    eng = BalanceEngine(book)
    out = []
    for konto, total in sorted(per.items()):
        if konto not in book.accounts:
            out.append(Finding("warnung", "anlagen", f"Konto {konto} fehlt im Kontenplan"))
            continue
        saldo = eng.balance(konto, year)
        if saldo != total:
            out.append(Finding("hinweis", "anlagen", f"Konto {konto}: Saldo {saldo:.2f}, Anlagen {total:.2f} — "
                               "eine Anschaffung ohne Anlage erfasst, oder umgekehrt?"))
    open_ = [l["nummer"] for l in sched["anlagen"] if l["offen"]]
    if open_ and date.today() > date(year, 12, 31):
        out.append(Finding("hinweis", "anlagen", f"Abschreibungen {year} noch nicht gebucht ({', '.join(open_[:5])})"))
    return out


# ---------- agent ----------

def fixed_assets(jahr: int = 0) -> dict:
    """Anlagenspiegel: Anlagen mit Anfangsbestand, Zugang, Abschreibung, Abgang und Endbestand im Jahr.

    Args:
        jahr: Geschäftsjahr (0 = letztes Jahr mit Buchungen).
    """
    from aeradex.tools import call
    return call(lambda b: api.jsonable(anlagen.schedule(b, jahr or max(b.years()))))


@hookimpl
def aeradex_tools():
    return [fixed_assets]


@hookimpl
def aeradex_instructions():
    return ("- Anlagen (Plugin anlagen): Anschaffungen auf Sachanlagekonten (15xx) gehören in die Anlagenbuchhaltung; "
            "fixed_assets() zeigt den Anlagenspiegel. Abschreibungen bucht ein Mensch (Plugins → Anlagen).")


# ---------- CLI ----------

def _run(book, a):
    if a.aktion == "list":
        return anlagen.schedule(book, a.jahr or max(book.years()))
    if a.aktion == "add":
        return api.write(book, f"Anlage {a.bezeichnung} erfasst", anlagen.add, a.bezeichnung, a.datum, a.wert,
                         a.kategorie, a.konto, a.methode, a.satz)
    if a.aktion == "abschreiben":
        return api.write(book, f"Abschreibungen {a.jahr} gebucht", anlagen.depreciate, a.jahr)
    if a.aktion == "abgang":
        return api.write(book, f"Abgang {a.nummer}", anlagen.dispose, a.nummer, a.datum, a.wert or 0)


def _setup(p):
    p.add_argument("aktion", choices=["list", "add", "abschreiben", "abgang"])
    p.add_argument("--jahr", type=int, default=0)
    p.add_argument("--bezeichnung", default="")
    p.add_argument("--datum")
    p.add_argument("--wert", help="add: Anschaffungswert · abgang: Verkaufserlös")
    p.add_argument("--kategorie", default="", choices=[""] + list(anlagen.KATEGORIEN))
    p.add_argument("--konto", default="")
    p.add_argument("--methode", default="degressiv", choices=["degressiv", "linear"])
    p.add_argument("--satz")
    p.add_argument("--nummer", default="")


@hookimpl
def aeradex_commands():
    return [Command("anlagen", "Anlagenbuchhaltung: Anlagen, Abschreibungen, Anlagenspiegel", _run, _setup)]


# ---------- page ----------

def _context(book, query):
    years = book.years() or [date.today().year]
    year = int(query.get("jahr") or max(years))
    return {"jahr": year, "jahre": years, "spiegel": anlagen.schedule(book, year),
            "vorschau": anlagen.preview(book, year), "kategorien": anlagen.KATEGORIEN,
            "konten_aktiv": [a for a in book.accounts.values() if a.klasse == "aktiv" and a.nr[:2] in ("15", "16", "17")]}


def _add(book, form):
    return api.write(book, f"Anlage {form.get('bezeichnung')} erfasst", anlagen.add, form.get("bezeichnung") or "",
                     form.get("datum"), form.get("wert"), form.get("kategorie") or "", form.get("konto") or "",
                     form.get("methode") or "degressiv", form.get("satz") or None)


def _depreciate(book, form):
    return api.write(book, f"Abschreibungen {form.get('jahr')} gebucht", anlagen.depreciate, int(form.get("jahr")))


def _dispose(book, form):
    return api.write(book, f"Abgang {form.get('nummer')}", anlagen.dispose, form.get("nummer"), form.get("datum"),
                     form.get("erloes") or 0)


@hookimpl
def aeradex_pages():
    return [Page("anlagen", "Anlagen", "anlagen.html", _context,
                 {"erfassen": _add, "abschreiben": _depreciate, "abgang": _dispose})]

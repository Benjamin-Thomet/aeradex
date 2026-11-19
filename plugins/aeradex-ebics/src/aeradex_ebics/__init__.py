"""EBICS: Kontoauszüge automatisch von der Bank abholen (EBICS 3.0, camt.053)."""
from __future__ import annotations

import datetime as dt

from aeradex.plugins import Antwort, Command, Endpunkt, Page, Todo, hookimpl

from . import ebics

AERADEX_PLUGIN_API = 1


def _date(text):
    return dt.date.fromisoformat(text) if text else None


def _kurz(result: dict) -> str:
    imp, err = result.get("importe", []), result.get("fehler", [])
    if not result.get("dateien") and not imp and not err:
        return "Keine neuen Kontoauszüge bei der Bank"
    text = f"{result.get('dateien', 0)} Datei(en) abgeholt, {len(imp)} importiert"
    return text + (f", {len(err)} Fehler: " + "; ".join(err) if err else "")


# ---------- CLI ----------

def _run(book, a):
    if a.aktion == "status":
        st = ebics.status(book)
        cfg = st["cfg"]
        return {"schritt": st["schritt"], "bank": cfg.get("bank", ""), "host_id": cfg.get("host_id", ""),
                "partner_id": cfg.get("partner_id", ""), "user_id": cfg.get("user_id", ""),
                "ini_gesendet": cfg.get("ini_gesendet"), "hia_gesendet": cfg.get("hia_gesendet"),
                "bankschluessel": cfg.get("bankschluessel"), "bestaetigt": cfg.get("bestaetigt"),
                "letzter_abruf": cfg.get("letzter_abruf"), "letzter_fehler": cfg.get("letzter_fehler"),
                "eigene_hashes": st["eigene"], "bank_hashes": st["vergleich"], "wartet": st["offen"]}
    if a.aktion == "einrichten":
        erwartet = {k: getattr(a, k) for k in ebics.HASHES if getattr(a, k)}
        return ebics.einrichten(book, a.url, a.host, a.partner, a.teilnehmer, a.bank, erwartet)["schritt"]
    if a.aktion == "hev":
        return ebics.client(book).hev()
    if a.aktion == "senden":
        if not a.ja:
            raise ebics.BookError("INI und HIA gehen nur einmal an die Bank — mit --ja bestätigen")
        return ebics.senden(book)["schritt"]
    if a.aktion == "brief":
        from . import brief
        target = a.pdf or "ebics-initialisierungsbrief.pdf"
        with open(target, "wb") as fh:
            fh.write(brief.pdf(book))
        return f"Brief gespeichert: {target}"
    if a.aktion == "bankschluessel":
        return ebics.bankschluessel(book)["vergleich"]
    if a.aktion == "bestaetigen":
        return ebics.bestaetigen(book, a.von_wem)["schritt"]
    if a.aktion == "abholen":
        from aeradex import gitlog
        gitlog.AUTHOR.set(ebics.AUTHOR)
        if a.still and ebics.status(book)["schritt"] != "aktiv":
            return None                     # the timer runs before the setup is done: nothing to say
        result = ebics.abholen(book, _date(a.von), _date(a.bis))
        if a.still and not result.get("dateien") and not result.get("fehler"):
            return None
        return _kurz(result)
    if a.aktion == "importieren":
        return ebics.importieren(book)
    if a.aktion == "protokoll":
        return ebics.verlauf(book, 50)
    if a.aktion == "neu":
        if not a.ja:
            raise ebics.BookError("Verwirft Schlüssel und Stand (die Bank muss den Teilnehmer zurücksetzen) — mit --ja")
        return ebics.neu_beginnen(book)["schritt"]


def _setup(p):
    p.add_argument("aktion", choices=["status", "einrichten", "hev", "senden", "brief", "bankschluessel",
                                      "bestaetigen", "abholen", "importieren", "protokoll", "neu"])
    p.add_argument("--url", default="", help="einrichten: EBICS-Adresse der Bank (https://…)")
    p.add_argument("--host", default="", help="einrichten: Host-ID")
    p.add_argument("--partner", default="", help="einrichten: Partner-ID (Kunden-ID)")
    p.add_argument("--teilnehmer", default="", help="einrichten: Teilnehmer-ID (User-ID)")
    p.add_argument("--bank", default="", help="einrichten: Name der Bank für den Brief")
    for key in ebics.HASHES:
        p.add_argument(f"--{key.replace('_', '-')}", dest=key, default="",
                       help=f"einrichten: Hashwert {key} aus dem Brief der Bank")
    p.add_argument("--pdf", default="", help="brief: Zieldatei")
    p.add_argument("--von", default="", help="abholen: ab Datum JJJJ-MM-TT (sonst: alles Neue)")
    p.add_argument("--bis", default="")
    p.add_argument("--von-wem", dest="von_wem", default="", help="bestaetigen: wer verglichen hat")
    p.add_argument("--still", action="store_true", help="abholen: nichts ausgeben, wenn nichts Neues da ist (Timer)")
    p.add_argument("--ja", action="store_true")


@hookimpl
def aeradex_commands():
    return [Command("ebics", "EBICS: Kontoauszüge automatisch von der Bank abholen", _run, _setup)]


# ---------- page ----------

def _context(book, query):
    st = ebics.status(book)
    return {**st, "verlauf": ebics.verlauf(book, 30), "schritte": ebics.SCHRITTE, "hashes": ebics.HASHES,
            "buchbeginn": ebics.buchbeginn(book), "heute": dt.date.today().isoformat()}


def _einrichten(book, form):
    erwartet = {k: form.get(k, "") for k in ebics.HASHES}
    ebics.einrichten(book, form.get("url", ""), form.get("host_id", ""), form.get("partner_id", ""),
                     form.get("user_id", ""), form.get("bank", ""), erwartet)
    return {"meldung": "Gespeichert, Schlüssel erzeugt"}


def _senden(book, form):
    ebics.senden(book)
    return {"meldung": "Schlüssel an die Bank gesendet — jetzt den Brief drucken, unterschreiben und schicken"}


def _bankschluessel(book, form):
    st = ebics.bankschluessel(book)
    return {"meldung": "Bankschlüssel geholt und bestätigt" if st["schritt"] == "aktiv" else "Bankschlüssel geholt — bitte vergleichen"}


def _bestaetigen(book, form):
    from aeradex.web import auth
    user = auth.benutzer()
    ebics.bestaetigen(book, getattr(user, "name", "") or "")
    return {"meldung": "Bankschlüssel bestätigt — EBICS ist aktiv"}


def _abholen(book, form):
    return {"meldung": _kurz(ebics.abholen(book))}


def _zeitraum(book, form):
    von, bis = _date(form.get("von")), _date(form.get("bis"))
    if not von or not bis or von > bis:
        raise ValueError("Zeitraum: von und bis angeben (von vor bis)")
    return {"meldung": _kurz(ebics.abholen(book, von, bis))}


def _neu(book, form):
    ebics.neu_beginnen(book)
    return {"meldung": "Zurückgesetzt"}


@hookimpl
def aeradex_pages():
    return [Page("ebics", "EBICS", "ebics.html", _context,
                 {"einrichten": _einrichten, "senden": _senden, "bankschluessel": _bankschluessel,
                  "bestaetigen": _bestaetigen, "abholen": _abholen, "zeitraum": _zeitraum, "neu": _neu},
                 bereich="bank", rollen=("admin",))]


def _brief(book, query):
    from . import brief
    return Antwort(brief.pdf(book), "application/pdf",
                   headers={"Content-Disposition": 'inline; filename="ebics-initialisierungsbrief.pdf"'})


@hookimpl
def aeradex_endpunkte():
    return [Endpunkt("brief", _brief, rollen=("admin",))]


@hookimpl
def aeradex_todos(book):
    try:
        st = ebics.status(book)
    except Exception:                                   # unreadable keys etc.: the page says why
        return [Todo("EBICS: Schlüssel prüfen", "/p/ebics/ebics", level="red")]
    cfg = st["cfg"]
    text = {"senden": "EBICS: Schlüssel an die Bank senden", "freischaltung": "EBICS: Bankschlüssel holen (nach Freischaltung)",
            "pruefen": "EBICS: Bankschlüssel bestätigen"}.get(st["schritt"])
    if text and cfg.get("url"):
        return [Todo(text, "/p/ebics/ebics", tag="Bank")]
    if cfg.get("letzter_fehler"):
        return [Todo("EBICS: Abholen fehlgeschlagen", "/p/ebics/ebics", detail=cfg["letzter_fehler"]["text"],
                     tag="Bank", level="warn")]
    if st["offen"]:
        return [Todo("EBICS: Auszüge warten auf den Import", "/p/ebics/ebics", detail=f"{len(st['offen'])} Datei(en)",
                     tag="Bank", level="warn")]
    return []


# ---------- agent ----------

def ebics_status() -> dict:
    """Stand der EBICS-Verbindung zur Bank: Schritt der Einrichtung, letzter Abruf, letzter Fehler."""
    from aeradex.tools import call

    def run(b):
        st = ebics.status(b)
        cfg = st["cfg"]
        return {"schritt": st["schritt"], "bank": cfg.get("bank", ""), "letzter_abruf": cfg.get("letzter_abruf"),
                "letzter_fehler": cfg.get("letzter_fehler"), "wartet_auf_import": st["offen"]}
    return call(run)


def ebics_abholen() -> dict:
    """Neue Kontoauszüge (camt.053) jetzt per EBICS bei der Bank abholen und importieren. Läuft sonst täglich
    von selbst; nur nötig, wenn jemand die neuesten Bewegungen sofort braucht."""
    from aeradex.tools import call
    return call(lambda b: {"ergebnis": _kurz(ebics.abholen(b))})


@hookimpl
def aeradex_tools():
    return [ebics_status, ebics_abholen]


@hookimpl
def aeradex_instructions():
    return ("- EBICS (Plugin ebics): Die Kontoauszüge kommen automatisch von der Bank (täglich). Fehlen die neuesten "
            "Bewegungen, ebics_abholen(); ebics_status() zeigt den Stand. Die Einrichtung (Schlüssel, Brief an die "
            "Bank) macht ein Mensch unter Bank › EBICS.")

"""Team: the employees' phone app (aeradex-team-portal) connected to this book.

Entries recorded in the app (time, material, absences) are taken over into aeradex-leistungen; employees see their
own overtime, holiday balance and — end-to-end encrypted — their payslips. The book never leaves its computer
or server: the portal is only a mailbox that this plugin empties and fills whenever it runs (`aeradex team sync`).
"""
from __future__ import annotations

import time

from aeradex import api
from aeradex.book import Book, BookError
from aeradex.plugins import Command, Dienst, Finding, hookimpl

from . import abgleich, daten, seiten, verbindung

AERADEX_PLUGIN_API = 1
__version__ = "0.1.0"


@hookimpl
def aeradex_check(book, rows):
    try:
        accounts = daten.accounts(book)
        reqs = daten.requests(book)
    except (BookError, KeyError, ValueError) as exc:
        return [Finding("fehler", "team", f"Team-Dateien lassen sich nicht lesen: {exc}")]
    from aeradex_leistungen import daten as leistungen
    people = leistungen.people(book)
    out = [Finding("warnung", "team", f"App-Konto {nr}: Person unbekannt") for nr in accounts if nr not in people]
    users = [a["benutzer"] for a in accounts.values()]
    out += [Finding("fehler", "team", f"Benutzername {u} doppelt") for u in sorted(set(users)) if users.count(u) > 1]
    open_ = [r for r in reqs if r["status"] == "offen"]
    if open_:
        out.append(Finding("hinweis", "team", f"{len(open_)} Abwesenheits-Antrag/Anträge aus der App warten (Lohn › Team-App)"))
    return out


# ---------- agent ----------

def team_status() -> dict:
    """Zustand der Team-App: letzter Abgleich, Fehler, Personen mit App-Konto, offene Anträge."""
    from aeradex.tools import call

    def run(b):
        st = verbindung.state(b)
        return api.jsonable({"ok": True, "portal": verbindung.config()["url"], "letzter_abgleich": st.get("letzter_abgleich"),
                             "fehler": st.get("fehler"), "konten": daten.accounts(b),
                             "offene_antraege": [r for r in daten.requests(b) if r["status"] == "offen"]})
    return call(run)


def team_requests(status: str = "offen") -> dict:
    """Abwesenheits-Anträge aus der Team-App (Ferien, Kompensation, Unbezahlt, Weiterbildung).

    Args:
        status: offen, bewilligt, abgelehnt oder alle.
    """
    from aeradex.tools import call
    return call(lambda b: api.jsonable({"ok": True, "antraege": [
        r for r in daten.requests(b) if status == "alle" or r["status"] == status]}))


def decide_request(antrag: str, bewilligen: bool, grund: str = "") -> dict:
    """Einen Antrag aus der Team-App bewilligen (trägt die Abwesenheit ein) oder ablehnen (Grund nötig).
    Nur auf ausdrückliche Anweisung der Inhaberin verwenden.

    Args:
        antrag: Nummer, z.B. ANT-0003.
        bewilligen: true = bewilligen, false = ablehnen.
        grund: bei Ablehnung Pflicht; erscheint in der App.
    """
    from aeradex.tools import call
    return call(lambda b: api.write(b, f"Team: Antrag {antrag} {'bewilligt' if bewilligen else 'abgelehnt'}",
                                    daten.decide, antrag, bewilligen, grund))


def team_sync() -> dict:
    """Team-App jetzt abgleichen: Einträge der Mitarbeitenden übernehmen, Salden und Lohndokumente hochladen."""
    from aeradex.tools import call
    return call(lambda b: api.jsonable({"ok": True, **abgleich.run(b)}))


@hookimpl
def aeradex_tools():
    return [team_status, team_requests, decide_request, team_sync]


@hookimpl
def aeradex_instructions():
    return ("Team-App: Mitarbeitende erfassen Zeit, Material und Abwesenheiten auf dem Handy; `team_sync` übernimmt "
            "sie in die Leistungen. Ferien-Anträge stehen in `team_requests`; entscheiden (`decide_request`) nur auf "
            "Anweisung der Inhaberin. Übernommene Einträge sind normale Leistungen (Autor «… (App)» im Verlauf).")


# ---------- CLI ----------

def _run(book, args):
    if args.team_cmd == "verbinden":
        path = verbindung.save_config(args.url, args.token)
        return {"ok": True, "gespeichert": str(path)}
    if args.team_cmd == "sync":
        if not args.wiederholen:
            try:
                return abgleich.run(book)
            except verbindung.PortalError as exc:
                raise BookError(str(exc)) from None
        # as a service: sync at once when the book changed or entries wait on the portal (checked every
        # `--pruefen` seconds, cheaply), at the latest every `--wiederholen` seconds; never stop on a network error
        last_head, last_run = None, 0.0
        while True:
            head = abgleich.book_head(book)
            due = time.monotonic() - last_run >= args.wiederholen or head != last_head
            if not due:
                try:
                    due = abgleich.waiting(verbindung.transport()) > 0
                except (verbindung.PortalError, BookError):
                    due = False
            if due:
                try:
                    out = abgleich.run(Book(book.root))
                    if not args.still:
                        print(f"Abgeglichen: {out['uebernommen']} übernommen, {len(out['abgelehnt'])} abgelehnt, "
                              f"{out['hochgeladen']} Dokument(e) hochgeladen", flush=True)
                except (verbindung.PortalError, BookError) as exc:
                    print(f"team sync: {exc}", flush=True)
                last_run, last_head = time.monotonic(), abgleich.book_head(book)
            time.sleep(min(args.pruefen, args.wiederholen))
    if args.team_cmd == "konto":
        return api.write(book, f"Team: App-Konto {args.person}", daten.set_account, args.person, args.benutzer or "",
                         not args.sperren)
    if args.team_cmd == "einladen":
        out = abgleich.invite(book, args.person)
        out["adresse"] = f"{verbindung.config()['url'] or 'https://<portal>'}/app"
        if not out["zugestellt"]:
            out["hinweis"] = "Portal nicht erreicht: der Code gilt erst nach dem nächsten Abgleich"
        return out
    if args.team_cmd == "antraege":
        return api.jsonable([r for r in daten.requests(book) if args.alle or r["status"] == "offen"])
    if args.team_cmd == "entscheiden":
        return api.write(book, f"Team: Antrag {args.antrag}", daten.decide, args.antrag, args.bewilligen, args.grund or "")
    if args.team_cmd == "status":
        st = verbindung.state(book)
        return api.jsonable({"portal": verbindung.config()["url"], **st, "konten": daten.accounts(book)})
    return None


def _setup(parser):
    sub = parser.add_subparsers(dest="team_cmd", required=True)
    s = sub.add_parser("verbinden", help="Portal-Adresse und Sync-Token speichern (~/.config/aeradex/team.yaml)")
    s.add_argument("--url", required=True)
    s.add_argument("--token", required=True)
    s = sub.add_parser("sync", help="Abgleichen: Einträge übernehmen, Salden und Dokumente hochladen")
    s.add_argument("--wiederholen", type=int, default=0, metavar="SEK", help="immer wieder, spätestens alle SEK Sekunden")
    s.add_argument("--pruefen", type=int, default=15, metavar="SEK",
                   help="mit --wiederholen: so oft nachsehen, ob das Buch geändert hat oder Einträge warten")
    s.add_argument("--still", action="store_true", help="nur Fehler ausgeben")
    s = sub.add_parser("konto", help="App-Konto für eine Person anlegen, ändern oder sperren")
    s.add_argument("person")
    s.add_argument("--benutzer")
    s.add_argument("--sperren", action="store_true")
    s = sub.add_parser("einladen", help="Einmal-Code für die Aktivierung (setzt ein bestehendes Konto zurück)")
    s.add_argument("person")
    s = sub.add_parser("antraege", help="Abwesenheits-Anträge")
    s.add_argument("--alle", action="store_true")
    s = sub.add_parser("entscheiden", help="Antrag bewilligen oder ablehnen")
    s.add_argument("antrag")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--bewilligen", action="store_true")
    g.add_argument("--ablehnen", dest="bewilligen", action="store_false")
    s.add_argument("--grund")
    sub.add_parser("status", help="Zustand des Abgleichs")


@hookimpl
def aeradex_commands():
    return [Command("team", "Team-App (Mitarbeiter-Portal): abgleichen, Konten, Anträge", _run, _setup)]


@hookimpl
def aeradex_pages():
    return seiten.PAGES


@hookimpl
def aeradex_dienste(book):
    # while the local UI is open: changes reach the app within seconds (the scheduled task covers the time it is closed)
    if not verbindung.config()["url"]:
        return []
    return [Dienst("team-abgleich", ["team", "sync", "--wiederholen", "300", "--pruefen", "15", "--still"])]

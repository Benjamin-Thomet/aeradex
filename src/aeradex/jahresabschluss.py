"""Jahresabschluss: the checklist of a year's closing, and the closing entries for stocks and private shares.

    abschluss/<JJJJ>/checkliste.yaml          steps a person ticked off: {key: {am, von, notiz}}
    abschluss/<JJJJ>/abschlussbuchungen.yaml  what was counted or estimated per 31.12. and the entries it booked

Stocks (`BESTAENDE`): angefangene Arbeiten (1280), Materialvorrat (1210), Delkredere (1109),
Ferien- und Überstundenguthaben (2331). Each sets its account to the value per 31.12. with one entry
for the difference against the P&L account. That works with an opening balance and from year to year
without reversals: next year's value again books only the change. Privatanteile (Fahrzeug, Telefon,
Raum …) of an Einzelfirma are booked Privat an Aufwand. Every entry is a row of this document
(Quelle jahresabschluss:<JJJJ>:<art>), regenerated from the yaml, so `check` notices any change.

The checklist collects the steps of the closing in working order. A step's status comes from the
books where aeradex can tell (a booking exists, nothing is open); a person can tick off any step
(«kein Vorrat», «Saldenliste durchgesehen»). Plugins add steps (`aeradex_abschluss_schritte`) and
proposals for the values (`aeradex_abschluss_vorschlaege`)."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .book import Account, Book, BookError, Row
from .files import CENT, parse_amount, read_yaml, write_yaml

ZERO = Decimal("0")
PREFIX = "jahresabschluss"

# art → account, its counter account on the result, and how the value reads on the account
# (+1: a debit balance like an asset, -1: a credit balance like a provision or the Delkredere).
BESTAENDE = {
    "arbeiten": {"titel": "Angefangene Arbeiten", "konto": "1280", "name": "Nicht fakturierte Dienstleistungen",
                 "klasse": "aktiv", "gegen": "3940", "gegen_name": "Bestandesänderungen nicht fakturierte Dienstleistungen",
                 "gegen_klasse": "ertrag", "seite": 1,
                 "text": "Begonnene, noch nicht schlussverrechnete Aufträge per 31.12., zu Herstellkosten (Löhne, Material, "
                         "ohne Gewinn). Erhaltene Akontozahlungen bleiben auf «Erhaltene Anzahlungen»."},
    "material": {"titel": "Materialvorrat", "konto": "1210", "name": "Materialvorrat", "klasse": "aktiv",
                 "gegen": "4000", "gegen_name": "Materialaufwand", "gegen_klasse": "aufwand", "seite": 1,
                 "text": "Farbe, Material und Verbrauchsmaterial am Lager per 31.12., gezählt und zum Einstandspreis "
                         "(ohne MWST) bewertet."},
    "delkredere": {"titel": "Delkredere", "konto": "1109", "name": "Delkredere", "klasse": "aktiv",
                   "gegen": "3805", "gegen_name": "Verluste aus Forderungen", "gegen_klasse": "ertrag", "seite": -1,
                   "text": "Wertberichtigung auf den offenen Kundenrechnungen per 31.12. Steuerlich ohne Nachweis "
                           "anerkannt: 5 % der Inland-, 10 % der Auslandforderungen; gefährdete Posten einzeln."},
    "ferien": {"titel": "Ferien- und Überstundenguthaben", "konto": "2331",
               "name": "Rückstellung Ferien- und Überstundenguthaben", "klasse": "passiv",
               "gegen": "5000", "gegen_name": "Lohnaufwand", "gegen_klasse": "aufwand", "seite": -1,
               "text": "Nicht bezogene Ferien und Überstunden der Mitarbeitenden per 31.12., bewertet zum Lohn "
                       "(bei Stundenlohn mit Ferienzuschlag nur die Überstunden)."},
}
PRIVAT = "privatanteile"
ARTEN = (*BESTAENDE, PRIVAT)
PRIVAT_ARTEN = {"fahrzeug": ("Fahrzeug", "6270"), "telefon": ("Telefon, Internet", "6512"),
                "raum": ("Raum, Energie", "6000"), "andere": ("Andere", "")}
FAHRZEUG_SATZ = Decimal("0.009")          # 0.9 % of the purchase price (excl. MWST) per month (ESTV MI 08)
FAHRZEUG_MINIMUM = Decimal("150")         # at least CHF 150 per month

PHASEN = [("laufend", "Laufendes vervollständigen"), ("bewerten", "Bestände bewerten"),
          ("buchen", "Abschlussbuchungen"), ("kontrolle", "Kontrolle"), ("abschliessen", "Abschliessen")]


def money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def folder(book: Book, year: int) -> Path:
    return book.root / "abschluss" / str(int(year))


def path(book: Book, year: int) -> Path:
    return folder(book, year) / "abschlussbuchungen.yaml"


def saved(book: Book, year: int) -> dict:
    p = path(book, year)
    return (read_yaml(p) or {}) if p.exists() else {}


def _years(book: Book) -> list[int]:
    base = book.root / "abschluss"
    return sorted(int(p.name) for p in base.iterdir() if p.name.isdigit() and (p / "abschlussbuchungen.yaml").exists()) \
        if base.exists() else []


def konto(book: Book, art: str, role: str = "konto") -> str:
    """aeradex.yaml konten.abschluss_<art> (or _<art>_gegen) overrides the Swiss default."""
    key = f"abschluss_{art}" + ("_gegen" if role == "gegen" else "")
    return str((book.settings.get("konten") or {}).get(key) or BESTAENDE[art][role])


def privat_konto(book: Book) -> str:
    """Where private shares go: konten.privat, else 2850, else the first account closing into equity."""
    override = (book.settings.get("konten") or {}).get("privat")
    if override:
        return str(override)
    if "2850" in book.accounts:
        return "2850"
    eq = book.settings.konto("gewinnvortrag")
    found = next((a.nr for a in sorted(book.accounts.values(), key=lambda a: a.nr) if a.abschluss == eq), "")
    if not found:
        raise BookError("Kein Privatkonto im Kontenplan (2850 oder ein Konto mit Abschluss ins Eigenkapital)")
    return found


def _ensure_accounts(book: Book, art: str) -> list[Path]:
    """The accounts of a stock, created with the Kontenrahmen KMU name when the chart lacks them."""
    from .statements import default_group
    spec, added = BESTAENDE[art], False
    for role, name, klasse in (("konto", spec["name"], spec["klasse"]), ("gegen", spec["gegen_name"], spec["gegen_klasse"])):
        nr = konto(book, art, role)
        if nr not in book.accounts:
            book.accounts[nr] = Account(nr=nr, name=name, klasse=klasse, gruppe=default_group(nr, klasse))
            added = True
    if added:
        book.save_accounts()
        return [book.root / "kontenplan.yaml"]
    return []


def quelle(year: int, art: str) -> str:
    return f"{PREFIX}:{int(year)}:{art}"


# ---------- rows (the document's journal rows, for check) ----------

def _entry_rows(book: Book, year: int, art: str, entry: dict) -> list[Row]:
    from .mwst import split
    end, q = date(int(year), 12, 31), quelle(year, art)
    if art == PRIVAT:
        out = []
        privat = entry.get("privatkonto") or privat_konto(book)
        for p in entry.get("positionen") or []:
            amount = money(p.get("betrag"))
            if amount > 0:
                row = Row(end, entry["beleg"], f"Privatanteil {year}: {p['text']}", privat, str(p["konto"]), amount, q)
                out += split(book, row, str(p.get("mwst") or ""))
        return out
    b = entry.get("buchung") or {}
    amount = money(b.get("betrag"))
    if amount <= 0:
        return []
    spec = BESTAENDE[art]
    text = f"{spec['titel']} 31.12.{year}: {money(entry.get('wert')):,.2f}".replace(",", "'")
    return [Row(end, entry["beleg"], text, str(b["soll"]), str(b["haben"]), amount, q)]


def rows(book: Book) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for year in _years(book):
        data = saved(book, year)
        for art, entry in (data.get("buchungen") or {}).items():
            got = _entry_rows(book, year, art, entry)
            if got:
                out[quelle(year, art)] = got
    return out


# ---------- stocks ----------

def _own_effect(book: Book, year: int, art: str, nr: str) -> Decimal:
    """What this document's own entry moves on account nr (debit positive)."""
    q = quelle(year, art)
    return sum(((r.betrag if r.soll == nr else ZERO) - (r.betrag if r.haben == nr else ZERO)
                for r in book.rows if r.quelle == q), ZERO)


def balance_before(book: Book, year: int, art: str) -> Decimal:
    """The account per 31.12. without this closing entry, as the value reads (asset +, provision +)."""
    from .ledger import BalanceEngine
    nr = konto(book, art)
    if nr not in book.accounts:
        return ZERO
    raw = BalanceEngine(book).balance_at(nr, date(int(year), 12, 31)) - _own_effect(book, year, art, nr)
    return raw * BESTAENDE[art]["seite"]


def _difference(book: Book, year: int, art: str, value: Decimal) -> dict:
    """The entry that brings the account from its balance to `value`."""
    diff = value - balance_before(book, year, art)
    nr, gegen = konto(book, art), konto(book, art, "gegen")
    up = (diff > 0) == (BESTAENDE[art]["seite"] > 0)      # the account's debit grows
    soll, haben = (nr, gegen) if up else (gegen, nr)
    return {"soll": soll, "haben": haben, "betrag": abs(diff), "differenz": diff}


def proposals(book: Book, year: int, art: str) -> list[dict]:
    """Values to start from: from the books (Delkredere) and from the plugins (open work, holidays, vehicles)."""
    from . import plugins
    out = []
    if art == "delkredere":
        from .ledger import BalanceEngine
        nr = book.settings.konto("debitoren")
        if nr in book.accounts:
            open_ = BalanceEngine(book).balance_at(nr, date(int(year), 12, 31))
            if open_ > 0:
                out.append({"text": f"5 % der offenen Kundenrechnungen ({open_:,.2f})".replace(",", "'"),
                            "betrag": money(open_ * Decimal("0.05")), "detail": f"Saldo {nr} per 31.12.{year}"})
    if art == "arbeiten":
        from . import invoices
        for a in invoices.open_akonto(book):
            if a["datum"] <= f"{year}-12-31":
                out.append({"text": f"{a['name']}: Akonto {a['nummer']}", "betrag": None, "info": True,
                            "detail": f"Akonto netto {money(a['netto']):,.2f} erhalten, Schlussrechnung offen — "
                                      "Auftrag läuft noch?".replace(",", "'")})
    out += plugins.abschluss_vorschlaege(book, int(year), art)
    return out


def _positions(raw: list[dict], with_account: bool) -> list[dict]:
    out = []
    for i, p in enumerate(raw or [], 1):
        text = str(p.get("text") or "").strip()
        if not text and p.get("betrag") in (None, ""):
            continue
        amount = money(parse_amount(p.get("betrag"), f"Zeile {i}: Betrag"))
        if not text:
            raise BookError(f"Zeile {i}: Text fehlt")
        if amount < 0:
            raise BookError(f"Zeile {i}: Betrag darf nicht negativ sein")
        item = {"text": text, "betrag": f"{amount:.2f}"}
        if with_account:
            item["art"] = str(p.get("art") or "andere")
            item["konto"] = str(p.get("konto") or "").strip() or PRIVAT_ARTEN.get(item["art"], ("", ""))[1]
            if not item["konto"]:
                raise BookError(f"Zeile {i}: Konto fehlt")
            if p.get("mwst"):
                item["mwst"] = str(p["mwst"]).upper()
            if p.get("grundlage"):
                item["grundlage"] = str(p["grundlage"])
        out.append(item)
    return out


def view(book: Book, year: int, art: str) -> dict:
    """Everything the page of one closing entry shows."""
    if art not in ARTEN:
        raise BookError(f"Unbekannte Abschlussbuchung {art} ({', '.join(ARTEN)})")
    year = int(year)
    entry = (saved(book, year).get("buchungen") or {}).get(art)
    out = {"jahr": year, "art": art, "gebucht": entry, "vorschlaege": proposals(book, year, art)}
    if art == PRIVAT:
        try:
            privat = privat_konto(book)
        except BookError:
            privat = ""
        out.update(titel="Privatanteile", privatkonto=privat, arten=PRIVAT_ARTEN,
                   text="Private Nutzung von Geschäftsaufwand durch die Inhaberin bzw. den Inhaber: Fahrzeug "
                        "(0.9 % des Kaufpreises ohne MWST pro Monat, mindestens CHF 150), Telefon, Raum. "
                        "Gebucht Privat an Aufwand.",
                   mwst_hinweis=_vat_hint(book, entry))
        return out
    spec = BESTAENDE[art]
    before = balance_before(book, year, art)
    value = money((entry or {}).get("wert")) if entry else None
    out.update(titel=spec["titel"], text=spec["text"], konto=konto(book, art), gegen=konto(book, art, "gegen"),
               konto_name=(book.accounts.get(konto(book, art)) or type("A", (), {"name": spec["name"]})).name,
               saldo=before, wert=value,
               veraltet=bool(entry) and money((entry.get("buchung") or {}).get("betrag")) != _difference(book, year, art, value)["betrag"])
    return out


def _vat_hint(book: Book, entry: dict | None) -> str:
    from . import mwst
    if mwst.config(book)["methode"] != "effektiv" or not entry:
        return ""
    plain = [p for p in entry.get("positionen") or [] if not p.get("mwst")]
    tax = sum((money(Decimal(p["betrag"]) * Decimal("8.1") / Decimal("108.1")) for p in plain), ZERO)
    if not tax:
        return ""
    return (f"MWST: Auf den Privatanteilen ohne Code liegt eine Vorsteuerkorrektur (Eigenverbrauch) von etwa "
            f"CHF {tax:,.2f}. Sie gehört in die Abrechnung des letzten Quartals, Ziffer 415 (von Hand im ePortal) — "
            "oder den Code U81 setzen, dann ist sie als Leistung in Ziffer 303 abgerechnet.").replace(",", "'")


def book_entry(book: Book, year: int, art: str, positionen: list[dict], wert=None, notiz: str = "",
               von: str = "") -> tuple[dict, list[Path]]:
    """Book (or book anew) one closing entry per 31.12.: a stock to its value, or the private shares."""
    from .journal import ensure_open, next_beleg, post
    if art not in ARTEN:
        raise BookError(f"Unbekannte Abschlussbuchung {art} ({', '.join(ARTEN)})")
    year = int(year)
    end = date(year, 12, 31)
    ensure_open(book, end)
    data = saved(book, year)
    entries = data.setdefault("buchungen", {})
    old = entries.get(art) or {}
    touched: list[Path] = []
    pos = _positions(positionen, art == PRIVAT)
    entry = {"datum": end.isoformat(), "beleg": old.get("beleg") or "", "positionen": pos,
             "gebucht_am": date.today().isoformat()}
    if von:
        entry["von"] = von
    if notiz.strip():
        entry["notiz"] = notiz.strip()
    if art == PRIVAT:
        if book.settings.rechtsform_art != "einzelfirma":
            raise BookError("Privatanteile gibt es nur bei einer Einzelfirma")
        if not pos:
            raise BookError("Keine Privatanteile erfasst")
        for p in pos:
            book.account(p["konto"])
            if p.get("mwst"):
                from .mwst import code
                code(p["mwst"])
        entry["privatkonto"] = privat_konto(book)
        book.account(entry["privatkonto"])
    else:
        touched += _ensure_accounts(book, art)
        value = money(sum((Decimal(p["betrag"]) for p in pos), ZERO)) if pos else \
            money(parse_amount(wert if wert not in (None, "") else "0", "Wert"))
        entry["wert"] = f"{value:.2f}"
        diff = _difference(book, year, art, value)
        entry["buchung"] = {"soll": diff["soll"], "haben": diff["haben"], "betrag": f"{diff['betrag']:.2f}"}
    q = quelle(year, art)
    touched += book.remove_rows(lambda r: r.quelle == q)
    if not entry["beleg"]:
        entry["beleg"] = next_beleg(book, year)
    new = _entry_rows(book, year, art, entry)
    if new:
        touched += post(book, new)
    entries[art] = entry
    p = path(book, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(p, data)
    return entry, touched + [p]


def remove_entry(book: Book, year: int, art: str) -> tuple[dict, list[Path]]:
    """Take a closing entry back (only while the year is open). Its Beleg number stays used."""
    from .journal import ensure_open
    year = int(year)
    ensure_open(book, date(year, 12, 31))
    data = saved(book, year)
    entry = (data.get("buchungen") or {}).pop(art, None)
    if not entry:
        raise BookError(f"Für {year} ist keine Abschlussbuchung «{art}» gebucht")
    touched = book.remove_rows(lambda r: r.quelle == quelle(year, art))
    p = path(book, year)
    if data.get("buchungen"):
        write_yaml(p, data)
    else:
        p.unlink()
    return entry, touched + [p]


def fahrzeug_anteil(kaufpreis, monate: int = 12) -> Decimal:
    """ESTV: 0.9 % of the purchase price excl. MWST per month of business use, at least CHF 150 a month."""
    monthly = max(money(Decimal(str(kaufpreis)) * FAHRZEUG_SATZ), FAHRZEUG_MINIMUM)
    return money(monthly * int(monate))


# ---------- checklist ----------

def marks_path(book: Book, year: int) -> Path:
    return folder(book, year) / "checkliste.yaml"


def marks(book: Book, year: int) -> dict:
    p = marks_path(book, year)
    return (read_yaml(p) or {}) if p.exists() else {}


def mark(book: Book, year: int, key: str, done: bool = True, notiz: str = "", von: str = "") -> tuple[dict, list[Path]]:
    keys = {s["key"] for s in steps(book, year)}
    if key not in keys:
        raise BookError(f"Unbekannter Schritt «{key}»")
    data = marks(book, year)
    if done:
        data[key] = {"am": date.today().isoformat(), **({"von": von} if von else {}),
                     **({"notiz": notiz.strip()} if notiz.strip() else {})}
    else:
        data.pop(key, None)
    p = marks_path(book, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    if data:
        write_yaml(p, data)
    elif p.exists():
        p.unlink()
    return data.get(key, {}), [p]


def _step(key, phase, titel, status, text="", link="", wert=""):
    return {"key": key, "phase": phase, "titel": titel, "status": status, "text": text, "link": link, "wert": wert}


def _chf(v) -> str:
    return f"CHF {Decimal(str(v or 0)):,.2f}".replace(",", "'")


def _core_steps(book: Book, year: int) -> list[dict]:
    from . import abgrenzung, bank, erfassung, mwst, payroll
    from . import check as checks
    end = date(year, 12, 31)
    q = f"?jahr={year}"
    out = []
    add = lambda *a, **k: out.append(_step(*a, **k))  # noqa: E731

    # 1 Laufendes
    drafts = erfassung.drafts(book)
    add("belegeingang", "laufend", "Belegeingang leer", "ok" if not drafts else "offen",
        "Alle Rechnungen und Quittungen des Jahres erfasst." if not drafts else
        f"{len(drafts)} Entw{'ürfe' if len(drafts) > 1 else 'urf'} noch nicht freigegeben.", "/kreditoren?status=entwurf")
    open_tx = [t for t in bank.transactions(book) if t["Status"] == "offen" and t["Datum"] <= end.isoformat()]
    latest: dict[str, dict] = {}
    for r in bank.reconciliation(book):
        if r["datum"] <= end and (r["konto"] not in latest or r["datum"] > latest[r["konto"]]["datum"]):
            latest[r["konto"]] = r
    at_end = [r for r in latest.values() if r["datum"] >= date(year, 12, 28)]
    bad = [r for r in at_end if r["differenz"]]
    title = f"Bank per 31.12.{year} abgestimmt"
    if open_tx:
        add("bank", "laufend", title, "offen",
            f"{len(open_tx)} Bankbewegung{'en' if len(open_tx) > 1 else ''} bis 31.12. noch nicht abgeglichen.", "/bank")
    elif bad:
        add("bank", "laufend", title, "offen", "Kontoauszug und Buchhaltung weichen ab: "
            + ", ".join(f"{r['konto']} {_chf(r['differenz'])}" for r in bad), "/bank/abstimmung")
    elif at_end and len(at_end) == len(latest):
        add("bank", "laufend", title, "ok", "Saldo laut Kontoauszug per Ende Jahr stimmt mit der Buchhaltung überein.",
            "/bank/abstimmung")
    else:
        add("bank", "laufend", title, "offen",
            "Den Kontoauszug bis Ende Jahr importieren (camt.053 oder PDF) und den Saldo vergleichen.", "/bank")
    cash = book.accounts.get("1000")
    if cash and any("1000" in (r.soll, r.haben) for r in book.rows if r.datum.year == year):
        add("kasse", "laufend", "Kasse gezählt", "offen",
            "Bargeld per 31.12. zählen und mit Konto 1000 vergleichen.", f"/konten/1000{q}")
    slips = [s for s in payroll.payslips(book) if int(s["jahr"]) == year]
    if slips or payroll.employees(book):
        drafts_ = [s for s in slips if s.get("status") != "abgeschlossen"]
        dec = [s for s in slips if int(s["monat"]) == 12]
        status = "offen" if drafts_ or not dec else "ok"
        add("lohn", "laufend", f"Löhne {year} abgeschlossen", status,
            f"{len(drafts_)} Lohnabrechnung(en) noch nicht abgeschlossen." if drafts_ else
            "Dezember noch nicht abgerechnet." if not dec else "Alle Monate abgeschlossen.", "/lohn")
    cfg = mwst.config(book)
    if cfg["methode"] != "keine":
        missing = [p for p in mwst.periods(book, year) if mwst.load(book, p) is None]
        add("mwst_perioden", "laufend", f"MWST-Abrechnungen {year}", "offen" if missing else "ok",
            f"Noch nicht verbucht: {', '.join(missing)}." if missing else "Alle Perioden verbucht.", f"/mwst{q}")

    # 2 Bewerten
    data = (saved(book, year).get("buchungen") or {})
    for art, spec in BESTAENDE.items():
        if art == "ferien" and not payroll.employees(book):
            continue
        entry = data.get(art)
        if entry:
            v = view(book, year, art)
            add(art, "bewerten", spec["titel"], "offen" if v["veraltet"] else "ok",
                ("Gebucht, aber der Kontostand hat sich seither geändert — neu buchen. " if v["veraltet"] else "")
                + f"Per 31.12. {_chf(entry['wert'])} (Beleg {entry['beleg']}).",
                f"/abschluss/buchung/{art}{q}", wert=entry["wert"])
        else:
            add(art, "bewerten", spec["titel"], "offen", spec["text"], f"/abschluss/buchung/{art}{q}")
    if any(a.is_foreign for a in book.accounts.values()):
        done = (book.root / "bewertung" / f"{year}-12-31.yaml").exists()
        add("fremdwaehrung", "bewerten", "Fremdwährungen bewertet", "ok" if done else "offen",
            "Fremdwährungskonten zum Kurs per 31.12. bewerten.", f"/abschluss/buchungen{q}#fremdwaehrung")

    # 3 Abschlussbuchungen
    try:
        n = abgrenzung.open_count(book, year)
        total = len(abgrenzung.proposals(book, year))
    except BookError:
        n = total = 0
    add("abgrenzungen", "buchen", "Rechnungsabgrenzungen", "offen" if n else "ok",
        f"{n} Abgrenzung(en) noch nicht gebucht." if n else
        ("Alle gebucht." if total else "Keine Buchung mit Leistungszeitraum über den Jahreswechsel — "
         "Versicherungen, Miete im Voraus oder offene Rechnungen des Jahres prüfen."), f"/abschluss/buchungen{q}#abgrenzungen")
    if cfg["methode"] != "keine" and cfg["abrechnungsart"] == "vereinnahmt":
        done = mwst.load_abgrenzung(book, year)
        add("mwst_abgrenzung", "buchen", "MWST-Abgrenzung offene Posten", "ok" if done else "offen",
            "Steuer auf offenen Debitoren und Kreditoren per 31.12. (vereinnahmte Entgelte).",
            f"/mwst/abstimmung{q}")
    if book.settings.rechtsform_art == "einzelfirma":
        entry = data.get(PRIVAT)
        add(PRIVAT, "buchen", "Privatanteile", "ok" if entry else "offen",
            f"Gebucht: {_chf(sum((Decimal(p['betrag']) for p in entry['positionen']), ZERO))} (Beleg {entry['beleg']})."
            if entry else "Privatanteil Fahrzeug, Telefon, Raum der Inhaberin bzw. des Inhabers.",
            f"/abschluss/buchung/{PRIVAT}{q}")
        eq = book.settings.konto("gewinnvortrag")
        loose = [a for a in book.accounts.values() if a.klasse == "passiv" and a.nr.startswith("285")
                 and not a.abschluss and a.nr != eq]
        add("privat_eigenkapital", "buchen", "Privat an Eigenkapital", "offen" if loose else "ok",
            ("Diese Privatkonten werden nicht ins Eigenkapital abgeschlossen: "
             + ", ".join(f"{a.nr} {a.name}" for a in loose) + " — unter Konten «Abschluss ins Eigenkapital» setzen.")
            if loose else f"Die Privatkonten gehen bei der Eröffnung {year + 1} automatisch ins Konto {eq}; "
                          "keine Umbuchung nötig.", f"/konten{q}" if loose else f"/abschluss/jahresrechnung{q}#eigenkapital")

    # 4 Kontrolle
    errors = [i for i in checks.run(book) if i.level == "fehler"]
    add("pruefung", "kontrolle", "Prüfung ohne Fehler", "offen" if errors else "ok",
        f"{len(errors)} Fehler: {errors[0].message}" if errors else "aeradex check findet keine Fehler.",
        "/einstellungen#pruefung")
    if cfg["methode"] != "keine":
        try:
            ok = mwst.abstimmung(book, year).get("ok")
        except BookError:
            ok = False
        add("mwst_abstimmung", "kontrolle", "MWST-Umsatzabstimmung", "ok" if ok else "offen",
            "Umsätze und Steuer laut Buchhaltung gegen die Abrechnungen.", f"/mwst/abstimmung{q}")
    if payroll.employees(book):
        add("lohnabstimmung", "kontrolle", "Lohnabstimmung und Lohnmeldungen", "hinweis",
            "Lohnkonto gegen Buchhaltung; Lohnausweise und Meldungen an AHV, UVG, BVG.", f"/lohn/lohnmeldungen{q}")
    add("saldenliste", "kontrolle", "Saldenliste durchgesehen", "hinweis",
        "Jedes Bilanzkonto belegt (Kontoauszug, Abrechnung, Inventar); Kreditkarte und Kontokorrente stimmen mit "
        "den Abrechnungen; keine unplausiblen Vorzeichen.", f"/konten/saldenliste{q}")

    # 5 Abschliessen
    add("jahresrechnung", "abschliessen", "Jahresrechnung geprüft", "hinweis",
        "Bilanz und Erfolgsrechnung in der Vorschau durchsehen, mit dem Vorjahr vergleichen.", f"/abschluss/vorschau{q}")
    add("unterlagen", "abschliessen", "Unterlagen an Treuhand / Steuererklärung", "hinweis",
        "Abschlussunterlagen als ZIP herunterladen und weitergeben.", f"/abschluss/unterlagen{q}")
    lock = book.settings.sperre_bis
    add("sperre", "abschliessen", f"Geschäftsjahr {year} gesperrt", "ok" if lock and lock >= end else "offen",
        f"Gesperrt bis {lock:%d.%m.%Y}." if lock and lock >= end else
        "Zum Schluss sperren — danach sind die Buchungen unveränderlich.", f"/abschluss/abschliessen{q}#sperre")
    return out


def steps(book: Book, year: int) -> list[dict]:
    from . import plugins
    year = int(year)
    out = _core_steps(book, year)
    known = {p for p, _ in PHASEN}
    for s in plugins.abschluss_schritte(book, year):
        out.append(_step(s.key, s.phase if s.phase in known else "buchen", s.titel, s.status, s.text, s.link))
    order = {p: i for i, (p, _) in enumerate(PHASEN)}
    return sorted(out, key=lambda s: order[s["phase"]])  # stable: core order within a phase, plugins after


def checklist(book: Book, year: int) -> dict:
    """The closing of `year`, phase by phase. A step a person ticked off counts as done ("von Hand")."""
    year = int(year)
    ticked = marks(book, year)
    items = steps(book, year)
    for s in items:
        m = ticked.get(s["key"])
        s["markiert"] = m or None
        s["erledigt"] = s["status"] == "ok" or bool(m) or s["status"] == "entfaellt"
    relevant = [s for s in items if s["status"] != "entfaellt"]
    return {"jahr": year, "erledigt": sum(1 for s in relevant if s["erledigt"]), "total": len(relevant),
            "phasen": [{"key": k, "titel": t, "schritte": [s for s in items if s["phase"] == k]}
                       for k, t in PHASEN if any(s["phase"] == k for s in items)]}


def due_year(book: Book, today: date | None = None) -> int | None:
    """The year whose closing is due: from 1 December, until it is locked."""
    today = today or date.today()
    lock = book.settings.sperre_bis
    for year in book.years():
        if lock and lock >= date(year, 12, 31):
            continue
        if today >= date(year, 12, 1):
            return year
    return None

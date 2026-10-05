"""Jahresrechnung: Bilanz and Erfolgsrechnung by the Swiss OR minimum
classification (Art. 959a / 959b OR), Gewinnverwendung and Anhang (Art. 959c).

Group codes are the vocabulary of the chart of accounts: every account sits in
one group, by default derived from its number along the Kontenrahmen KMU, and a
`gruppe:` in kontenplan.yaml overrides it.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .book import Book
from .files import read_yaml
from .ledger import BalanceEngine

ZERO = Decimal("0")

# (code, label, klasse) in the order the statements print them.
GROUPS = [
    # Aktiven — Umlaufvermögen
    ("fluessige", "Flüssige Mittel und kurzfristig gehaltene Aktiven mit Börsenkurs", "aktiv"),
    ("ford_ll", "Forderungen aus Lieferungen und Leistungen", "aktiv"),
    ("ford_uebrige_kf", "Übrige kurzfristige Forderungen", "aktiv"),
    ("ford_nahe_kf", "Kurzfristige Forderungen ggü. Nahestehenden", "aktiv"),
    ("vorraete", "Vorräte und nicht fakturierte Dienstleistungen", "aktiv"),
    ("arap", "Aktive Rechnungsabgrenzungen", "aktiv"),
    # Aktiven — Anlagevermögen
    ("finanzanlagen", "Finanzanlagen", "aktiv"),
    ("finanzanlagen_nahe", "Darlehen ggü. Nahestehenden", "aktiv"),
    ("beteiligungen", "Beteiligungen", "aktiv"),
    ("sachanlagen", "Sachanlagen", "aktiv"),
    ("immaterielle", "Immaterielle Werte", "aktiv"),
    ("nicht_einbezahlt", "Nicht einbezahltes Grund-, Gesellschafter- oder Stiftungskapital", "aktiv"),
    # Passiven — kurzfristiges Fremdkapital
    ("verb_ll", "Verbindlichkeiten aus Lieferungen und Leistungen", "passiv"),
    ("verb_verzinsl_kf", "Kurzfristige verzinsliche Verbindlichkeiten", "passiv"),
    ("verb_uebrige_kf", "Übrige kurzfristige Verbindlichkeiten", "passiv"),
    ("verb_nahe_kf", "Kurzfristige Verbindlichkeiten ggü. Nahestehenden", "passiv"),
    ("prap", "Passive Rechnungsabgrenzungen und kurzfristige Rückstellungen", "passiv"),
    # Passiven — langfristiges Fremdkapital
    ("verb_verzinsl_lf", "Langfristige verzinsliche Verbindlichkeiten", "passiv"),
    ("verb_uebrige_lf", "Übrige langfristige Verbindlichkeiten", "passiv"),
    ("verb_nahe_lf", "Langfristige Verbindlichkeiten ggü. Nahestehenden", "passiv"),
    ("rueckstellungen", "Rückstellungen", "passiv"),
    # Eigenkapital
    ("eigenkapital", "Eigenkapital", "passiv"),
    # Erfolgsrechnung (Gesamtkostenverfahren, Art. 959b Abs. 2 OR)
    ("ertrag", "Nettoerlöse aus Lieferungen und Leistungen", "ertrag"),
    ("material", "Material-, Waren- und Drittleistungsaufwand", "aufwand"),
    ("personal", "Personalaufwand", "aufwand"),
    ("betrieb", "Übriger betrieblicher Aufwand", "aufwand"),
    ("abschreibungen", "Abschreibungen und Wertberichtigungen", "aufwand"),
    ("finanz", "Finanzaufwand und Finanzertrag", "aufwand"),
    ("nebenbetrieb", "Betriebliche Nebenerfolge", "aufwand"),
    ("betriebsfremd", "Betriebsfremder Aufwand und Ertrag", "aufwand"),
    ("ausserordentlich", "Ausserordentlicher, einmaliger oder periodenfremder Aufwand und Ertrag", "aufwand"),
    ("steuern", "Direkte Steuern", "aufwand"),
    ("abschluss", "Abschlusskonten", "aufwand"),
]
GROUP_LABEL = {code: label for code, label, _ in GROUPS}
GROUP_KLASSE = {code: k for code, _, k in GROUPS}


def default_group(nr: str, klasse: str) -> str:
    """The statement group an account number belongs to in the Kontenrahmen KMU."""
    digits = "".join(ch for ch in nr if ch.isdigit())
    n = int(digits[:4].ljust(4, "0")) if digits else 0
    if klasse == "aktiv":
        if n < 1100: return "fluessige"
        if n < 1110: return "ford_ll"
        if n < 1200: return "ford_uebrige_kf"
        if n < 1300: return "vorraete"
        if n < 1400: return "arap"
        if n < 1480: return "finanzanlagen"
        if n < 1500: return "beteiligungen"
        if n < 1700: return "sachanlagen"
        if n < 1800: return "immaterielle"
        return "nicht_einbezahlt"
    if klasse == "passiv":
        if n < 2100: return "verb_ll"
        if n < 2200: return "verb_verzinsl_kf"
        if n < 2300: return "verb_uebrige_kf"
        if n < 2400: return "prap"
        if n < 2500: return "verb_verzinsl_lf"
        if n < 2600: return "verb_uebrige_lf"
        if n < 2700: return "rueckstellungen"
        if n < 2800: return "verb_uebrige_lf"
        return "eigenkapital"
    if n < 4000: return "ertrag"
    if n < 5000: return "material"
    if n < 6000: return "personal"
    if n < 6800: return "betrieb"
    if n < 6900: return "abschreibungen"
    if n < 7000: return "finanz"
    if n < 8000: return "nebenbetrieb"
    if n < 8500: return "betriebsfremd"
    if n < 8900: return "ausserordentlich"
    if n < 9000: return "steuern"
    return "abschluss"


_UMLAUF = ["fluessige", "ford_ll", "ford_uebrige_kf", "ford_nahe_kf", "vorraete", "arap"]
_ANLAGE = ["finanzanlagen", "finanzanlagen_nahe", "beteiligungen", "sachanlagen", "immaterielle",
           "nicht_einbezahlt"]
_FK_KURZ = ["verb_ll", "verb_verzinsl_kf", "verb_uebrige_kf", "verb_nahe_kf", "prap"]
_FK_LANG = ["verb_verzinsl_lf", "verb_uebrige_lf", "verb_nahe_lf", "rueckstellungen"]
_PL = ["ertrag", "material", "personal", "betrieb", "abschreibungen", "finanz", "nebenbetrieb",
       "betriebsfremd", "ausserordentlich", "steuern", "abschluss"]


def _effective_group(acct, balance: Decimal) -> str:
    flip = acct.gruppe_negativ
    if flip and flip in GROUP_LABEL and acct.is_balance_sheet:
        if (acct.klasse == "aktiv" and balance < 0) or (acct.klasse == "passiv" and balance > 0):
            return flip
    return acct.gruppe


def year_end_statement(book: Book, year: int, engine: BalanceEngine | None = None) -> dict:
    """Bilanz and Erfolgsrechnung for `year` with the prior-year column.

    Figures are presented in reading convention: Aktiven positive, Passiven and
    Ertrag positive, Aufwand negative. Every line lists its member accounts."""
    engine = engine or BalanceEngine(book)
    result_acct = book.settings.konto("jahresergebnis")

    members: dict[str, list[dict]] = {}
    for nr, acct in sorted(book.accounts.items()):
        cur, pri = engine.balance(nr, year), engine.prior(nr, year)
        code = _effective_group(acct, cur)
        members.setdefault(code, []).append({"konto": nr, "name": acct.name, "cur": cur, "pri": pri})

    def col(codes, exclude=()):
        c = p = ZERO
        for code in codes:
            for m in members.get(code, []):
                if m["konto"] not in exclude:
                    c += m["cur"]
                    p += m["pri"]
        return c, p

    def line(rows, label, cur, pri, style="line", sign=1, codes=None, keep_zero=False, exclude=()):
        cur, pri = cur * sign, pri * sign
        if style == "line" and not keep_zero and not cur and not pri:
            return
        detail = []
        for code in codes or []:
            for m in members.get(code, []):
                if m["konto"] in exclude or (not m["cur"] and not m["pri"]):
                    continue
                detail.append({"konto": m["konto"], "name": m["name"],
                               "aktuell": m["cur"] * sign, "vorjahr": m["pri"] * sign, **fw_note(m["konto"])})
        rows.append({"label": label, "aktuell": cur, "vorjahr": pri, "stil": style, "konten": detail})

    from .ledger import fw_balance
    stichtag = date(year, 12, 31)

    def fw_note(nr):
        acct = book.accounts[nr]
        if not acct.is_foreign:
            return {}
        return {"fw": f"{acct.waehrung} {fw_balance(book, nr, stichtag) * (1 if acct.klasse == 'aktiv' else -1):,.2f}".replace(",", "'")}

    foreign = [a.nr for a in book.accounts.values() if a.is_foreign]
    revalued = (book.root / "bewertung" / f"{stichtag.isoformat()}.yaml").exists()

    res_c, res_p = col(_PL)

    aktiven: list[dict] = []
    um = col(_UMLAUF)
    for code in _UMLAUF:
        line(aktiven, GROUP_LABEL[code], *col([code]), codes=[code])
    line(aktiven, "Umlaufvermögen", *um, style="zwischentotal")
    an = col(_ANLAGE)
    for code in _ANLAGE:
        line(aktiven, GROUP_LABEL[code], *col([code]), codes=[code])
    line(aktiven, "Anlagevermögen", *an, style="zwischentotal")
    total_a = (um[0] + an[0], um[1] + an[1])
    line(aktiven, "Total Aktiven", *total_a, style="total")

    passiven: list[dict] = []
    fkk = col(_FK_KURZ)
    for code in _FK_KURZ:
        line(passiven, GROUP_LABEL[code], *col([code]), sign=-1, codes=[code])
    if fkk[0] or fkk[1]:
        line(passiven, "Kurzfristiges Fremdkapital", *fkk, style="zwischentotal", sign=-1)
    fkl = col(_FK_LANG)
    for code in _FK_LANG:
        line(passiven, GROUP_LABEL[code], *col([code]), sign=-1, codes=[code])
    if fkl[0] or fkl[1]:
        line(passiven, "Langfristiges Fremdkapital", *fkl, style="zwischentotal", sign=-1)
    line(passiven, "Fremdkapital", fkk[0] + fkl[0], fkk[1] + fkl[1], style="zwischentotal", sign=-1)
    # Equity is listed account by account (Kapital, Reserven, Gewinnvortrag);
    # the year's result is its own computed line, so the result account is left out.
    eig_c = eig_p = ZERO
    for m in members.get("eigenkapital", []):
        if m["konto"] == result_acct:
            continue
        eig_c += m["cur"]
        eig_p += m["pri"]
        line(passiven, m["name"], m["cur"], m["pri"], sign=-1)
    line(passiven, "Jahresgewinn / Jahresverlust", res_c, res_p, sign=-1, keep_zero=True)
    line(passiven, "Eigenkapital", eig_c + res_c, eig_p + res_p, style="zwischentotal", sign=-1)
    total_p = (fkk[0] + fkl[0] + eig_c + res_c, fkk[1] + fkl[1] + eig_p + res_p)
    line(passiven, "Total Passiven", *total_p, style="total", sign=-1)

    erfolg: list[dict] = []
    er, mat = col(["ertrag"]), col(["material"])
    line(erfolg, "Betriebsertrag aus Lieferungen und Leistungen", *er, sign=-1, codes=["ertrag"])
    line(erfolg, GROUP_LABEL["material"], *mat, sign=-1, codes=["material"])
    brutto = (er[0] + mat[0], er[1] + mat[1])
    line(erfolg, "Bruttoergebnis", *brutto, style="zwischentotal", sign=-1)
    per, bet = col(["personal"]), col(["betrieb"])
    line(erfolg, GROUP_LABEL["personal"], *per, sign=-1, codes=["personal"])
    line(erfolg, GROUP_LABEL["betrieb"], *bet, sign=-1, codes=["betrieb"])
    ebitda = (brutto[0] + per[0] + bet[0], brutto[1] + per[1] + bet[1])
    line(erfolg, "Betriebliches Ergebnis vor Abschreibungen, Finanzerfolg und Steuern (EBITDA)",
         *ebitda, style="zwischentotal", sign=-1)
    ab = col(["abschreibungen"])
    line(erfolg, GROUP_LABEL["abschreibungen"], *ab, sign=-1, codes=["abschreibungen"])
    ebit = (ebitda[0] + ab[0], ebitda[1] + ab[1])
    line(erfolg, "Betriebliches Ergebnis vor Finanzerfolg und Steuern (EBIT)", *ebit,
         style="zwischentotal", sign=-1)
    for code in ("finanz", "nebenbetrieb", "betriebsfremd", "ausserordentlich", "steuern", "abschluss"):
        line(erfolg, GROUP_LABEL[code], *col([code]), sign=-1, codes=[code])
    line(erfolg, "Jahresgewinn / Jahresverlust", res_c, res_p, style="total", sign=-1)

    lock = book.settings.sperre_bis
    return {
        "firma": book.settings.firma, "jahr": year, "vorjahr": year - 1,
        "abgeschlossen": bool(lock and lock >= date(year, 12, 31)),
        "aktiven": aktiven, "passiven": passiven, "erfolg": erfolg,
        "total_aktiven": total_a[0], "total_passiven": -total_p[0],
        "differenz": total_a[0] + total_p[0],
        "jahresergebnis": -res_c,
        "gewinnverwendung": profit_allocation(book, year, engine)
                            if book.settings.rechtsform_art == "gesellschaft" else None,
        "eigenkapital": equity_change(book, year, engine)
                        if book.settings.rechtsform_art != "gesellschaft" else None,
        "fremdwaehrung": {"konten": foreign, "stichtag": stichtag, "bewertet": revalued} if foreign else None,
    }


# ---------- Eigenkapital (Einzelfirma, Verein) ----------

def equity_change(book: Book, year: int, engine: BalanceEngine | None = None) -> dict:
    """How the equity account moves into the next year: opening, the Privat accounts that close
    into it, the year's result. Positive numbers = equity. Einzelfirma and Verein have no
    Gewinnverwendung — this is what happens instead, at the opening of the next year."""
    engine = engine or BalanceEngine(book)
    nr = book.settings.konto("gewinnvortrag")
    acct = book.accounts.get(nr)
    anfang = -engine.balance(nr, year)
    privat = [{"konto": a.nr, "name": a.name, "betrag": -engine.balance(a.nr, year)}
              for a in sorted(book.accounts.values(), key=lambda a: a.nr) if a.abschluss == nr]
    gewinn = -engine.result(year)
    return {"jahr": year, "konto": nr, "name": acct.name if acct else nr, "bestand": anfang, "privat": privat,
            "jahresergebnis": gewinn, "neu": anfang + sum((p["betrag"] for p in privat), ZERO) + gewinn}


# ---------- Gewinnverwendung ----------

def _require_company(book: Book) -> None:
    if book.settings.rechtsform_art != "gesellschaft":
        from .book import BookError
        raise BookError(f"Gewinnverwendung und Dividende gibt es nur bei AG und GmbH (Rechtsform: "
                        f"{book.settings.get('rechtsform')}); das Ergebnis geht ins Eigenkapital")


def allocation_path(book: Book, year: int):
    return book.root / "abschluss" / str(year) / "gewinnverwendung.yaml"


def profit_allocation(book: Book, year: int, engine: BalanceEngine | None = None) -> dict:
    """Bilanzgewinn and its proposed use. Positive numbers = profit."""
    engine = engine or BalanceEngine(book)
    vortrag = -engine.balance(book.settings.konto("gewinnvortrag"), year)
    gewinn = -engine.result(year)
    path = allocation_path(book, year)
    data = read_yaml(path) if path.exists() else {}
    dividende = Decimal(str(data.get("dividende") or 0))
    reserve = Decimal(str(data.get("reserve") or 0))
    bilanzgewinn = vortrag + gewinn
    return {
        "jahr": year, "gewinnvortrag": vortrag, "jahresgewinn": gewinn,
        "bilanzgewinn": bilanzgewinn, "dividende": dividende, "reserve": reserve,
        "vortrag_neu": bilanzgewinn - dividende - reserve,
        "gebucht": bool(data.get("gebucht")),
    }


# ---------- Anhang ----------

def default_anhang(book: Book, year: int) -> str:
    s = book.settings
    rechtsform = s.get("rechtsform") or "GmbH"
    ort = s.adresse.get("ort") or ""
    return (
        "## Firma, Rechtsform und Sitz\n"
        f"{s.firma}, {rechtsform}, mit Sitz in {ort}.\n\n"
        "## Grundlagen der Rechnungslegung\n"
        "Die vorliegende Jahresrechnung wurde gemäss den Bestimmungen über die kaufmännische "
        "Buchführung und Rechnungslegung des Schweizerischen Obligationenrechts "
        "(Art. 957–963b OR) erstellt.\n\n"
        "## Anzahl Vollzeitstellen\n"
        "Die Anzahl Vollzeitstellen im Jahresdurchschnitt liegt nicht über 10.\n\n"
        "## Wesentliche Ereignisse nach dem Bilanzstichtag\n"
        "Nach dem Bilanzstichtag sind keine wesentlichen Ereignisse eingetreten, die einen "
        f"Einfluss auf die Jahresrechnung {year} haben.\n\n"
        "## Weitere Angaben\n"
        "Auflösung von wesentlichen stillen Reserven: keine.\n"
    )


def anhang(book: Book, year: int) -> str:
    path = book.root / "abschluss" / str(year) / "anhang.md"
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8")
    return default_anhang(book, year)


def parse_anhang(text: str) -> list[tuple[str, str]]:
    blocks = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("# "):
            continue
        blocks.append(("heading", line[3:].strip()) if line.startswith("## ") else ("para", line))
    return blocks


def set_allocation(book: Book, year: int, dividende=0, reserve=0):
    """Store the proposed Gewinnverwendung for `year` (decided by the GV)."""
    _require_company(book)
    from .files import write_yaml

    current = profit_allocation(book, year)
    if current["gebucht"]:
        from .book import BookError
        raise BookError(f"Gewinnverwendung {year} ist bereits gebucht")
    path = allocation_path(book, year)
    write_yaml(path, {"jahr": year, "dividende": Decimal(str(dividende or 0)),
                      "reserve": Decimal(str(reserve or 0)), "gebucht": False})
    return profit_allocation(book, year), [path]


def book_allocation(book: Book, year: int, datum=None):
    """Book the Gewinnverwendung on the GV date (default 30.06. of the next year).

    The year's result is already folded into the Gewinnvortrag at the opening of
    the next year, so only the distribution is booked: DR Gewinnvortrag an
    Dividende / gesetzliche Gewinnreserve."""
    from .book import BookError, Row
    from .files import parse_date, write_yaml
    from .journal import post

    _require_company(book)
    alloc = profit_allocation(book, year)
    if alloc["gebucht"]:
        raise BookError(f"Gewinnverwendung {year} ist bereits gebucht")
    if not (alloc["dividende"] or alloc["reserve"]):
        raise BookError(f"Keine Gewinnverwendung für {year} erfasst — zuerst `batzen allocation set`")
    if alloc["dividende"] + alloc["reserve"] > alloc["bilanzgewinn"]:
        raise BookError("Dividende und Reserve übersteigen den Bilanzgewinn "
                        f"({alloc['bilanzgewinn']:.2f})")
    d = parse_date(datum, "datum") if datum else date(year + 1, 6, 30)
    if d.year != year + 1:
        raise BookError(f"Die Gewinnverwendung {year} wird im Jahr {year + 1} gebucht")
    s = book.settings
    beleg = f"GV-{year}"
    rows = []
    if alloc["dividende"]:
        rows.append(Row(d, beleg, f"Gewinnverwendung {year}: Dividende", s.konto("gewinnvortrag"),
                        s.konto("dividende"), alloc["dividende"], f"abschluss:{year}"))
    if alloc["reserve"]:
        rows.append(Row(d, beleg, f"Gewinnverwendung {year}: Gesetzliche Gewinnreserve",
                        s.konto("gewinnvortrag"), s.konto("reserve"), alloc["reserve"], f"abschluss:{year}"))
    touched = post(book, rows)
    path = allocation_path(book, year)
    write_yaml(path, {"jahr": year, "dividende": alloc["dividende"], "reserve": alloc["reserve"],
                      "gebucht": True, "datum": d.isoformat()})
    return rows, touched + [path]


# ---------- Dividende: Auszahlung mit Verrechnungssteuer ----------

VST_SATZ = Decimal("0.35")


def dividend_path(book: Book, year: int):
    return book.root / "abschluss" / f"dividende-{year}.yaml"


def dividend_rows(book: Book, saved: dict) -> list:
    """The payout owns its rows: Beschlossene Ausschüttungen an Bank (65 %) und an Verrechnungssteuer (35 %)."""
    from .book import Row
    from .files import parse_date
    d = parse_date(saved["datum"])
    year = int(saved["jahr"])
    beleg, quelle = f"DIV-{year}", f"dividende:{year}"
    brutto, vst = Decimal(str(saved["brutto"])), Decimal(str(saved["vst"]))
    s = book.settings
    return [Row(d, beleg, f"Dividende {year}: Auszahlung netto", s.konto("dividende"), str(saved["konto"]),
                brutto - vst, quelle),
            Row(d, beleg, f"Dividende {year}: Verrechnungssteuer 35 %", s.konto("dividende"),
                s.konto("verrechnungssteuer"), vst, quelle)]


def pay_dividend(book: Book, year: int, datum=None, konto: str = ""):
    """Pay out the dividend decided for `year`: 65 % to the shareholders, 35 % Verrechnungssteuer
    owed to the ESTV — to be declared with Formular 103 and paid within 30 days."""
    from datetime import timedelta
    from .book import BookError
    from .files import parse_date, write_yaml
    from .journal import ensure_open, post
    _require_company(book)
    alloc = profit_allocation(book, year)
    if not alloc["gebucht"]:
        raise BookError(f"Gewinnverwendung {year} ist noch nicht gebucht")
    brutto = Decimal(str(alloc["dividende"]))
    if not brutto:
        raise BookError(f"Für {year} ist keine Dividende beschlossen")
    if dividend_path(book, year).exists():
        raise BookError(f"Dividende {year} ist bereits ausbezahlt")
    d = parse_date(datum, "datum") if datum else date.today()
    ensure_open(book, d)
    konto = konto or book.settings.konto("bank")
    book.account(konto)
    book.account(book.settings.konto("verrechnungssteuer"))
    vst = (brutto * VST_SATZ).quantize(Decimal("0.01"))
    saved = {"jahr": year, "datum": d.isoformat(), "brutto": brutto, "vst": vst, "netto": brutto - vst,
             "konto": konto, "formular": "103", "frist": (d + timedelta(days=30)).isoformat()}
    touched = post(book, dividend_rows(book, saved))
    write_yaml(dividend_path(book, year), saved)
    return saved, touched + [dividend_path(book, year)]

"""Jahres-Lohnmeldungen and the company Lohnkonto as documents.

The figures come only from closed payslips (frozen at closing). The wage reported to each insurer is the
gross wage on which that deduction was actually taken — payroll.calculate does not model the AHV
Rentnerfreibetrag, the youth exemption, the UVG/ALV Höchstlohn or the 8-hour NBU threshold, so those cases
are not corrected here but flagged as Prüfhinweise for a person to look at before filing.

Every recipient gets one block: Ausgleichskasse (AHV/IV/EO, ALV, FAK), UVG, KTG, BVG, Quellensteuer.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from .book import Book, BookError
from .files import parse_date
from .payroll import (AG_ORDER, D, ZERO, config, display_name, employees, lohnkonto_matrix, money,
                      payslips)

# Legal constants, by the year they apply from. Sources:
# - AHVV Art. 6quater: Freibetrag für erwerbstätige Rentner CHF 1'400 pro Monat / 16'800 pro Jahr.
# - AVIG Art. 3 Abs. 2 / UVV Art. 22 Abs. 1: höchstens versicherter Verdienst CHF 148'200 (seit 2016).
# - AHVG Art. 3 Abs. 1bis: Erwerbstätige ab 1. Januar nach Vollendung des 17. Altersjahrs beitragspflichtig.
# - UVV Art. 13: NBU-versichert ab mindestens 8 Stunden pro Woche beim selben Arbeitgeber.
# - AHVG Art. 21 und Übergangsbestimmungen AHV 21: Referenzalter 65; Frauen Jahrgang ≤1960: 64,
#   1961: 64 J. 3 Mt., 1962: 64 J. 6 Mt., 1963: 64 J. 9 Mt., ab 1964: 65.
RENTNER_FREIBETRAG_MONAT = Decimal("1400")
HOECHSTLOHN = {2016: Decimal("148200")}
NBU_MIN_STUNDEN_WOCHE = Decimal("8")
_FRAUEN_REFERENZALTER_MONATE = {1961: 64 * 12 + 3, 1962: 64 * 12 + 6, 1963: 64 * 12 + 9}

GESCHLECHT_LABEL = {"w": "Frauen", "m": "Männer", "": "ohne Angabe"}


def hoechstlohn(year: int) -> Decimal:
    return HOECHSTLOHN[max(y for y in HOECHSTLOHN if y <= year)] if year >= min(HOECHSTLOHN) else HOECHSTLOHN[2016]


def referenzalter_monate(geburtsjahr: int, geschlecht: str) -> int:
    if geschlecht != "w" or geburtsjahr >= 1964:
        return 65 * 12
    if geburtsjahr <= 1960:
        return 64 * 12
    return _FRAUEN_REFERENZALTER_MONATE[geburtsjahr]


def rentner_ab(birth: date, geschlecht: str) -> date:
    """First day of the month after the Referenzalter is reached: from then on the Freibetrag applies."""
    months = birth.year * 12 + (birth.month - 1) + referenzalter_monate(birth.year, geschlecht) + 1
    return date(months // 12, months % 12 + 1, 1)


def _fmt(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _period(emp: dict, year: int, months: list[int]) -> tuple[date | None, date | None]:
    if not months:
        return None, None
    import calendar
    start, end = date(year, months[0], 1), date(year, months[-1], calendar.monthrange(year, months[-1])[1])
    entry = parse_date(emp["eintritt"]) if emp.get("eintritt") else None
    leave = parse_date(emp["austritt"]) if emp.get("austritt") else None
    if entry and start <= entry <= end:
        start = entry
    if leave and start <= leave <= end:
        end = leave
    return start, end


def _person(year: int, nr: str, emp: dict, slips: list[dict]) -> dict:
    closed = sorted((p for p in slips if p.get("status") == "abgeschlossen"), key=lambda p: int(p["monat"]))
    months = [int(p["monat"]) for p in closed]

    def total(key, when=lambda p: True):
        return sum((D(p["werte"].get(key)) for p in closed if when(p)), ZERO)

    def ag(code):
        return sum((D((p.get("ag") or {}).get(code)) for p in closed), ZERO)

    def taken(code):     # the month had this insurance: an employee or employer contribution was levied
        return lambda p: bool(D(p["werte"].get(code)) or D((p.get("ag") or {}).get(code)))

    cap = money(hoechstlohn(year) * len(months) / 12)
    start, end = _period(emp, year, months)
    alv_lohn = total("bruttolohn", taken("alv"))
    uvg_lohn = total("bruttolohn")
    return {
        "nummer": nr, "name": display_name(emp), "ahv_nr": emp.get("ahv_nr") or "",
        "geburtsdatum": emp.get("geburtsdatum") or "", "geschlecht": emp.get("geschlecht") or "",
        "eintritt": emp.get("eintritt") or "", "austritt": emp.get("austritt") or "",
        "von": start, "bis": end, "monate": months,
        "bruttolohn": total("bruttolohn"),
        "ahv_lohn": total("bruttolohn", taken("ahv")),
        "alv_lohn": alv_lohn, "alv_lohn_max": min(alv_lohn, cap),
        "uvg_lohn": uvg_lohn, "uvg_lohn_max": min(uvg_lohn, cap),
        "nbu_lohn": min(total("bruttolohn", lambda p: bool(D(p["werte"].get("uvg")))), cap),
        "ktg_lohn": total("bruttolohn", taken("ktg")),
        "hoechstlohn": cap,
        "an": {k: total(k) for k in ("ahv", "alv", "uvg", "ktg", "bvg", "quellensteuer")},
        "ag": {code: ag(code) for code in AG_ORDER},
        "kinderzulagen": total("kinderzulagen"), "korrektur": total("korrektur"),
        "qst": [{"monat": int(p["monat"]), "kanton": p["werte"].get("qst_kanton") or "",
                 "tarif": p["werte"].get("qst_code") or "", "basis": D(p["werte"].get("qst_basis")),
                 "satz": D(p["werte"].get("qst_satz")), "betrag": D(p["werte"].get("quellensteuer"))}
                for p in closed if D(p["werte"].get("quellensteuer")) or p["werte"].get("qst_code")],
    }


def _hints(year: int, emp: dict, person: dict, cfg: dict, totals) -> list[str]:
    name = f"{person['nummer']} {person['name']}"
    out = []
    missing = [label for key, label in (("ahv_nr", "AHV-Nr."), ("geburtsdatum", "Geburtsdatum"),
                                        ("geschlecht", "Geschlecht")) if not person[key]]
    if missing:
        out.append(f"{name}: {', '.join(missing)} {'fehlt' if len(missing) == 1 else 'fehlen'} im Personalstamm")
    if totals["fehlende_monate"]:
        out.append(f"{name}: nicht gerechnet {', '.join(f'{m:02d}' for m in totals['fehlende_monate'])}/{year}"
                   " — fehlt in der Meldung")
    if totals["offene_monate"]:
        out.append(f"{name}: Entwurf {', '.join(f'{m:02d}' for m in totals['offene_monate'])}/{year}"
                   " — erst nach dem Abschliessen in der Meldung")
    if person["geburtsdatum"]:
        birth = parse_date(person["geburtsdatum"])
        if year < birth.year + 18 and person["ahv_lohn"]:
            out.append(f"{name}: erst ab 1.1.{birth.year + 18} AHV-beitragspflichtig (AHVG Art. 3) — "
                       "AHV/ALV-Abzüge und gemeldeten AHV-Lohn prüfen; UVG gilt trotzdem")
        ab = rentner_ab(birth, person["geschlecht"])
        if ab <= date(year, 12, 31) and person["bruttolohn"]:
            sex = "" if person["geschlecht"] else " (Geschlecht fehlt: mit Referenzalter 65 gerechnet)"
            out.append(f"{name}: Rentner ab {_fmt(ab)}{sex} — AHV-Freibetrag CHF 1'400/Monat abziehen "
                       "und keine ALV mehr (AHVV Art. 6quater); die Lohnabrechnung rechnet das nicht")
    if person["alv_lohn"] > person["alv_lohn_max"] or person["uvg_lohn"] > person["uvg_lohn_max"]:
        out.append(f"{name}: Höchstlohn ALV/UVG CHF {hoechstlohn(year):,.0f} (anteilig "
                   f"{person['hoechstlohn']:,.2f}) überschritten — gemeldet ist der begrenzte Lohn, "
                   "die Abzüge wurden auf dem ganzen Lohn gerechnet".replace(",", "'"))
    if person["an"]["uvg"] and (emp.get("lohnart") or "monat") == "monat":
        hours = D(emp.get("pensum") if emp.get("pensum") is not None else 100) / 100 * \
            D(emp.get("vollzeit_stunden_woche") or 42)
        if hours < NBU_MIN_STUNDEN_WOCHE:
            out.append(f"{name}: {hours:.1f} Std./Woche — unter 8 Std. nicht NBU-versichert (UVV Art. 13)")
    an = cfg["saetze_an"]
    for code, label in (("ahv", "AHV/IV/EO"), ("alv", "ALV")):
        base = person["ahv_lohn"] if code == "ahv" else person["alv_lohn"]
        expected = base * D(an.get(code))
        if base and abs(person["an"][code] - expected) > Decimal("0.01") * max(len(person["monate"]), 1):
            out.append(f"{name}: {label}-Abzug {person['an'][code]:.2f} entspricht nicht "
                       f"{D(an.get(code)) * 100:.2f} % des Lohns ({expected:.2f}) — Satz im Jahr geändert?")
    return out


def _sum(people: list[dict], key, sub=None) -> Decimal:
    return sum(((p[key][sub] if sub else p[key]) for p in people), ZERO)


def _by_sex(people: list[dict], key: str) -> dict[str, Decimal]:
    out = {}
    for sex in ("w", "m", ""):
        group = [p for p in people if p["geschlecht"] == sex]
        if group:
            out[GESCHLECHT_LABEL[sex]] = _sum(group, key)
    return out


def _ledger_check(book: Book, year: int, cfg: dict, people: list[dict]) -> list[dict]:
    """Wage and employer contribution totals against the expense accounts. A difference means a booking on
    the account outside the payroll run (missing from the declaration) or a payroll month booked elsewhere."""
    from .ledger import BalanceEngine
    moves = BalanceEngine(book).year_movements(year)
    expected: dict[str, Decimal] = {}
    lohn = str(cfg["konten"]["lohnaufwand"])
    expected[lohn] = _sum(people, "bruttolohn") + _sum(people, "korrektur")
    for code in AG_ORDER:
        acct = str(cfg["ag_konten"][code][0])
        expected[acct] = expected.get(acct, ZERO) + _sum(people, "ag", code)
    accounts = book.accounts
    return [{"konto": nr, "name": accounts[nr].name if nr in accounts else "",
             "lohnkonto": amount, "buchhaltung": money(moves.get(nr, ZERO)),
             "differenz": money(moves.get(nr, ZERO) - amount)} for nr, amount in expected.items()]


def jahresmeldung(book: Book, year: int) -> dict:
    """All figures for the annual wage declarations of `year`, per recipient, with Prüfhinweise."""
    from . import lohnausweis
    cfg = config(book)
    slips = payslips(book, year)
    emps = employees(book)
    people, hints = [], []
    for nr in sorted({p["mitarbeiter"] for p in slips}):
        emp = emps.get(nr) or {"nummer": nr}
        own = [p for p in slips if p["mitarbeiter"] == nr]
        person = _person(year, nr, emp, own)
        totals = lohnausweis.annual_totals(book, year, nr) if nr in emps else \
            {"fehlende_monate": [], "offene_monate": []}
        hints += _hints(year, emp, person, cfg, totals)
        if person["monate"]:
            people.append(person)
    an_ahv, ag_ahv = _sum(people, "an", "ahv"), _sum(people, "ag", "ahv")
    an_alv, ag_alv = _sum(people, "an", "alv"), _sum(people, "ag", "alv")
    fak, verwaltung, kz = _sum(people, "ag", "fak"), _sum(people, "ag", "verwaltung"), _sum(people, "kinderzulagen")
    ausgleichskasse = {
        "ahv_lohn": _sum(people, "ahv_lohn"), "alv_lohn": _sum(people, "alv_lohn_max"),
        "fak_lohn": _sum(people, "ahv_lohn"), "kinderzulagen": kz,
        "nach_geschlecht": _by_sex(people, "ahv_lohn"),
        "beitraege": {"ahv_an": an_ahv, "ahv_ag": ag_ahv, "alv_an": an_alv, "alv_ag": ag_alv, "fak": fak,
                      "verwaltung": verwaltung},
        "total_beitraege": an_ahv + ag_ahv + an_alv + ag_alv + fak + verwaltung,
        "saldo": an_ahv + ag_ahv + an_alv + ag_alv + fak + verwaltung - kz,
    }
    uvg = {"uvg_lohn": _sum(people, "uvg_lohn_max"), "nbu_lohn": _sum(people, "nbu_lohn"),
           "nach_geschlecht": _by_sex(people, "uvg_lohn_max"),
           "nbu_nach_geschlecht": _by_sex(people, "nbu_lohn"),
           "beitraege": {"nbu_an": _sum(people, "an", "uvg"), "bu_ag": _sum(people, "ag", "uvg")}}
    ktg = {"ktg_lohn": _sum(people, "ktg_lohn"), "nach_geschlecht": _by_sex(people, "ktg_lohn"),
           "beitraege": {"an": _sum(people, "an", "ktg"), "ag": _sum(people, "ag", "ktg")}}
    bvg = {"beitraege": {"an": _sum(people, "an", "bvg"), "ag": _sum(people, "ag", "bvg")}}
    qst_rows = [dict(q, nummer=p["nummer"], name=p["name"], ahv_nr=p["ahv_nr"]) for p in people for q in p["qst"]]
    qst_kantone: dict[str, Decimal] = {}
    for q in qst_rows:
        qst_kantone[q["kanton"] or "?"] = qst_kantone.get(q["kanton"] or "?", ZERO) + q["betrag"]
    check = _ledger_check(book, year, cfg, people) if people else []
    for c in check:
        if c["differenz"]:
            hints.append(f"Konto {c['konto']} {c['name']}: Buchhaltung {c['buchhaltung']:.2f}, Lohnkonto "
                         f"{c['lohnkonto']:.2f} — Differenz {c['differenz']:.2f}; Buchungen ausserhalb "
                         "des Lohnlaufs prüfen")
    return {"jahr": year, "personen": people, "hinweise": hints, "hoechstlohn": hoechstlohn(year),
            "ausgleichskasse": ausgleichskasse, "uvg": uvg, "ktg": ktg, "bvg": bvg,
            "quellensteuer": {"zeilen": qst_rows, "nach_kanton": qst_kantone,
                              "total": sum(qst_kantone.values(), ZERO)},
            "abgleich": check}


# ---------- documents: one list of tables for PDF, Excel and CSV ----------

MONATE = ["", "Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]


def lohnkonto_tables(lk: dict) -> list[dict]:
    head = ["Lohnart / Abzug"] + MONATE[1:] + [f"Total {lk['jahr']}"]

    def rows(items):
        return [[r["label"]] + [r["monate"][m] for m in range(1, 13)] + [r["total"]] for r in items]

    bold = {"brutto", "zwischentotal", "netto"}
    return [{"titel": "Lohn und Abzüge", "kopf": head, "zeilen": rows(lk["zeilen"]),
             "totals": [i for i, r in enumerate(lk["zeilen"]) if r["stil"] in bold]},
            {"titel": "Arbeitgeberbeiträge", "kopf": head, "zeilen": rows(lk["ag_zeilen"]),
             "totals": [i for i, r in enumerate(lk["ag_zeilen"]) if r["stil"] in bold]}]


def meldung_tables(m: dict) -> list[dict]:
    people = m["personen"]
    year = m["jahr"]

    def period(p):
        return f"{_fmt(p['von'])}–{_fmt(p['bis'])}"

    def sex_rows(groups: dict, label: str):
        return [[f"{label} {g}", v] for g, v in groups.items()]

    ak, u, k, b, q = m["ausgleichskasse"], m["uvg"], m["ktg"], m["bvg"], m["quellensteuer"]
    tables = [
        {"titel": "Ausgleichskasse: Lohnbescheinigung AHV/IV/EO, ALV, FAK",
         "kopf": ["AHV-Nr.", "Name", "Geburtsdatum", "G.", "Beitragsperiode", "AHV-Lohn", "ALV-Lohn",
                  "Kinderzulagen"],
         "zeilen": [[p["ahv_nr"], p["name"], _fmt(parse_date(p["geburtsdatum"])) if p["geburtsdatum"] else "",
                     p["geschlecht"], period(p), p["ahv_lohn"], p["alv_lohn_max"], p["kinderzulagen"]]
                    for p in people] +
                   [["", "Total", "", "", "", ak["ahv_lohn"], ak["alv_lohn"], ak["kinderzulagen"]]],
         "totals": [len(people)]},
        {"titel": "Ausgleichskasse: Beiträge (Kontrolle)", "kopf": ["Position", "CHF"],
         "zeilen": [["AHV/IV/EO Arbeitnehmer", ak["beitraege"]["ahv_an"]],
                    ["AHV/IV/EO Arbeitgeber", ak["beitraege"]["ahv_ag"]],
                    ["ALV Arbeitnehmer", ak["beitraege"]["alv_an"]], ["ALV Arbeitgeber", ak["beitraege"]["alv_ag"]],
                    ["FAK Arbeitgeber", ak["beitraege"]["fak"]], ["Verwaltungskosten", ak["beitraege"]["verwaltung"]],
                    ["Total Beiträge", ak["total_beitraege"]],
                    ["./. ausbezahlte Kinderzulagen", -ak["kinderzulagen"]],
                    ["Saldo zugunsten Ausgleichskasse", ak["saldo"]]] +
                   sex_rows(ak["nach_geschlecht"], "AHV-Lohn"),
         "totals": [6, 8]},
        {"titel": "UVG: Lohnmeldung Unfallversicherung",
         "kopf": ["Name", "G.", "Beitragsperiode", "AHV-Lohn", "UVG-Lohn BU", "UVG-Lohn NBU", "Prämie NBU (AN)",
                  "Prämie BU (AG)"],
         "zeilen": [[p["name"], p["geschlecht"], period(p), p["ahv_lohn"], p["uvg_lohn_max"], p["nbu_lohn"],
                     p["an"]["uvg"], p["ag"]["uvg"]] for p in people] +
                   [["Total", "", "", ak["ahv_lohn"], u["uvg_lohn"], u["nbu_lohn"], u["beitraege"]["nbu_an"],
                     u["beitraege"]["bu_ag"]]] +
                   [[f"BU {g}", "", "", "", v, "", "", ""] for g, v in u["nach_geschlecht"].items()] +
                   [[f"NBU {g}", "", "", "", "", v, "", ""] for g, v in u["nbu_nach_geschlecht"].items()],
         "totals": [len(people)]},
        {"titel": "KTG: Lohnmeldung Krankentaggeld",
         "kopf": ["Name", "G.", "Beitragsperiode", "KTG-Lohn", "Prämie AN", "Prämie AG"],
         "zeilen": [[p["name"], p["geschlecht"], period(p), p["ktg_lohn"], p["an"]["ktg"], p["ag"]["ktg"]]
                    for p in people] +
                   [["Total", "", "", k["ktg_lohn"], k["beitraege"]["an"], k["beitraege"]["ag"]]] +
                   [[f"KTG-Lohn {g}", "", "", v, "", ""] for g, v in k["nach_geschlecht"].items()],
         "totals": [len(people)]},
        {"titel": "BVG: Beiträge berufliche Vorsorge",
         "kopf": ["AHV-Nr.", "Name", "Beitragsperiode", "AHV-Lohn", "Beitrag AN", "Beitrag AG", "Total"],
         "zeilen": [[p["ahv_nr"], p["name"], period(p), p["ahv_lohn"], p["an"]["bvg"], p["ag"]["bvg"],
                     p["an"]["bvg"] + p["ag"]["bvg"]] for p in people] +
                   [["", "Total", "", ak["ahv_lohn"], b["beitraege"]["an"], b["beitraege"]["ag"],
                     b["beitraege"]["an"] + b["beitraege"]["ag"]]],
         "totals": [len(people)]},
    ]
    if q["zeilen"]:
        tables.append({
            "titel": "Quellensteuer: Abrechnung pro Monat",
            "kopf": ["Monat", "AHV-Nr.", "Name", "Kanton", "Tarif", "QST-Basis", "Satz %", "Quellensteuer"],
            "zeilen": [[f"{r['monat']:02d}/{year}", r["ahv_nr"], r["name"], r["kanton"], r["tarif"], r["basis"],
                        money(r["satz"] * 100), r["betrag"]] for r in q["zeilen"]] +
                      [[f"Total {kt}", "", "", "", "", "", "", v] for kt, v in q["nach_kanton"].items()],
            "totals": list(range(len(q["zeilen"]), len(q["zeilen"]) + len(q["nach_kanton"])))})
    if m["abgleich"]:
        tables.append({"titel": "Abgleich mit der Buchhaltung",
                       "kopf": ["Konto", "Name", "Lohnkonto", "Buchhaltung", "Differenz"],
                       "zeilen": [[c["konto"], c["name"], c["lohnkonto"], c["buchhaltung"], c["differenz"]]
                                  for c in m["abgleich"]], "totals": []})
    return tables


def _is_num(v) -> bool:
    return isinstance(v, Decimal)


def pdf(book: Book, title: str, subtitle: str, tables: list[dict], hints: list[str] = ()) -> bytes:
    from . import pdf as pdfmod
    blocks = []
    if hints:
        blocks.append(("h2", "Prüfhinweise"))
        blocks += [("small", h) for h in hints]
    for t in tables:
        n = len(t["kopf"])
        right = tuple(i for i in range(n) if any(_is_num(r[i]) for r in t["zeilen"]))
        first = 0.16 if n > 10 else 0.22
        widths = [first] + [(1 - first) / (n - 1)] * (n - 1)
        rows = [[pdfmod.chf(v, True) if _is_num(v) else v for v in r] for r in t["zeilen"]]
        blocks += [("h2", t["titel"]), ("table", t["kopf"], rows, widths,
                                         {"right": right, "totals": tuple(t.get("totals", ())), "wrap": (0, 1)})]
    return pdfmod.report_pdf(book, title, blocks, subtitle=subtitle, wide=True)


def xlsx(book: Book, title: str, tables: list[dict], hints: list[str] = ()) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise BookError("Für Excel fehlt openpyxl: pip install 'aeradex[excel]' — oder als CSV exportieren") from None
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    bold = Font(bold=True)
    ws.append([f"{book.settings.firma} · {title}"])
    ws["A1"].font = Font(bold=True, size=12)
    for h in hints:
        ws.append([f"Hinweis: {h}"])
    width = 0
    for t in tables:
        ws.append([])
        ws.append([t["titel"]])
        ws.cell(ws.max_row, 1).font = bold
        ws.append(t["kopf"])
        for cell in ws[ws.max_row]:
            cell.font = bold
        for i, r in enumerate(t["zeilen"]):
            ws.append([float(v) if _is_num(v) else v for v in r])
            for j, v in enumerate(r, start=1):
                if _is_num(v):
                    ws.cell(ws.max_row, j).number_format = "#,##0.00"
                if i in t.get("totals", ()):
                    ws.cell(ws.max_row, j).font = bold
        width = max(width, len(t["kopf"]))
    ws.column_dimensions["A"].width = 28
    for j in range(2, width + 1):
        ws.column_dimensions[get_column_letter(j)].width = 14
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv(tables: list[dict]) -> bytes:
    from .dossier import csv_bytes
    rows: list[list] = []
    for t in tables:
        rows += [[t["titel"]], t["kopf"], *t["zeilen"], []]
    return csv_bytes(rows[0], rows[1:-1]) if rows else csv_bytes([], [])


def lohnkonto_document(book: Book, year: int, nr: str | None, fmt: str) -> tuple[str, bytes]:
    lk = lohnkonto_matrix(book, year, nr)
    tables = lohnkonto_tables(lk)
    title = f"Lohnkonto {year} {lk['bereich']}"
    note = [f"{lk['entwuerfe']} Entwurf/Entwürfe nicht enthalten"] if lk["entwuerfe"] else []
    return _document(book, title, "Abgeschlossene Lohnabrechnungen, Beträge in CHF", tables, note, fmt)


def meldung_document(book: Book, year: int, fmt: str) -> tuple[str, bytes]:
    m = jahresmeldung(book, year)
    return _document(book, f"Lohnmeldungen {year}",
                     "Abgeschlossene Lohnabrechnungen, Beträge in CHF · vor dem Einreichen die Prüfhinweise klären",
                     meldung_tables(m), m["hinweise"], fmt)


def _document(book, title, subtitle, tables, hints, fmt) -> tuple[str, bytes]:
    if fmt == "pdf":
        return f"{title}.pdf", pdf(book, title, subtitle, tables, hints)
    if fmt == "xlsx":
        return f"{title}.xlsx", xlsx(book, title, tables, hints)
    if fmt == "csv":
        return f"{title}.csv", csv(tables)
    raise BookError(f"Unbekanntes Format {fmt} (pdf, xlsx, csv)")


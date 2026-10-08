"""Quick entry: one line of text becomes a time or material entry.

    3.5h Müller Fassade spachteln          3.5 hours for the customer/project matching «Müller Fassade»
    2:30 P0003 Malerarbeiten Decke         2.5 hours on project P0003, Leistungsart «Malerarbeiten»
    8-12 gestern K0001 Beratung            08:00–12:00 yesterday
    12 l Dispersionsfarbe Huber            12 litres of the product «Dispersionsfarbe» for Huber
    1.5h intern Buchhaltung                not billable, category Intern
    start P0003 Spachteln                  without a duration: start the timer (stoppuhr.py)

Explicit numbers (K0001, P0003, P001, M0001/@lea) always win; names are matched word by word (prefixes, without
accents), longest match first. When two candidates fit equally well, nothing is guessed: the preview offers them.
Deterministic — no language model involved."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal

from aeradex import invoices
from aeradex.book import Book

from . import daten

WEEKDAYS = {"mo": 0, "montag": 0, "di": 1, "dienstag": 1, "mi": 2, "mittwoch": 2, "do": 3, "donnerstag": 3,
            "fr": 4, "freitag": 4, "sa": 5, "samstag": 5, "so": 6, "sonntag": 6}
UNITS = {"stk": "Stk", "st": "Stk", "stück": "Stk", "stueck": "Stk", "x": "Stk", "l": "l", "lt": "l", "liter": "l",
         "m": "m", "lm": "m", "m2": "m²", "m²": "m²", "qm": "m²", "m3": "m³", "m³": "m³", "kg": "kg", "g": "g",
         "pauschal": "Pauschal", "psch": "Pauschal", "pce": "Stk", "pack": "Pack", "rolle": "Rolle", "rollen": "Rolle"}
_DUR = [(re.compile(r"^(\d{1,2})[:.](\d{2})\s*[-–]\s*(\d{1,2})[:.](\d{2})$"), "range_hm"),     # 08:00-12:30
        (re.compile(r"^(\d{1,2})\s*[-–]\s*(\d{1,2})$"), "range_h"),                               # 8-12
        (re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:h|std|stunden?)$", re.I), "hours"),               # 3.5h
        (re.compile(r"^(\d{1,2}):(\d{2})$"), "hm"),                                                # 2:30
        (re.compile(r"^(\d+)\s*(?:min|m)$", re.I), "minutes")]                                     # 90min
_QTY = re.compile(r"^(\d+(?:[.,]\d+)?)$")
_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})?$")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,;/()]+", norm(text)) if w]


def _duration(tok: str) -> Decimal | None:
    for rx, kind in _DUR:
        m = rx.match(tok)
        if not m:
            continue
        g = [int(x) if x.isdigit() else x for x in m.groups()]
        if kind == "range_hm":
            minutes = (g[2] * 60 + g[3]) - (g[0] * 60 + g[1])
        elif kind == "range_h":
            if not (0 <= g[0] < 24 and 0 < g[1] <= 24 and g[1] > g[0]):
                return None
            minutes = (g[1] - g[0]) * 60
        elif kind == "hours":
            return Decimal(str(m.group(1)).replace(",", "."))
        elif kind == "hm":
            minutes = g[0] * 60 + g[1]
        else:
            minutes = g[0]
        return (Decimal(minutes) / 60).quantize(Decimal("0.01")) if minutes > 0 else None
    return None


def _date(tok: str, today: date) -> date | None:
    t = norm(tok)
    if t == "heute":
        return today
    if t == "gestern":
        return today - timedelta(days=1)
    if t == "vorgestern":
        return today - timedelta(days=2)
    if t in WEEKDAYS:                       # the most recent such day (today counts)
        return today - timedelta(days=(today.weekday() - WEEKDAYS[t]) % 7)
    m = _DATE.match(tok)
    if m:
        y = int(m.group(3)) + (2000 if m.group(3) and len(m.group(3)) == 2 else 0) if m.group(3) else today.year
        try:
            return date(y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", tok)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _score(words: list[str], name: str) -> int:
    """How many consecutive input words (from the start of `words`) are prefixes of the name's words, in order."""
    target = _words(name)
    n, j = 0, 0
    for w in words:
        while j < len(target) and not target[j].startswith(w):
            j += 1
        if j >= len(target) or len(w) < 2 and not w.isdigit():
            break
        n += 1
        j += 1
    return n


def _match(words: list[str], items: dict[str, str]) -> tuple[list[tuple[str, int, int]], int]:
    """Best (key, start, length) matches of a run of `words` against item names; all ties are returned."""
    best: list[tuple[str, int, int]] = []
    top = 0
    for start in range(len(words)):
        for key, name in items.items():
            n = _score(words[start:], name)
            if n > top:
                best, top = [(key, start, n)], n
            elif n == top and n:
                best.append((key, start, n))
    return best, top


def parse(book: Book, text: str, wer: str = "", today: date | None = None) -> dict:
    """The entry a line describes (not saved). Keys: art (Zeit/Produkt), datum, wer, stunden/menge, einheit,
    kunde, projekt, leistung, produkt, text, abrechenbar, kategorie, start (timer), kandidaten {feld: [(nr, name)]},
    fehlt [..], preis, betrag, budget (percent of the project's hour budget after this entry)."""
    today = today or date.today()
    cfg = daten.settings(book)
    people = {k: p for k, p in daten.people(book).items() if p.get("aktiv", True)}
    custs = {k: invoices.qr.invoice_name(c) for k, c in invoices.customers(book).items()}
    projs = {k: p for k, p in daten.projects(book).items() if p.get("status") != "abgeschlossen"}
    services = daten.services(book)
    goods = daten.materials(book)
    out = {"art": "Zeit", "datum": today, "wer": (wer or "").upper(), "stunden": None, "menge": None, "einheit": "",
           "kunde": "", "projekt": "", "leistung": "", "produkt": "", "text": "", "abrechenbar": True,
           "kategorie": "", "start": False, "kandidaten": {}, "fehlt": [], "preis": None, "betrag": None,
           "budget": None, "eingabe": text}
    tokens = [t for t in re.split(r"\s+", (text or "").strip()) if t]
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok, low = tokens[i], norm(tokens[i])
        nxt = norm(tokens[i + 1]) if i + 1 < len(tokens) else ""
        if i == 0 and low in ("start", "▶", ">"):
            out["start"] = True
        elif out["stunden"] is None and out["menge"] is None and (d := _duration(tok)) is not None:
            out["stunden"] = d
        elif out["stunden"] is None and out["menge"] is None and _QTY.match(tok) and nxt in UNITS:
            out["menge"], out["einheit"], out["art"] = Decimal(tok.replace(",", ".")), UNITS[nxt], "Produkt"
            i += 1
        elif out["menge"] is None and out["stunden"] is None and (m := re.match(r"^(\d+(?:[.,]\d+)?)(stk|l|m2|m²|kg|m)$", low)):
            out["menge"], out["einheit"], out["art"] = Decimal(m.group(1).replace(",", ".")), UNITS[m.group(2)], "Produkt"
        elif (d := _date(tok, today)) is not None:
            out["datum"] = d
        elif tok.upper() in projs:
            out["projekt"] = tok.upper()
        elif tok.upper() in custs:
            out["kunde"] = tok.upper()
        elif tok.upper() in services:
            out["leistung"] = tok.upper()
        elif tok.upper() in goods:
            out["produkt"], out["art"] = tok.upper(), "Produkt"
        elif tok.upper() in people:
            out["wer"] = tok.upper()
        elif tok.startswith("@") and len(tok) > 1:
            hit = [k for k, p in people.items() if norm(p["name"]).startswith(norm(tok[1:])) or
                   any(w.startswith(norm(tok[1:])) for w in _words(p["name"]))]
            if len(hit) == 1:
                out["wer"] = hit[0]
            else:
                out["kandidaten"]["wer"] = [(k, people[k]["name"]) for k in hit[:4]]
        elif low in {norm(k) for k in cfg["kategorien"]}:
            out["abrechenbar"] = False
            out["kategorie"] = next(k for k in cfg["kategorien"] if norm(k) == low)
        else:
            rest.append(tok)
        i += 1

    # Names: project first (it brings the customer), then customer, then Leistungsart / product; each match
    # consumes its words so the rest stays as the text.
    def consume(field: str, items: dict[str, str], minimum: int = 1) -> None:
        nonlocal rest
        if out[field] or not rest:
            return
        words = _words(" ".join(rest))
        if len(words) != len(rest):                  # punctuation split a token: match on whole tokens only
            words = [norm(t) for t in rest]
        best, top = _match(words, items)
        if top < minimum:
            return
        keys = list(dict.fromkeys(k for k, _, _ in best))
        if len(keys) > 1:
            out["kandidaten"][field] = [(k, items[k]) for k in keys[:4]]
            return
        key, start, n = best[0]
        out[field] = key
        rest = rest[:start] + rest[start + n:]

    if out["art"] == "Zeit":
        consume("projekt", {k: p["name"] for k, p in projs.items()})
    if out["projekt"] and rest:
        # «Beispiel Fassade spachteln»: the project's customer named alongside is no part of the text
        own = (projs.get(out["projekt"]) or {}).get("kunde", "")
        best, top = _match([norm(t) for t in rest], {own: custs.get(own, "")})
        if top:
            _, start, n = best[0]
            rest = rest[:start] + rest[start + n:]
    if not out["projekt"]:
        consume("kunde", custs)
    if out["art"] == "Produkt":
        consume("produkt", {k: p["text"] for k, p in goods.items()})
    else:
        consume("leistung", {k: p["text"] for k, p in services.items()}, minimum=1)
    out["text"] = " ".join(rest).strip(" :-–")

    # Defaults and what is missing
    if out["projekt"]:
        p = projs.get(out["projekt"]) or daten.project(book, out["projekt"])
        out["kunde"] = p["kunde"]
        if out["art"] == "Zeit" and out["abrechenbar"] and not out["text"] and not out["leistung"]:
            out["text"] = p["name"]                      # «2h Fassade»: the project names the work
    if not out["wer"]:
        out["wer"] = next(iter(people), "")
    if out["art"] == "Zeit":
        if out["stunden"] is None and not out["start"]:
            out["fehlt"].append("Dauer (z.B. 2.5h, 2:30, 8-12)")
        if out["abrechenbar"] and not out["kunde"] and "kunde" not in out["kandidaten"] and "projekt" not in out["kandidaten"]:
            out["fehlt"].append("Kunde oder Projekt (oder «intern», «Ferien» …)")
        if out["abrechenbar"] and not out["text"] and not out["leistung"]:
            out["fehlt"].append("Tätigkeit (erscheint auf der Rechnung)")
        if out["abrechenbar"] and out["kunde"] and out["wer"]:
            from .saetze import hourly_rate
            try:
                out["preis"] = hourly_rate(book, out["wer"], out["kunde"], out["projekt"], out["leistung"])
                if out["stunden"] is not None:
                    out["betrag"] = daten.money(out["preis"] * out["stunden"])
            except Exception as exc:                         # no rate anywhere: say so in the preview
                out["fehlt"].append(str(exc).split(" — ")[0])
        if out["projekt"] and out["stunden"] is not None:
            from . import kontrolle
            st = kontrolle.project(book, out["projekt"])
            if st.get("budget_stunden"):
                out["budget"] = int((Decimal(st["stunden"]) + out["stunden"]) * 100 / Decimal(st["budget_stunden"]))
    else:
        if not out["produkt"] and "produkt" not in out["kandidaten"]:
            out["fehlt"].append("Produkt (Name oder Nummer)")
        if not out["kunde"] and "kunde" not in out["kandidaten"]:
            out["fehlt"].append("Kunde oder Projekt")
        if out["produkt"] and out["kunde"]:
            from .saetze import product_price
            out["preis"] = product_price(goods[out["produkt"]], out["kunde"])
            out["betrag"] = daten.money(out["preis"] * out["menge"])
    out["ok"] = not out["fehlt"] and not out["kandidaten"]
    return out


def save(book: Book, text: str, wer: str = "", today: date | None = None) -> tuple[dict, list]:
    """Parse and record (time or material). Refuses when anything is missing or ambiguous."""
    from aeradex.book import BookError
    p = parse(book, text, wer, today)
    if p["kandidaten"]:
        field, options = next(iter(p["kandidaten"].items()))
        raise BookError(f"Nicht eindeutig ({field}): {', '.join(f'{k} {n}' for k, n in options)} — Nummer angeben")
    if p["fehlt"]:
        raise BookError("Es fehlt: " + "; ".join(p["fehlt"]))
    if p["start"] and p["art"] == "Zeit" and p["stunden"] is None:
        raise BookError("Ohne Dauer startet die Stoppuhr — dafür ▶ verwenden")
    if p["art"] == "Zeit":
        return daten.add_time(book, p["datum"], p["wer"], p["stunden"], p["text"], p["kunde"], p["projekt"],
                              p["abrechenbar"], p["kategorie"], None, p["leistung"])
    return daten.add_material(book, p["datum"], p["kunde"], p["produkt"], p["menge"], p["projekt"], None,
                              p["text"], p["wer"])

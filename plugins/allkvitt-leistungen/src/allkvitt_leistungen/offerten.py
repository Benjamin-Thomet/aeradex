"""Quotes (Offerten): offerten/<JJJJ>/O-JJJJ-NNNN.md with a PDF next to it.

A quote has the same positions as an invoice. Status: entwurf → versendet →
angenommen | abgelehnt. Accepting it opens a project with the quote as budget:

* `pauschal`: the quote is invoiced as agreed — in full, or in partial invoices
  (percent or amount) and a final invoice that deducts them;
* `aufwand`: the quote is the budget, the recorded time and products are billed.

Quotes book nothing; invoices made from them are normal allkvitt invoices.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from allkvitt import api, invoices
from allkvitt import mwst as vat
from allkvitt import pdf as core_pdf
from allkvitt.book import Book, BookError
from allkvitt.files import parse_amount, parse_date, read_frontmatter, write_frontmatter

from . import daten, saetze

ZERO = Decimal("0")
STATUS = ("entwurf", "versendet", "angenommen", "abgelehnt")


def paths(book: Book) -> list[Path]:
    folder = book.root / "offerten"
    return sorted(folder.glob("*/O-*.md")) if folder.exists() else []


def quotes(book: Book) -> dict[str, dict]:
    out = {}
    for path in paths(book):
        meta, body = read_frontmatter(path)
        meta["_pfad"] = path
        meta["_text"] = body.strip()
        out[str(meta.get("nummer"))] = meta
    return out


def quote(book: Book, nr: str) -> dict:
    found = quotes(book).get(str(nr).strip().upper())
    if found is None:
        raise BookError(f"Offerte {nr} nicht gefunden")
    return found


def _next_number(book: Book, year: int) -> str:
    prefix = f"O-{year}-"
    nums = [int(k[len(prefix):]) for k in quotes(book) if k.startswith(prefix) and k[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:04d}"


def resolve_positions(book: Book, kunde: str, raw: list[dict]) -> list[dict]:
    """Positions from products ({produkt, menge}), hours ({stunden, text, preis?, wer?}) or free text
    ({text, menge, preis, einheit?, konto?, mwst?}) → normalized invoice positions with `art`."""
    cfg = daten.settings(book)
    taxed = vat.config(book)["methode"] != "keine"
    resolved, kinds = [], []
    for i, p in enumerate(raw, 1):
        p = {k: v for k, v in p.items() if v not in (None, "")}
        if p.get("produkt"):
            prod = daten.product(book, p["produkt"])
            item = {"text": p.get("text") or prod["text"], "menge": p.get("menge", 1), "einheit": prod.get("einheit"),
                    "preis": p.get("preis", saetze.product_price(prod, kunde)),
                    "konto": prod.get("konto") or cfg["konto_produkte"]}
            if taxed and (p.get("mwst") or prod.get("mwst")):
                item["mwst"] = p.get("mwst") or prod.get("mwst")
            kinds.append(("produkt", prod["nummer"]))
        elif p.get("stunden") is not None:
            if p.get("preis") is None:
                if p.get("wer"):
                    p["preis"] = saetze.hourly_rate(book, p["wer"], kunde)
                elif invoices.customer(book, kunde).get("stundensatz") not in (None, ""):
                    p["preis"] = invoices.customer(book, kunde)["stundensatz"]
                else:
                    raise BookError(f"Position {i}: Stundensatz fehlt (preis oder wer angeben)")
            item = {"text": p.get("text") or "Arbeit", "menge": p["stunden"], "einheit": "h", "preis": p["preis"],
                    "konto": cfg["konto_stunden"]}
            if taxed and p.get("mwst"):
                item["mwst"] = p["mwst"]
            kinds.append(("stunden", ""))
        else:
            item = {k: p[k] for k in ("text", "menge", "einheit", "preis", "konto", "mwst") if k in p}
            if not taxed:
                item.pop("mwst", None)
            kinds.append(("frei", ""))
        resolved.append(item)
    pos = invoices.normalize_positions(resolved, cfg["konto_stunden"],
                                       "U81" if taxed else "")
    for p in pos:
        book.account(p["konto"])
    return [{**p, "menge": invoices._num(p["menge"]), "art": art, **({"produkt": nr} if nr else {})}
            for p, (art, nr) in zip(pos, kinds)]


def _sums(meta: dict, pos: list[dict]) -> None:
    breakdown = invoices.mwst_breakdown(pos)
    meta["netto"] = sum((daten.money(p["betrag"]) for p in pos), ZERO)
    meta["mwst"] = breakdown
    meta["total"] = meta["netto"] + sum((b["steuer"] for b in breakdown), ZERO)


def _write(book: Book, meta: dict, text: str) -> list[Path]:
    path = book.root / "offerten" / meta["datum"][:4] / f"{meta['nummer']}.md"
    clean = {k: v for k, v in meta.items() if not k.startswith("_")}
    write_frontmatter(path, clean, text)
    from .pdf import quote_pdf
    pdf_path = path.with_suffix(".pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(quote_pdf(book, {**clean, "_text": text}))
    return [path, pdf_path]


def create(book: Book, kunde: str, positionen: list[dict], datum=None, titel: str = "", text: str = "",
           gueltig_tage: int | None = None, abrechnung: str = "pauschal") -> tuple[dict, list[Path]]:
    cust = invoices.customer(book, kunde)
    if abrechnung not in daten.ABRECHNUNG:
        raise BookError("Abrechnung: pauschal oder aufwand")
    d = parse_date(datum, "datum") if datum else date.today()
    days = int(gueltig_tage or daten.settings(book)["gueltig_tage"])
    pos = resolve_positions(book, cust["nummer"], positionen)
    meta = {"nummer": _next_number(book, d.year), "kunde": cust["nummer"], "an": invoices._snapshot_address(cust),
            "titel": titel.strip(), "datum": d.isoformat(), "gueltig_bis": (d + timedelta(days=days)).isoformat(),
            "waehrung": "CHF", "positionen": pos, "abrechnung": abrechnung, "status": "entwurf",
            "projekt": "", "rechnungen": []}
    _sums(meta, pos)
    if meta["total"] <= 0:
        raise BookError("Offertsumme muss positiv sein")
    return meta, _write(book, meta, text)


def update(book: Book, nr: str, positionen: list[dict] | None = None, titel: str | None = None,
           text: str | None = None, gueltig_tage: int | None = None, abrechnung: str | None = None) -> tuple[dict, list[Path]]:
    """Revise a quote that is not accepted or declined yet (the PDF is regenerated)."""
    meta = quote(book, nr)
    if meta["status"] in ("angenommen", "abgelehnt"):
        raise BookError(f"Offerte {nr} ist {meta['status']} — nicht mehr änderbar")
    meta.pop("_pfad")
    body = meta.pop("_text", "")
    if positionen is not None:
        meta["positionen"] = resolve_positions(book, meta["kunde"], positionen)
        _sums(meta, meta["positionen"])
    if titel is not None:
        meta["titel"] = titel.strip()
    if gueltig_tage:
        meta["gueltig_bis"] = (parse_date(meta["datum"]) + timedelta(days=int(gueltig_tage))).isoformat()
    if abrechnung:
        if abrechnung not in daten.ABRECHNUNG:
            raise BookError("Abrechnung: pauschal oder aufwand")
        meta["abrechnung"] = abrechnung
    return meta, _write(book, meta, body if text is None else text)


def set_status(book: Book, nr: str, status: str, projektname: str = "") -> tuple[dict, list[Path]]:
    """entwurf | versendet | abgelehnt — or angenommen, which opens the project (budget = quote)."""
    if status not in STATUS:
        raise BookError(f"Status: {', '.join(STATUS)}")
    meta = quote(book, nr)
    if meta["status"] == "angenommen":
        raise BookError(f"Offerte {nr} ist angenommen — Status bleibt (Projekt {meta.get('projekt')})")
    path = meta.pop("_pfad")
    body = meta.pop("_text", "")
    touched = [path]
    meta["status"] = status
    if status == "angenommen":
        hours = [p for p in meta["positionen"] if p.get("art") == "stunden"]
        rates = {daten.money(p["preis"]) for p in hours}
        proj, ppaths = daten.add_project(
            book, meta["kunde"], projektname or meta.get("titel") or f"Offerte {meta['nummer']}",
            meta.get("abrechnung") or "pauschal",
            budget_stunden=sum((Decimal(str(p["menge"])) for p in hours), ZERO) if hours else None,
            budget_chf=meta["netto"], satz=next(iter(rates)) if len(rates) == 1 else None, offerte=meta["nummer"])
        meta["projekt"] = proj["nummer"]
        meta["angenommen_am"] = date.today().isoformat()
        touched += ppaths
    write_frontmatter(path, meta, body)
    return meta, touched


# ---------- invoices from a flat-rate quote ----------

def _invoice_status(book: Book) -> dict[str, str]:
    return {nr: str(m.get("status") or "aktiv") for nr, m in invoices.invoices(book).items()}


def billed(book: Book, meta: dict) -> list[dict]:
    """The quote's invoices that are not voided."""
    status = _invoice_status(book)
    return [r for r in meta.get("rechnungen") or [] if status.get(r["nummer"]) not in (None, "storniert")]


def remaining(book: Book, meta: dict) -> Decimal:
    done = billed(book, meta)
    if any(r["art"] in ("voll", "schluss") for r in done):
        return ZERO
    return daten.money(meta["netto"]) - sum((daten.money(r["netto"]) for r in done), ZERO)


def _groups(meta: dict) -> dict[tuple[str, str], Decimal]:
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for p in meta["positionen"]:
        out[(str(p["konto"]), str(p.get("mwst") or ""))] += daten.money(p["betrag"])
    return dict(out)


def _labels(book: Book, meta: dict) -> dict[tuple[str, str], str]:
    """A customer-facing name per account group, shown only when a quote has several groups:
    «Arbeit» (hours), «Material» (products), otherwise the account's name."""
    kinds: dict[tuple[str, str], set[str]] = defaultdict(set)
    for p in meta["positionen"]:
        kinds[(str(p["konto"]), str(p.get("mwst") or ""))].add(p.get("art") or "frei")
    if len(kinds) < 2:
        return {k: "" for k in kinds}
    out = {}
    for (konto, code), arts in kinds.items():
        name = {frozenset({"stunden"}): "Arbeit", frozenset({"produkt"}): "Material"}.get(frozenset(arts))
        if name is None:
            name = book.accounts[konto].name if konto in book.accounts else konto
        out[(konto, code)] = f"Anteil {name}" + (f", {code}" if code and len({c for _, c in kinds}) > 1 else "")
    return out


def _split(total: Decimal, groups: dict[tuple[str, str], Decimal]) -> dict[tuple[str, str], Decimal]:
    """Share `total` across the groups in proportion; the rounding Rappen go to the largest group."""
    base = sum(groups.values(), ZERO)
    out = {k: (total * v / base).quantize(daten.CENT, rounding=ROUND_HALF_UP) for k, v in groups.items()}
    largest = max(groups, key=lambda k: (groups[k], k))
    out[largest] += total - sum(out.values(), ZERO)
    return {k: v for k, v in out.items() if v}


def invoice(book: Book, nr: str, anteil=None, betrag=None, schluss: bool = False, datum=None,
            text: str = "", zahlungsfrist: int | None = None) -> tuple[dict, list[Path]]:
    """Invoice an accepted flat-rate quote: in full (no anteil/betrag), a partial invoice
    (`anteil` percent or `betrag` net CHF), or the final invoice (`schluss`) that deducts the partial ones."""
    meta = quote(book, nr)
    if meta["status"] != "angenommen":
        raise BookError(f"Offerte {nr} ist nicht angenommen")
    if meta.get("abrechnung") != "pauschal":
        raise BookError(f"Offerte {nr} wird nach Aufwand abgerechnet — unter Abrechnen die Leistungen verrechnen")
    open_ = remaining(book, meta)
    if open_ <= 0:
        raise BookError(f"Offerte {nr} ist vollständig verrechnet")
    done = billed(book, meta)
    partials = [r for r in done if r["art"] == "teil"]
    groups = _groups(meta)
    labels = _labels(book, meta)

    def suffix(konto, code):
        label = labels.get((str(konto), str(code or "")), "")
        return f" ({label})" if label else ""
    title = meta.get("titel") or f"Offerte {meta['nummer']}"
    if anteil not in (None, "") or betrag not in (None, ""):
        if schluss:
            raise BookError("Schlussrechnung ohne Anteil/Betrag")
        amount = (daten.money(daten.money(meta["netto"]) * parse_amount(anteil, "anteil") / 100) if anteil not in (None, "")
                  else daten.money(parse_amount(betrag, "betrag")))
        if amount <= 0 or amount >= open_:
            raise BookError(f"Teilrechnung muss zwischen 0 und dem offenen Betrag {open_:.2f} liegen "
                            "(für den Rest die Schlussrechnung)")
        shares = _split(amount, groups)
        n = len(partials) + 1
        raw = [{"text": f"{n}. Teilrechnung «{title}» gemäss Offerte {meta['nummer']}{suffix(k, m)}", "menge": 1, "preis": v,
                "konto": k, "mwst": m} for (k, m), v in sorted(shares.items())]
        art, teile = "teil", [{"konto": k, "mwst": m, "betrag": v} for (k, m), v in sorted(shares.items())]
    else:
        raw = [{k: p[k] for k in ("text", "menge", "einheit", "preis", "konto", "mwst") if k in p}
               for p in meta["positionen"]]
        for r in partials:
            for t in r.get("teile") or []:
                raw.append({"text": f"abzüglich Teilrechnung {r['nummer']}{suffix(t['konto'], t['mwst'])}", "menge": 1,
                            "preis": -daten.money(t["betrag"]), "konto": t["konto"], "mwst": t["mwst"]})
        art, teile = ("schluss" if partials else "voll"), []
    if not text:
        text = {"teil": f"Teilrechnung gemäss unserer Offerte {meta['nummer']} vom "
                        f"{parse_date(meta['datum']).strftime('%d.%m.%Y')}.",
                "schluss": f"Schlussrechnung gemäss unserer Offerte {meta['nummer']}.",
                "voll": f"Gemäss unserer Offerte {meta['nummer']} vom "
                        f"{parse_date(meta['datum']).strftime('%d.%m.%Y')}."}[art]
    inv, touched = invoices.issue_invoice(book, meta["kunde"], raw, datum, text, zahlungsfrist)
    inv["_text"] = text
    pdf_path = api.meta_pdf_path(book, inv)
    pdf_path.write_bytes(core_pdf.invoice_pdf(book, inv, invoices.customer(book, meta["kunde"])))
    netto = sum((daten.money(p["betrag"]) for p in inv["positionen"]), ZERO)
    path = meta.pop("_pfad")
    body = meta.pop("_text", "")
    meta["rechnungen"] = list(meta.get("rechnungen") or []) + [
        {"nummer": inv["nummer"], "art": art, "netto": netto, **({"teile": teile} if teile else {})}]
    write_frontmatter(path, meta, body)
    return ({"rechnung": inv["nummer"], "art": art, "netto": netto, "total": inv["total"],
             "offen_nach": remaining(book, {**meta})}, touched + [pdf_path, path])


def expired(book: Book, today: date | None = None) -> list[str]:
    today = today or date.today()
    return [nr for nr, m in quotes(book).items()
            if m.get("status") == "versendet" and parse_date(m["gueltig_bis"]) < today]


"""Mahnwesen: reminders for overdue invoices.

    mahnungen/R-JJJJ-NNNN.yaml        the reminders sent for an invoice (stufe, datum, frist, pdf)
    mahnungen/R-JJJJ-NNNN-M<n>.pdf    the reminder letter with a QR-bill for the open amount

Three steps: Zahlungserinnerung, 2. Mahnung, 3. (letzte) Mahnung. A step can follow
once the deadline of the previous one has passed. The QR-bill carries the invoice's
own reference, so a payment made from a reminder is matched by the bank import like
any other. No fees and no interest are added: in Switzerland both need a contractual
basis (AGB), and they would change the invoice's open amount.

Settings (batzen.yaml):

    mahnwesen:
      frist_tage: 10        # deadline set by each reminder
      texte: {1: "…", 2: "…", 3: "…"}   # optional own wording; {frist} {vorher} {nummer}
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .book import Book, BookError
from .files import parse_date, read_yaml, write_yaml

STUFEN = {1: "Zahlungserinnerung", 2: "2. Mahnung", 3: "3. Mahnung"}
TEXTE = {
    1: ("Sicher ist Ihnen entgangen, dass die folgende Rechnung noch offen ist. Wir bitten Sie, den offenen "
        "Betrag bis {frist} zu überweisen. Sollte sich Ihre Zahlung mit diesem Schreiben gekreuzt haben, "
        "betrachten Sie es bitte als gegenstandslos."),
    2: ("Trotz unserer Zahlungserinnerung vom {vorher} ist die folgende Rechnung noch offen. Bitte überweisen "
        "Sie den offenen Betrag bis {frist}. Haben Sie Fragen zur Rechnung, melden Sie sich bitte bei uns."),
    3: ("Die folgende Rechnung ist trotz zweier Mahnungen noch offen. Wir fordern Sie letztmals auf, den offenen "
        "Betrag bis {frist} zu bezahlen. Nach Ablauf dieser Frist leiten wir ohne weitere Mitteilung die "
        "Betreibung ein."),
}


def config(book: Book) -> dict:
    raw = book.settings.get("mahnwesen") or {}
    return {"frist_tage": int(raw.get("frist_tage") or 10),
            "texte": {**TEXTE, **{int(k): str(v) for k, v in (raw.get("texte") or {}).items()}}}


def path(book: Book, nr: str) -> Path:
    return book.root / "mahnungen" / f"{nr}.yaml"


def history(book: Book, nr: str) -> list[dict]:
    p = path(book, nr)
    return list((read_yaml(p) or {}).get("mahnungen") or []) if p.exists() else []


def overdue(book: Book, as_of: date | None = None) -> list[dict]:
    """Open invoices past their due date, with the reminders sent and what can come next."""
    from . import invoices
    as_of = as_of or date.today()
    paid = invoices.settlements(book)
    out = []
    for nr, meta in invoices.invoices(book).items():
        st = invoices.invoice_state(book, meta, paid.get(nr, []))
        if st["status"] not in ("offen", "teilbezahlt") or parse_date(st["faellig"]) >= as_of:
            continue
        sent = history(book, nr)
        last = sent[-1] if sent else None
        next_step = len(sent) + 1 if len(sent) < 3 else None
        ready_from = parse_date(last["frist"]) + timedelta(days=1) if last else parse_date(st["faellig"]) + timedelta(days=1)
        out.append({**st, "tage": (as_of - parse_date(st["faellig"])).days, "mahnungen": sent,
                    "letzte": last, "naechste_stufe": next_step,
                    "naechste_bezeichnung": STUFEN.get(next_step, "Betreibung prüfen"),
                    "bereit": next_step is not None and as_of >= ready_from, "ab": ready_from})
    out.sort(key=lambda r: (-r["tage"], r["nummer"]))
    return out


def create(book: Book, nr: str, datum=None, frist_tage: int | None = None) -> tuple[dict, list[Path]]:
    """Write the next reminder for an invoice (PDF with QR-bill for the open amount)."""
    from . import invoices, pdf
    when = parse_date(datum, "datum") if datum else date.today()
    item = next((r for r in overdue(book, when) if r["nummer"] == nr), None)
    if item is None:
        meta = invoices.invoice(book, nr)
        st = invoices.invoice_state(book, meta)
        if st["status"] not in ("offen", "teilbezahlt"):
            raise BookError(f"{nr} ist {st['status']} — keine Mahnung nötig")
        raise BookError(f"{nr} ist erst am {parse_date(st['faellig']):%d.%m.%Y} fällig")
    if item["naechste_stufe"] is None:
        raise BookError(f"{nr} wurde bereits dreimal gemahnt — als Nächstes die Betreibung prüfen")
    if not item["bereit"]:
        raise BookError(f"Die Frist der {item['letzte']['bezeichnung']} läuft bis "
                        f"{parse_date(item['letzte']['frist']):%d.%m.%Y}")
    cfg = config(book)
    stufe = item["naechste_stufe"]
    frist = when + timedelta(days=int(frist_tage or cfg["frist_tage"]))
    meta = invoices.invoice(book, nr)
    customer = invoices.customers(book).get(meta.get("kunde"))
    vorher = item["letzte"]["datum"] if item["letzte"] else ""
    text = cfg["texte"][stufe].format(frist=f"{frist:%d.%m.%Y}", nummer=nr,
                                      vorher=f"{parse_date(vorher):%d.%m.%Y}" if vorher else "")
    entry = {"stufe": stufe, "bezeichnung": STUFEN[stufe], "datum": when.isoformat(), "frist": frist.isoformat(),
             "offen": f"{item['offen']:.2f}", "pdf": f"mahnungen/{nr}-M{stufe}.pdf"}
    target = book.root / entry["pdf"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf.reminder_pdf(book, meta, item, entry, text, customer))
    sent = history(book, nr) + [entry]
    write_yaml(path(book, nr), {"rechnung": nr, "mahnungen": sent})
    return entry, [target, path(book, nr)]

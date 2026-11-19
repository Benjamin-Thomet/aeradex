"""Payment files to the bank: the pain.001 aeradex already makes (Kreditoren payment runs, salary payments), sent
with the participant's electronic signature, and the bank's status reports (pain.002) read back.

Money leaves the account here, so: only a person sends (never the agent), only files aeradex made, only once
(by content and by MsgId), only while unchanged (the total the person saw must still be in the file), and never
with an execution date in the past. What was sent is kept in `.aeradex/lokal/ebics/zahlungen.yaml`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import zipfile
from decimal import Decimal
from pathlib import Path

import yaml
from aeradex.book import BookError
from lxml import etree

from . import ebics
from . import protokoll as p

NS001 = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
OK = {"ACCP", "ACTC", "ACSP", "ACSC", "ACWC", "ACFC"}
TEXT = {"ACCP": "angenommen", "ACTC": "angenommen (technisch geprüft)", "ACSP": "in Ausführung",
        "ACSC": "ausgeführt", "ACWC": "angenommen mit Änderung", "ACFC": "angenommen", "PART": "teilweise abgelehnt",
        "RJCT": "abgelehnt", "PDNG": "ausstehend", "RCVD": "erhalten"}


def _registry_path(book) -> Path:
    return ebics.ordner(book) / "zahlungen.yaml"


def gesendet(book) -> dict:
    path = _registry_path(book)
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _save(book, data: dict) -> None:
    path = _registry_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def lesen(data: bytes) -> dict:
    """MsgId, number, total and execution date of a pain.001.001.09 — refuses anything else."""
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        raise BookError(f"Keine gültige Zahlungsdatei: {exc}") from None
    if etree.QName(root).namespace != NS001:
        raise BookError("Nur pain.001.001.09 (Swiss Payment Standards) wird gesendet")
    n = {"p": NS001}
    hdr = root.find("p:CstmrCdtTrfInitn/p:GrpHdr", n)
    dates = sorted({d.text for d in root.iterfind(".//p:PmtInf/p:ReqdExctnDt/p:Dt", n)})
    return {"msg_id": hdr.findtext("p:MsgId", namespaces=n), "anzahl": int(hdr.findtext("p:NbOfTxs", namespaces=n)),
            "total": Decimal(hdr.findtext("p:CtrlSum", namespaces=n)), "ausfuehrung": dates[0] if dates else "",
            "konten": sorted({i.text for i in root.iterfind(".//p:DbtrAcct/p:Id/p:IBAN", n)})}


def dateien(book) -> list[dict]:
    """Every payment file of the book with what EBICS knows about it, newest first."""
    sent = gesendet(book)
    found = []
    for path in sorted((book.root / "zahlungen").glob("*.xml")) if (book.root / "zahlungen").exists() else []:
        found.append((path, "Kreditoren", None))
    for manifest in sorted(book.root.glob("lohn/*/*/zahlung.yaml")):
        meta = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        if meta.get("status") == "erstellt" and meta.get("datei"):
            found.append((book.root / meta["datei"], "Lohn", meta))
    out = []
    for path, art, meta in found:
        if not path.exists():
            continue
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        try:
            info = lesen(data)
        except BookError as exc:
            out.append({"pfad": book.rel(path), "art": art, "fehler": str(exc), "sha256": sha})
            continue
        if meta and meta.get("sha256") != sha:
            info["fehler"] = "Datei wurde nach dem Erstellen verändert — nicht senden"
        out.append({"pfad": book.rel(path), "art": art, "sha256": sha, **info, "total": f"{info['total']:.2f}",
                    "ebics": sent.get(sha) or next((s for s in sent.values() if s.get("msg_id") == info["msg_id"]), None)})
    out.sort(key=lambda d: (d.get("ausfuehrung", ""), d["pfad"]), reverse=True)
    return out


def senden(book, pfad: str, total: str, wer: str = "", post=None, heute: dt.date | None = None) -> dict:
    """Send one payment file. `total` is what the person confirmed; it must still be the file's CtrlSum."""
    if ebics.status(book)["schritt"] != "aktiv":
        raise BookError("EBICS ist noch nicht aktiv")
    entry = next((d for d in dateien(book) if d["pfad"] == pfad), None)
    if entry is None:
        raise BookError(f"Zahlungsdatei {pfad} nicht gefunden (nur Zahlungsläufe und Lohnzahlungen aus aeradex)")
    if entry.get("fehler"):
        raise BookError(entry["fehler"])
    if entry["ebics"]:
        raise BookError(f"Diese Zahlungen wurden schon am {entry['ebics']['gesendet'][:16].replace('T', ' ')} an die "
                        "Bank gesendet — nicht nochmals")
    if Decimal(str(total or "0")) != Decimal(entry["total"]):
        raise BookError(f"Der Betrag hat sich geändert (Datei: {entry['total']}) — Seite neu laden und nochmals prüfen")
    heute = heute or dt.date.today()
    if entry["ausfuehrung"] and entry["ausfuehrung"] < heute.isoformat():
        raise BookError(f"Ausführungsdatum {entry['ausfuehrung']} liegt in der Vergangenheit — Zahlungslauf neu "
                        "erstellen")
    path = book.root / pfad
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise BookError("Die Datei hat sich eben geändert — Seite neu laden")
    c = ebics.client(book, post=post)
    try:
        tid, order_id = c.upload(p.PAIN001, data, path.name)
    except p.EbicsFehler as exc:
        ebics.protokoll(book, "zahlung", False, f"{pfad}: {exc}", code=exc.code)
        raise
    sent = gesendet(book)
    sent[entry["sha256"]] = {"pfad": pfad, "art": entry["art"], "msg_id": entry["msg_id"], "anzahl": entry["anzahl"],
                             "total": entry["total"], "ausfuehrung": entry["ausfuehrung"], "gesendet": ebics._now(),
                             "von": wer, "auftrag": order_id, "transaktion": tid, "status": "gesendet"}
    _save(book, sent)
    ebics.protokoll(book, "zahlung", True, f"{pfad}: {entry['anzahl']} Zahlung(en), CHF {entry['total']} gesendet"
                    + (f" (Auftrag {order_id})" if order_id else ""), transaktion=tid)
    return sent[entry["sha256"]]


# ---------- status reports (pain.002) ----------

def pain002(data: bytes) -> list[dict]:
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    ns = etree.QName(root).namespace or ""
    n = {"p": ns}
    out = []
    for grp in root.iterfind(".//p:OrgnlGrpInfAndSts", n):
        msg = grp.findtext("p:OrgnlMsgId", namespaces=n) or ""
        status = grp.findtext("p:GrpSts", namespaces=n) or ""
        txs = []
        for tx in root.iterfind(".//p:TxInfAndSts", n):
            code = tx.findtext("p:TxSts", namespaces=n) or ""
            reason = " ".join(filter(None, [tx.findtext("p:StsRsnInf/p:Rsn/p:Cd", namespaces=n) or "",
                                            " ".join(t.text or "" for t in tx.iterfind("p:StsRsnInf/p:AddtlInf", n))]))
            txs.append({"e2e": tx.findtext("p:OrgnlEndToEndId", namespaces=n) or "", "status": code,
                        "grund": reason.strip()})
        if not status:
            pmt = [s.text for s in root.iterfind(".//p:OrgnlPmtInfAndSts/p:PmtInfSts", n) if s.text]
            states = set(pmt) | {t["status"] for t in txs if t["status"]}
            status = "RJCT" if states == {"RJCT"} else "PART" if "RJCT" in states else (pmt[0] if pmt else "")
        grund = " ".join(filter(None, [grp.findtext("p:StsRsnInf/p:Rsn/p:Cd", namespaces=n) or "",
                                       " ".join(t.text or "" for t in grp.iterfind("p:StsRsnInf/p:AddtlInf", n))]))
        out.append({"msg_id": msg, "status": status, "grund": grund.strip(), "transaktionen": txs})
    return out


def offen(book) -> list[dict]:
    return [s for s in gesendet(book).values() if s.get("status") not in ("ausgeführt", "abgelehnt")]


def status_abholen(book, client) -> dict:
    """Fetch status reports while sent payments wait for a final answer. Errors only go to the log:
    a missing status must never stop the statements."""
    if not offen(book):
        return {"statusmeldungen": 0}
    count = 0
    try:
        for _ in range(10):
            got = client.download(p.PAIN002)
            if got is None:
                break
            tid, data = got
            folder = ebics.ordner(book) / "status"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            (folder / f"{stamp}-{tid[:8]}.bin").write_bytes(data)
            client.quittung(tid)
            for name, xml in _files(data):
                count += anwenden(book, xml)
    except (p.EbicsFehler, BookError, etree.XMLSyntaxError) as exc:
        ebics.protokoll(book, "status", False, f"Statusmeldungen: {exc}")
    return {"statusmeldungen": count}


def _files(data: bytes):
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".xml")]
    return [("status.xml", data)]


def anwenden(book, xml: bytes) -> int:
    sent = gesendet(book)
    by_msg = {s["msg_id"]: key for key, s in sent.items()}
    changed = 0
    for report in pain002(xml):
        key = by_msg.get(report["msg_id"])
        if key is None:
            continue
        code = report["status"]
        entry = sent[key]
        entry["status"] = ("ausgeführt" if code == "ACSC" else "abgelehnt" if code == "RJCT" else
                           "teilweise abgelehnt" if code == "PART" else "angenommen" if code in OK else
                           TEXT.get(code, code or entry.get("status", "")))
        entry["bank_code"] = code
        entry["status_zeit"] = ebics._now()
        bad = [t for t in report["transaktionen"] if t["status"] == "RJCT"]
        entry["abgelehnt"] = bad
        if report["grund"]:
            entry["grund"] = report["grund"]
        ebics.protokoll(book, "status", code not in ("RJCT", "PART"),
                        f"{entry['pfad']}: {TEXT.get(code, code)}" + (f" — {report['grund']}" if report["grund"] else "")
                        + (f" ({len(bad)} abgelehnt)" if bad else ""))
        changed += 1
    _save(book, sent)
    return changed

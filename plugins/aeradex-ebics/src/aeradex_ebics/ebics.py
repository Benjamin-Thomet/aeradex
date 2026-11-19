"""EBICS for one book: participant, keys, the steps with the bank, fetching and importing statements.

Everything lives outside git in `<book>/.aeradex/lokal/ebics/` — the private keys must never land in the
book's history or in a copy of the book:

- `teilnehmer.yaml`  connection (URL, Host-ID, Partner-ID, Teilnehmer-ID), the bank's hashes from its letter,
                     the state of the steps, the last fetch
- `schluessel.yaml`  the three private keys (PEM, encrypted when AERADEX_EBICS_PASSWORT is set) with their
                     certificates and, after HPB, the bank's certificates. File mode 0600.
- `eingang/`         what the bank delivered, kept until it is imported (then `archiv/`)
- `protokoll.jsonl`  every exchange with the bank

The statements themselves go through the normal bank import (`api.bank_import`): stored under
bank/auszuege/, movements matched, nothing imported twice.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import io
import json
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import yaml
from aeradex import api
from aeradex.book import BookError
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from . import protokoll as p

SCHRITTE = ("einrichten", "senden", "brief", "freischaltung", "pruefen", "aktiv")
HASHES = ("x002_schluessel", "e002_schluessel", "x002_zertifikat", "e002_zertifikat")
AUTHOR = "aeradex EBICS <ebics@aeradex.local>"


def ordner(book) -> Path:
    return book.root / ".aeradex" / "lokal" / "ebics"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------- settings and state ----------

def lesen(book) -> dict:
    path = ordner(book) / "teilnehmer.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}


def schreiben(book, data: dict) -> None:
    folder = ordner(book)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "teilnehmer.yaml.tmp"
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(folder / "teilnehmer.yaml")


def protokoll(book, art: str, ok: bool, text: str = "", **extra) -> None:
    folder = ordner(book)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "protokoll.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"zeit": _now(), "art": art, "ok": ok, "text": text, **extra}, ensure_ascii=False) + "\n")


def verlauf(book, n: int = 30) -> list[dict]:
    path = ordner(book) / "protokoll.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    return [json.loads(line) for line in reversed(lines) if line.strip()]


def teilnehmer(book, cfg: dict | None = None) -> p.Teilnehmer:
    cfg = cfg or lesen(book)
    missing = [k for k in ("url", "host_id", "partner_id", "user_id") if not cfg.get(k)]
    if missing:
        raise BookError("EBICS ist noch nicht eingerichtet (fehlt: " + ", ".join(missing) + ")")
    return p.Teilnehmer(cfg["url"], cfg["host_id"], cfg["partner_id"], cfg["user_id"])


def einrichten(book, url: str, host_id: str, partner_id: str, user_id: str, bank: str = "",
               erwartet: dict | None = None) -> dict:
    """Connection data from the bank's letter. Changing it after the keys were sent to the bank is refused:
    the bank knows this participant with these keys."""
    cfg = lesen(book)
    new = {"url": url.strip(), "host_id": host_id.strip().upper(), "partner_id": partner_id.strip().upper(),
           "user_id": user_id.strip().upper()}
    if not new["url"].startswith("https://"):
        raise BookError("Die EBICS-Adresse muss mit https:// beginnen")
    for key, value in new.items():
        if not value:
            raise BookError(f"{key} fehlt")
    if cfg.get("ini_gesendet") and any(cfg.get(k) != v for k, v in new.items()):
        raise BookError("Die Schlüssel sind schon bei der Bank. Teilnehmer und Bank lassen sich nicht mehr ändern "
                        "— zuerst «Neu beginnen» (die Bank muss den Teilnehmer dann zurücksetzen).")
    cfg.update(new)
    cfg["bank"] = bank.strip() or cfg.get("bank", "")
    hashes = {k: p.normalise_hash(v) for k, v in (erwartet or {}).items() if k in HASHES and p.normalise_hash(v)}
    for k, v in hashes.items():
        if len(v) != 64:
            raise BookError(f"Hashwert {k}: 32 Bytes (64 Hex-Zeichen) erwartet, {len(v)} gelesen — bitte prüfen")
    if hashes:
        cfg["erwartet"] = {**(cfg.get("erwartet") or {}), **hashes}
    cfg.setdefault("eingerichtet", _now())
    schreiben(book, cfg)
    if not schluessel_datei(book).exists():
        k = p.neue_schluessel(cfg["partner_id"], cfg["user_id"], book.settings.firma)
        speichern_schluessel(book, k)
        protokoll(book, "schluessel", True, "Schlüssel A006, X002, E002 erzeugt (RSA 2048, Zertifikate selbst signiert)")
    return status(book)


def neu_beginnen(book) -> dict:
    """Throw away keys and state (kept as a dated copy) — only when the bank resets the participant."""
    folder = ordner(book)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in ("teilnehmer.yaml", "schluessel.yaml"):
        if (folder / name).exists():
            (folder / name).replace(folder / f"{name}.{stamp}.alt")
    protokoll(book, "neu", True, "Teilnehmer und Schlüssel zurückgesetzt")
    return status(book)


# ---------- keys ----------

def schluessel_datei(book) -> Path:
    return ordner(book) / "schluessel.yaml"


def _passwort() -> bytes | None:
    pw = os.environ.get("AERADEX_EBICS_PASSWORT", "")
    return pw.encode() if pw else None


def _pem_key(key) -> str:
    pw = _passwort()
    enc = serialization.BestAvailableEncryption(pw) if pw else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc).decode()


def _pem_cert(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def speichern_schluessel(book, k: p.Schluessel) -> None:
    data = {"a006": _pem_key(k.a006), "x002": _pem_key(k.x002), "e002": _pem_key(k.e002),
            "cert_a": _pem_cert(k.cert_a), "cert_x": _pem_cert(k.cert_x), "cert_e": _pem_cert(k.cert_e)}
    if k.bank_x is not None:
        data["bank_x"], data["bank_e"] = _pem_cert(k.bank_x), _pem_cert(k.bank_e)
    path = schluessel_datei(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def laden_schluessel(book) -> p.Schluessel:
    path = schluessel_datei(book)
    if not path.exists():
        raise BookError("Es gibt noch keine EBICS-Schlüssel (zuerst einrichten)")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        keys = [serialization.load_pem_private_key(data[n].encode(), _passwort()) for n in ("a006", "x002", "e002")]
    except (TypeError, ValueError):
        raise BookError("Die EBICS-Schlüssel lassen sich nicht öffnen (AERADEX_EBICS_PASSWORT falsch oder fehlt?)") from None
    certs = [x509.load_pem_x509_certificate(data[n].encode()) for n in ("cert_a", "cert_x", "cert_e")]
    bank = [x509.load_pem_x509_certificate(data[n].encode()) if data.get(n) else None for n in ("bank_x", "bank_e")]
    return p.Schluessel(*keys, *certs, *bank)


def eigene_hashes(k: p.Schluessel) -> list[dict]:
    """For the letter and the page: per key its certificate hash (EBICS 3.0) and key hash (EBICS 2.5)."""
    out = []
    for name, art, key, cert in (("A006", "Elektronische Unterschrift", k.a006, k.cert_a),
                                 ("X002", "Authentifikation", k.x002, k.cert_x),
                                 ("E002", "Verschlüsselung", k.e002, k.cert_e)):
        out.append({"version": name, "art": art, "zertifikat": p.hex_paare(p.cert_hash(cert)),
                    "schluessel": p.hex_paare(p.key_hash(key.public_key())),
                    "seriennummer": f"{cert.serial_number:X}", "bits": key.key_size,
                    "aussteller": cert.issuer.rfc4514_string()})
    return out


def bank_hashes(k: p.Schluessel) -> dict:
    if k.bank_x is None:
        return {}
    return {"x002_schluessel": p.key_hash(k.bank_x.public_key()).hex(), "e002_schluessel": p.key_hash(k.bank_e.public_key()).hex(),
            "x002_zertifikat": p.cert_hash(k.bank_x).hex(), "e002_zertifikat": p.cert_hash(k.bank_e).hex()}


def vergleich(cfg: dict, k: p.Schluessel) -> list[dict]:
    """The bank's hashes as received by HPB next to those on its letter."""
    got = bank_hashes(k)
    expected = cfg.get("erwartet") or {}
    labels = {"x002_schluessel": "Bankschlüssel Authentifikation (X002)", "e002_schluessel": "Bankschlüssel Verschlüsselung (E002)",
              "x002_zertifikat": "Bankzertifikat Authentifikation (X002)", "e002_zertifikat": "Bankzertifikat Verschlüsselung (E002)"}
    return [{"key": key, "label": labels[key], "erhalten": p.hex_paare(bytes.fromhex(got[key])) if key in got else "",
             "erwartet": p.hex_paare(bytes.fromhex(expected[key])) if key in expected else "",
             "gleich": (got.get(key) == expected[key]) if key in expected and key in got else None}
            for key in HASHES]


# ---------- the steps ----------

def client(book, cfg: dict | None = None, post=None) -> p.Client:
    cfg = cfg or lesen(book)
    return p.Client(teilnehmer(book, cfg), laden_schluessel(book) if schluessel_datei(book).exists() else None,
                    post=post or _POST)


_POST = None          # tests put their bank here


def senden(book, post=None) -> dict:
    """INI (signature key) and HIA (authentication and encryption keys) to the bank. Each only once:
    the bank refuses a second one until it resets the participant."""
    cfg = lesen(book)
    c = client(book, cfg, post)
    if c.k is None:
        raise BookError("Es gibt noch keine EBICS-Schlüssel (zuerst einrichten)")
    for art, fn in (("INI", c.ini), ("HIA", c.hia)):
        key = f"{art.lower()}_gesendet"
        if cfg.get(key):
            continue
        try:
            fn()
        except p.EbicsFehler as exc:
            if exc.code in ("091118", "091120"):      # the bank already has it: count it as sent
                protokoll(book, art, True, f"schon bei der Bank ({exc.code})")
            else:
                protokoll(book, art, False, str(exc), code=exc.code)
                schreiben(book, cfg)
                raise
        else:
            protokoll(book, art, True, "gesendet")
        cfg[key] = _now()
        schreiben(book, cfg)
    return status(book)


def bankschluessel(book, post=None) -> dict:
    """HPB: fetch the bank's certificates. Accepted at once when they match the hashes on the bank's letter;
    otherwise a person compares them (with the letter, or by phone with the bank) and confirms."""
    cfg = lesen(book)
    c = client(book, cfg, post)
    try:
        certs = c.hpb()
    except p.EbicsFehler as exc:
        protokoll(book, "HPB", False, str(exc), code=exc.code)
        raise
    k = c.k
    k.bank_x, k.bank_e = certs["X002"], certs["E002"]
    rows = vergleich(cfg, k)
    if any(r["gleich"] is False for r in rows):
        protokoll(book, "HPB", False, "Bankschlüssel weichen vom Brief der Bank ab — NICHT übernommen")
        raise BookError("Die Schlüssel, die der Server geliefert hat, stimmen NICHT mit dem Brief der Bank überein: "
                        + "; ".join(f"{r['label']}: erwartet {r['erwartet']}, erhalten {r['erhalten']}"
                                    for r in rows if r["gleich"] is False)
                        + ". Nichts übernommen — zuerst die Hashwerte im Brief prüfen und mit der Bank klären.")
    speichern_schluessel(book, k)
    cfg["bankschluessel"] = _now()
    complete = all(r["gleich"] for r in rows)
    if complete:
        cfg["bestaetigt"] = {"zeit": _now(), "wie": "alle vier Hashwerte stimmen mit dem Brief der Bank überein"}
    else:
        cfg.pop("bestaetigt", None)
    schreiben(book, cfg)
    protokoll(book, "HPB", True, "Bankschlüssel geholt" + (" und mit dem Brief bestätigt" if complete else
                                                          " — warten auf Bestätigung"))
    return status(book)


def bestaetigen(book, wer: str = "") -> dict:
    cfg = lesen(book)
    k = laden_schluessel(book)
    if k.bank_x is None:
        raise BookError("Es sind noch keine Bankschlüssel da (zuerst holen)")
    if any(r["gleich"] is False for r in vergleich(cfg, k)):
        raise BookError("Die Hashwerte stimmen nicht mit dem Brief überein — so nicht bestätigen")
    cfg["bestaetigt"] = {"zeit": _now(), "wie": f"von Hand bestätigt{(' von ' + wer) if wer else ''}"}
    schreiben(book, cfg)
    protokoll(book, "bestaetigt", True, cfg["bestaetigt"]["wie"])
    return status(book)


def status(book) -> dict:
    cfg = lesen(book)
    has_keys = schluessel_datei(book).exists()
    k = laden_schluessel(book) if has_keys else None
    if not cfg.get("url") or not has_keys:
        schritt = "einrichten"
    elif not (cfg.get("ini_gesendet") and cfg.get("hia_gesendet")):
        schritt = "senden"
    elif k.bank_x is None:
        schritt = "freischaltung"
    elif not cfg.get("bestaetigt"):
        schritt = "pruefen"
    else:
        schritt = "aktiv"
    return {"schritt": schritt, "cfg": cfg, "eigene": eigene_hashes(k) if k else [],
            "vergleich": vergleich(cfg, k) if k and k.bank_x is not None else [],
            "offen": [f.name for f in sorted((ordner(book) / "eingang").glob("*")) if f.is_file()]
            if (ordner(book) / "eingang").exists() else []}


# ---------- fetching ----------

@contextmanager
def _sperre(book):
    folder = ordner(book)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".abholen.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BookError("Es läuft schon eine Abholung") from None
        yield


def dienste(cfg: dict) -> list[p.Dienst]:
    """What to fetch; default camt.053 (EOP, CH, ZIP). `dienste:` in teilnehmer.yaml may list others."""
    items = cfg.get("dienste") or [{}]
    return [p.Dienst(**{k: str(v) for k, v in item.items() if k in p.Dienst.__dataclass_fields__}) for item in items]


def _xml_dateien(data: bytes) -> list[tuple[str, bytes]]:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [(Path(n).name, z.read(n)) for n in sorted(z.namelist())
                    if not n.endswith("/") and n.lower().endswith(".xml")]
    if data.lstrip()[:1] == b"<":
        return [("auszug.xml", data)]
    raise BookError("Die Bank hat eine unbekannte Datei geliefert (weder ZIP noch XML)")


def abholen(book, von: dt.date | None = None, bis: dt.date | None = None, post=None) -> dict:
    """Fetch what the bank has (without dates: everything not fetched yet), confirm it to the bank once it is
    stored, then import every statement into the book."""
    cfg = lesen(book)
    if status(book)["schritt"] != "aktiv":
        raise BookError("EBICS ist noch nicht aktiv (Einrichtung auf der Seite Bank › EBICS abschliessen)")
    eingang = ordner(book) / "eingang"
    eingang.mkdir(parents=True, exist_ok=True)
    result = {"dateien": 0}
    with _sperre(book):
        c = client(book, cfg, post)
        for dienst in dienste(cfg):
            for _ in range(20):                     # the bank may hand out several orders one after the other
                try:
                    got = c.download(dienst, von, bis)
                except p.EbicsFehler as exc:
                    protokoll(book, "abholen", False, str(exc), code=exc.code, dienst=dienst.msg)
                    cfg["letzter_fehler"] = {"zeit": _now(), "text": str(exc)}
                    schreiben(book, cfg)
                    raise
                if got is None:
                    break
                tid, data = got
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                for n, (name, xml) in enumerate(_xml_dateien(data), 1):
                    (eingang / f"{stamp}-{tid[:8]}-{n:02d}-{name}").write_bytes(xml)
                    result["dateien"] += 1
                c.quittung(tid)                     # stored safely: now the bank may mark it as fetched
                protokoll(book, "abholen", True, f"{dienst.msg}: {len(_xml_dateien(data))} Datei(en)", transaktion=tid)
                if von and bis:
                    break                           # a date range is answered in one go
        from . import zahlungen
        result.update(zahlungen.status_abholen(book, c))
        cfg["letzter_abruf"] = _now()
        cfg.pop("letzter_fehler", None)
        schreiben(book, cfg)
        result.update(importieren(book))
    return result


def importieren(book) -> dict:
    """Import what waits in eingang/ (also left-overs of an earlier run whose import failed)."""
    eingang = ordner(book) / "eingang"
    out = {"importe": [], "fehler": []}
    start = buchbeginn(book)
    for path in sorted(eingang.glob("*.xml")) if eingang.exists() else []:
        ende = _auszug_ende(path)
        if start and ende and ende < start:
            protokoll(book, "import", True, f"endet {ende} vor dem ersten Buchjahr ({start}) — nur archiviert",
                      datei=path.name)
            _archivieren(book, path)
            continue
        try:
            # The import works on a staged copy of the book (without .aeradex/lokal): hand it a copy outside.
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copy(path, Path(tmp) / path.name)
                res = api.bank_import(book, str(Path(tmp) / path.name))
        except BookError as exc:
            if "keinen Kontoauszug" not in str(exc):
                out["fehler"].append(f"{path.name}: {exc}")      # stays in eingang/ for the next run
                protokoll(book, "import", False, str(exc), datei=path.name)
                continue
            protokoll(book, "import", True, "leerer Auszug, nichts zu importieren", datei=path.name)
        else:
            out["importe"].append({"datei": path.name, "meldung": res.get("meldung", "")})
            protokoll(book, "import", True, res.get("meldung", ""), datei=path.name)
        _archivieren(book, path)
    return out


def _archivieren(book, path: Path) -> None:
    target = ordner(book) / "archiv" / path.name[:4] / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    path.replace(target)


def buchbeginn(book) -> str:
    """The first day of the first business year: statements that end before it belong to the old bookkeeping."""
    years = book.years()
    return f"{min(years)}-01-01" if years else ""


def _auszug_ende(path: Path) -> str:
    """The last day a camt.053 file covers (latest statement end or movement), '' when unknown."""
    from aeradex import bank
    try:
        statements = bank.parse(path.read_bytes())
    except Exception:
        return ""
    days = []
    for s in statements:
        days.append(s.get("bis") or s.get("schluss_datum") or "")
        days += [e["datum"].isoformat() for e in s.get("buchungen", [])]
    days = [d for d in days if d]
    return max(days) if days else ""

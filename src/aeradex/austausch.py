"""The `.aeradex` file: a whole book in one file, to hand to another aeradex user.

A book lives in a local folder (text files + git). To pass it on — to the
Treuhänder, a successor, a second computer — `export_book` packs it into one
ZIP container:

    mimetype          "application/vnd.aeradex+zip", stored first and uncompressed
    manifest.json     format version, aeradex version, company, years, commit,
                      plugins, SHA-256 of every file
    buch/…            the book's files as they are on disk (no .git, no berichte/)
    historie.bundle   `git bundle --all`: the complete change history

With a password the manifest and payload are encrypted together (AES-256-GCM,
key from scrypt) into `payload.enc`; only `mimetype` and a small header that
names the cipher stay readable. `import_book` checks every hash, refuses paths
that leave the target folder, restores the history from the bundle, and runs
`aeradex check` on the result.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import __version__, gitlog
from .book import Book, BookError

MIMETYPE = "application/vnd.aeradex+zip"
FORMAT = "aeradex-buch"
FORMAT_VERSION = 1
SUFFIX = ".aeradex"
MAX_TOTAL = 4 * 1024 ** 3          # uncompressed size an import accepts
SKIP_DIRS = {".git", "berichte", "__pycache__", "node_modules"}
SKIP_FILES = {".aeradex/write.lock", ".DS_Store"}
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


# ---------- encryption ----------

def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise BookError("Für verschlüsselte .aeradex-Dateien fehlt ein Paket: pip install 'aeradex[crypt]'") from exc
    return AESGCM, Scrypt


def _key(password: str, salt: bytes, params: dict) -> bytes:
    _, Scrypt = _crypto()
    return Scrypt(salt=salt, length=32, n=params["n"], r=params["r"], p=params["p"]).derive(password.encode("utf-8"))


def _encrypt(data: bytes, password: str) -> tuple[bytes, dict]:
    AESGCM, _ = _crypto()
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {"verfahren": "AES-256-GCM", "kdf": "scrypt", **SCRYPT, "salt": salt.hex(), "nonce": nonce.hex()}
    aad = json.dumps({"format": FORMAT, "version": FORMAT_VERSION}, sort_keys=True).encode()
    return AESGCM(_key(password, salt, SCRYPT)).encrypt(nonce, data, aad), header


def _decrypt(data: bytes, password: str, header: dict) -> bytes:
    AESGCM, _ = _crypto()
    from cryptography.exceptions import InvalidTag
    if header.get("verfahren") != "AES-256-GCM" or header.get("kdf") != "scrypt":
        raise BookError(f"Unbekanntes Verschlüsselungsverfahren {header.get('verfahren')}")
    params = {k: int(header[k]) for k in ("n", "r", "p")}
    if params["n"] > 2 ** 20 or params["r"] > 32 or params["p"] > 16:
        raise BookError("Ungültige Schlüsselparameter in der Datei")
    aad = json.dumps({"format": FORMAT, "version": FORMAT_VERSION}, sort_keys=True).encode()
    try:
        return AESGCM(_key(password, bytes.fromhex(header["salt"]), params)).decrypt(
            bytes.fromhex(header["nonce"]), data, aad)
    except InvalidTag:
        raise BookError("Falsches Passwort oder beschädigte Datei") from None


# ---------- export ----------

def book_files(book: Book, with_inbox: bool = True) -> list[Path]:
    out = []
    for path in sorted(book.root.rglob("*")):
        rel = path.relative_to(book.root)
        from .storage import internal
        if internal(rel.as_posix()):
            continue
        if rel.parts[0] in SKIP_DIRS or rel.as_posix() in SKIP_FILES or path.name in SKIP_FILES:
            continue
        if not with_inbox and rel.parts[0] == "inbox" and path.name != ".gitkeep":
            continue
        if path.is_symlink() or not path.is_file():
            continue
        out.append(path)
    return out


def _bundle(book: Book) -> bytes | None:
    if not gitlog.is_repo(book.root):
        return None
    if gitlog._git(book.root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        return None          # no commit yet
    with tempfile.TemporaryDirectory(prefix="aeradex-export-") as tmp:
        target = Path(tmp) / "historie.bundle"
        result = gitlog._git(book.root, "bundle", "create", "-q", str(target), "--all", check=False)
        if result.returncode != 0:
            raise BookError("git bundle fehlgeschlagen:\n" + (result.stdout + result.stderr).strip())
        return target.read_bytes()


def _zip(entries: list[tuple[str, bytes]], first_stored: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_STORED if name == first_stored else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


def export_book(book: Book, target: Path, password: str | None = None, with_inbox: bool = True,
                with_history: bool = True) -> dict:
    from . import check as checks
    errors = [i for i in checks.run(book) if i.level == "fehler"]
    if errors:
        raise BookError("Das Buch hat Fehler — vor der Weitergabe beheben (aeradex check):\n"
                        + "\n".join(f"  {e}" for e in errors[:10]))
    target = Path(target)
    if target.suffix != SUFFIX:
        target = target.with_name(target.name + SUFFIX)
    if target.resolve().is_relative_to(book.root) and target.resolve().parts[len(book.root.parts)] != "berichte":
        raise BookError("Die .aeradex-Datei nicht ins Buch selbst legen (ausser in berichte/)")
    payload = [(f"buch/{p.relative_to(book.root).as_posix()}", p.read_bytes()) for p in book_files(book, with_inbox)]
    bundle = _bundle(book) if with_history else None
    head = None
    dirty = False
    if gitlog.is_repo(book.root):
        head = gitlog._git(book.root, "rev-parse", "HEAD", check=False).stdout.strip() or None
        dirty = bool(gitlog._git(book.root, "status", "--porcelain", check=False).stdout.strip())
    s = book.settings
    manifest = {
        "format": FORMAT, "version": FORMAT_VERSION, "aeradex": __version__,
        "firma": s.firma, "uid": s.get("uid") or "", "rechtsform": s.get("rechtsform") or "",
        "jahre": book.years(), "gesperrt_bis": s.sperre_bis.isoformat() if s.sperre_bis else None,
        "plugins": list(s.get("plugins") or []),
        "erstellt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commit": head, "uncommittete_aenderungen": dirty, "mit_inbox": with_inbox,
        "historie": hashlib.sha256(bundle).hexdigest() if bundle else None,
        "dateien": {name[len("buch/"):]: hashlib.sha256(data).hexdigest() for name, data in payload},
    }
    inner = [("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))] + payload
    if bundle:
        inner.append(("historie.bundle", bundle))
    if password:
        blob, header = _encrypt(_zip(inner), password)
        entries = [("mimetype", MIMETYPE.encode()),
                   ("manifest.json", json.dumps({"format": FORMAT, "version": FORMAT_VERSION, "verschluesselt": True,
                                                 "verschluesselung": header}, indent=2).encode()),
                   ("payload.enc", blob)]
    else:
        entries = [("mimetype", MIMETYPE.encode())] + inner
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_zip(entries, first_stored="mimetype"))
    return {"ok": True, "datei": str(target), "groesse": target.stat().st_size, "dateien": len(payload),
            "historie": bool(bundle), "verschluesselt": bool(password),
            "uncommittete_aenderungen": dirty,
            "meldung": f"Buch {s.firma} exportiert: {target.name} ({len(payload)} Dateien"
                       f"{', mit Änderungsverlauf' if bundle else ''}{', verschlüsselt' if password else ''})"}


# ---------- import ----------

def _safe_rel(name: str) -> str:
    """A path inside the book, or BookError — no absolute paths, no '..', no drive letters."""
    if "\\" in name or "\x00" in name:
        raise BookError(f"Ungültiger Pfad in der Datei: {name!r}")
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(part in ("..", ".", "") for part in p.parts) or ":" in p.parts[0]:
        raise BookError(f"Ungültiger Pfad in der Datei: {name!r}")
    if p.parts[0] == ".git":
        raise BookError(f"Pfad in .git/ nicht erlaubt: {name!r}")
    return p.as_posix()


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise BookError("Keine gültige .aeradex-Datei (kein ZIP-Container)") from None
    total = 0
    for info in z.infolist():
        total += info.file_size
        if stat.S_ISLNK(info.external_attr >> 16):
            raise BookError(f"Symbolische Links sind nicht erlaubt: {info.filename}")
    if total > MAX_TOTAL:
        raise BookError(f"Datei zu gross ({total // 1024 ** 2} MB entpackt)")
    return z


def read_container(source: Path, password: str | None = None) -> tuple[dict, dict[str, bytes], bytes | None]:
    """Manifest, book files (relative path → bytes) and the git bundle, all verified."""
    raw = Path(source).read_bytes()
    z = _open(raw)
    names = z.namelist()
    if not names or names[0] != "mimetype" or z.read("mimetype").decode(errors="replace").strip() != MIMETYPE:
        raise BookError("Keine .aeradex-Datei (mimetype fehlt oder stimmt nicht)")
    try:
        manifest = json.loads(z.read("manifest.json"))
    except (KeyError, ValueError):
        raise BookError("manifest.json fehlt oder ist ungültig") from None
    if manifest.get("format") != FORMAT:
        raise BookError(f"Unbekanntes Format {manifest.get('format')!r}")
    if int(manifest.get("version") or 0) > FORMAT_VERSION:
        raise BookError(f"Die Datei hat Formatversion {manifest.get('version')}; diese aeradex-Version kennt "
                        f"bis {FORMAT_VERSION}. aeradex aktualisieren (pip install -U aeradex).")
    if manifest.get("verschluesselt"):
        if not password:
            raise BookError("Die Datei ist verschlüsselt — Passwort angeben")
        inner = _decrypt(z.read("payload.enc"), password, manifest.get("verschluesselung") or {})
        z = _open(inner)
        try:
            manifest = json.loads(z.read("manifest.json"))
        except (KeyError, ValueError):
            raise BookError("manifest.json im verschlüsselten Teil fehlt oder ist ungültig") from None
    files: dict[str, bytes] = {}
    bundle = None
    for info in z.infolist():
        if info.is_dir() or info.filename in ("mimetype", "manifest.json"):
            continue
        if info.filename == "historie.bundle":
            bundle = z.read(info)
            continue
        if not info.filename.startswith("buch/"):
            raise BookError(f"Unerwarteter Eintrag in der Datei: {info.filename!r}")
        files[_safe_rel(info.filename[len("buch/"):])] = z.read(info)
    expected = manifest.get("dateien") or {}
    missing = sorted(set(expected) - set(files))
    extra = sorted(set(files) - set(expected))
    if missing or extra:
        raise BookError("Inhalt passt nicht zum Manifest"
                        + (f" — fehlt: {', '.join(missing[:5])}" if missing else "")
                        + (f" — zusätzlich: {', '.join(extra[:5])}" if extra else ""))
    bad = [name for name, data in files.items() if hashlib.sha256(data).hexdigest() != expected[name]]
    if bad:
        raise BookError(f"Prüfsumme stimmt nicht (Datei verändert oder beschädigt): {', '.join(bad[:5])}")
    if "aeradex.yaml" not in files:
        raise BookError("Die Datei enthält kein Buch (aeradex.yaml fehlt)")
    if manifest.get("historie"):
        if bundle is None or hashlib.sha256(bundle).hexdigest() != manifest["historie"]:
            raise BookError("Änderungsverlauf (historie.bundle) fehlt oder ist beschädigt")
    return manifest, files, bundle


def import_book(source: Path, target: Path, password: str | None = None) -> dict:
    from . import check as checks
    from . import plugins
    target = Path(target).resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BookError(f"Zielordner {target} existiert und ist nicht leer — einen neuen Ordner angeben")
    manifest, files, bundle = read_container(Path(source), password)
    created = not target.exists()
    try:
        if bundle:
            with tempfile.TemporaryDirectory(prefix="aeradex-import-") as tmp:
                bpath = Path(tmp) / "historie.bundle"
                bpath.write_bytes(bundle)
                result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "-q", str(bpath),
                                         str(target)], capture_output=True, text=True)
                if result.returncode != 0:
                    raise BookError("Änderungsverlauf konnte nicht übernommen werden:\n" + result.stderr.strip())
            gitlog._git(target, "remote", "remove", "origin", check=False)
            # Files the sender had deleted without committing; never inbox/ when it was left out.
            tracked = gitlog._git(target, "ls-files", "-z").stdout.split("\0")
            for rel in filter(None, tracked):
                if rel not in files and not (not manifest.get("mit_inbox", True) and rel.startswith("inbox/")):
                    (target / rel).unlink(missing_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            path = target / rel
            if not path.resolve().is_relative_to(target):
                raise BookError(f"Ungültiger Pfad in der Datei: {rel!r}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        book = Book(target)
        issues = checks.run(book)
        errors = [i for i in issues if i.level == "fehler"]
        commit = None
        if not errors:
            gitlog.init_repo(target)
            name = Path(source).name
            commit = gitlog.commit(target, f"aeradex: Buch importiert aus {name}" if not bundle else
                                   f"aeradex: Import aus {name} — nicht committete Änderungen des Absenders")
    except BaseException:
        if created and target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise
    missing_plugins = [p for p in manifest.get("plugins") or [] if p not in plugins.installed()]
    return {"ok": not errors, "ordner": str(target), "firma": manifest.get("firma"), "jahre": manifest.get("jahre"),
            "historie": bool(bundle), "commit": commit, "dateien": len(files),
            "fehlende_plugins": missing_plugins,
            "probleme": [i.as_dict() for i in issues if i.level in ("fehler", "warnung")],
            "meldung": (f"Buch {manifest.get('firma')} importiert nach {target}"
                        + (" mit Änderungsverlauf" if bundle else "")
                        + (f" — fehlende Plugins: {', '.join(missing_plugins)}" if missing_plugins else "")
                        + (f" — {len(errors)} Fehler, bitte aeradex check" if errors else ""))}


def inspect(source: Path, password: str | None = None) -> dict:
    """What a .aeradex file contains, without importing it."""
    manifest, files, bundle = read_container(Path(source), password)
    return {k: v for k, v in manifest.items() if k != "dateien"} | {"dateien": len(files), "historie": bool(bundle)}

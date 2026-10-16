"""Who may reach the book's tools from their own PC (Claude Code/Codex over ssh), managed in aeradex.

Every key aeradex writes is locked to the forced command (``restrict,command="aeradex-ssh <person>"``): it can
start the aeradex tools (MCP) and drop a file into inbox/, nothing else on the server. Lines aeradex did not write
(e.g. added by hand) are kept as they are. Only on the server: the service sets AERADEX_ZUGRIFF_KEYS.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

from aeradex.book import BookError

KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
             "sk-ssh-ed25519@openssh.com")
WHO = re.compile(r"^[a-z][a-z0-9]{1,19}$")


def keys_file() -> Path | None:
    raw = os.environ.get("AERADEX_ZUGRIFF_KEYS", "").strip()
    return Path(raw) if raw else None


def forced_command() -> str:
    return os.environ.get("AERADEX_ZUGRIFF_BEFEHL", "/usr/local/sbin/aeradex-ssh")


def server() -> dict:
    """How a PC reaches this book: host name, ssh user and the alias the setup writes into ~/.ssh/config."""
    import getpass
    return {"server": os.environ.get("AERADEX_ZUGRIFF_SERVER", "").strip() or "<server>",
            "benutzer": os.environ.get("AERADEX_ZUGRIFF_BENUTZER", "").strip() or getpass.getuser(),
            "alias": os.environ.get("AERADEX_ZUGRIFF_ALIAS", "").strip() or "aeradex-buch"}


def names_file() -> Path:
    return Path(os.environ.get("AERADEX_CONFIG") or Path.home() / ".config" / "aeradex") / "zugriff.yaml"


def names() -> dict[str, str]:
    path = names_file()
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            who, sep, name = line.partition(":")
            if sep and WHO.match(who.strip()):
                out[who.strip()] = name.strip().strip('"')
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def fingerprint(blob: str) -> str:
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _ours(line: str) -> tuple[str, str, str, str] | None:
    """(who, type, blob, comment) for a line aeradex wrote, else None."""
    m = re.match(r'^restrict,command="' + re.escape(forced_command()) + r' ([a-z][a-z0-9]{1,19})"\s+(\S+)\s+(\S+)\s*(.*)$',
                 line.strip())
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def entries() -> list[dict]:
    path = keys_file()
    if path is None or not path.exists():
        return []
    known = names()
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _ours(line)
        if parsed:
            who, kind, blob, comment = parsed
            out.append({"wer": who, "name": known.get(who, who), "typ": kind, "fingerprint": fingerprint(blob),
                        "kommentar": comment})
    return out


def parse_public_key(text: str) -> tuple[str, str, str]:
    """A pasted public key: exactly «type base64 [comment]», nothing before it (no options)."""
    parts = " ".join(str(text or "").split()).split(" ")
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise BookError("Öffentlichen Schlüssel einfügen, z.B. «ssh-ed25519 AAAA… name» (die Datei …/aeradex-buch.pub)")
    kind, blob = parts[0], parts[1]
    try:
        raw = base64.b64decode(blob, validate=True)
    except ValueError:
        raise BookError("Der Schlüssel ist nicht vollständig (Base64)") from None
    if not raw.startswith(len(kind).to_bytes(4, "big") + kind.encode()):
        raise BookError("Typ und Schlüssel passen nicht zusammen")
    comment = re.sub(r"[^A-Za-z0-9@._-]", "_", " ".join(parts[2:]))[:60]
    return kind, blob, comment


def add(who: str, name: str, public_key: str) -> dict:
    path = keys_file()
    if path is None:
        raise BookError("Zugriff wird nur auf dem Server verwaltet")
    who = str(who or "").strip().lower()
    if not WHO.match(who):
        raise BookError("Kürzel: 2–20 Kleinbuchstaben/Ziffern, beginnt mit einem Buchstaben (z.B. anna, ben)")
    name = " ".join(str(name or "").split())[:60]
    if not name or any(c in name for c in '":\n<>'):
        raise BookError("Name angeben (erscheint im Verlauf, z.B. «Anna Muster»)")
    kind, blob, comment = parse_public_key(public_key)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if any(blob in ln.split() for ln in lines):
        raise BookError("Dieser Schlüssel ist schon eingetragen")
    lines.append(f'restrict,command="{forced_command()} {who}" {kind} {blob} {comment or who}'.rstrip())
    _write(path, "\n".join(lines) + "\n")
    known = names()
    known[who] = name
    _write(names_file(), "".join(f"{k}: {v}\n" for k, v in sorted(known.items())))
    return {"wer": who, "fingerprint": fingerprint(blob)}


def remove(fp: str) -> dict:
    path = keys_file()
    if path is None or not path.exists():
        raise BookError("Zugriff wird nur auf dem Server verwaltet")
    lines = path.read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines if not (_ours(ln) and fingerprint(_ours(ln)[2]) == fp)]
    if len(keep) == len(lines):
        raise BookError("Schlüssel nicht gefunden")
    _write(path, "\n".join(keep) + ("\n" if keep else ""))
    return {"entfernt": fp}

"""Login for running batzen on a server: users, password hashes, signed sessions.

Users live outside the book (never in its git history):

    <config>/users.yaml    name, display name, role, scrypt hash
    <config>/secret        random key that signs session cookies (created on first start)

Default <config> is ~/.config/batzen, or $BATZEN_CONFIG, or `batzen serve --config DIR`.

Roles:
    lesen        sees everything, changes nothing
    buchhaltung  books, invoices, payroll, bank, MWST — everything except the two below
    admin        additionally settings and locking/unlocking periods, and user management (CLI)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

ROLES = ("lesen", "buchhaltung", "admin")
SESSION_COOKIE = "batzen_login"
SESSION_DAYS = 14
# Paths only an admin may POST to.
ADMIN_ONLY = ("/einstellungen", "/abschluss/sperre", "/abschluss/entsperren")


def token_matches(sent: str, expected: str) -> bool:
    """Compare ASCII tokens, rejecting malformed user input without raising."""
    return sent.isascii() and hmac.compare_digest(sent, expected)


def config_dir(explicit: str | Path | None = None) -> Path:
    path = Path(explicit or os.environ.get("BATZEN_CONFIG") or Path.home() / ".config" / "batzen")
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- password hashing (scrypt, stdlib) ----------

def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Passwort: mindestens 10 Zeichen")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                   maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(candidate, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# ---------- users ----------

@dataclass
class User:
    name: str
    anzeige: str
    rolle: str

    @property
    def can_write(self) -> bool:
        return self.rolle in ("buchhaltung", "admin")

    @property
    def is_admin(self) -> bool:
        return self.rolle == "admin"

    @property
    def git_author(self) -> str:
        return f"{self.anzeige} <{self.name}@batzen>"


class UserStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / "users.yaml"

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return (yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}).get("benutzer", {}) or {}

    def _save(self, users: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump({"benutzer": users}, allow_unicode=True, sort_keys=True), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def list(self) -> list[User]:
        return [User(n, u.get("anzeige") or n, u.get("rolle", "lesen")) for n, u in sorted(self._load().items())]

    def get(self, name: str) -> User | None:
        u = self._load().get(name)
        return User(name, u.get("anzeige") or name, u.get("rolle", "lesen")) if u else None

    def add(self, name: str, password: str, rolle: str, anzeige: str = "") -> User:
        name = name.strip().lower()
        if not name or not name.replace(".", "").replace("-", "").replace("_", "").isalnum():
            raise ValueError("Benutzername: Buchstaben, Ziffern, . - _")
        if rolle not in ROLES:
            raise ValueError(f"Rolle: {', '.join(ROLES)}")
        users = self._load()
        if name in users:
            raise ValueError(f"Benutzer {name} existiert bereits")
        users[name] = {"anzeige": anzeige or name, "rolle": rolle, "hash": hash_password(password)}
        self._save(users)
        return self.get(name)

    def set_password(self, name: str, password: str) -> None:
        users = self._load()
        if name not in users:
            raise ValueError(f"Benutzer {name} unbekannt")
        users[name]["hash"] = hash_password(password)
        users[name]["version"] = int(users[name].get("version", 0)) + 1   # ends existing sessions
        self._save(users)

    def remove(self, name: str) -> None:
        users = self._load()
        if name not in users:
            raise ValueError(f"Benutzer {name} unbekannt")
        if users[name].get("rolle") == "admin" and sum(1 for u in users.values() if u.get("rolle") == "admin") == 1:
            raise ValueError("Der letzte Admin kann nicht entfernt werden")
        del users[name]
        self._save(users)

    def check(self, name: str, password: str) -> User | None:
        u = self._load().get((name or "").strip().lower())
        if not u:
            verify_password(password, "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAA")   # same timing either way
            return None
        return self.get(name.strip().lower()) if verify_password(password, u.get("hash", "")) else None

    def version(self, name: str) -> int:
        return int(self._load().get(name, {}).get("version", 0))

    def has_admin(self) -> bool:
        return any(u.rolle == "admin" for u in self.list())


# ---------- sessions ----------

class Sessions:
    """Stateless signed cookies: {user, expiry, session id, password version}."""

    def __init__(self, directory: Path):
        secret_file = Path(directory) / "secret"
        if not secret_file.exists():
            secret_file.write_bytes(secrets.token_bytes(32))
            os.chmod(secret_file, 0o600)
        self.key = secret_file.read_bytes()

    def _sign(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.key, payload, hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self, user: str, version: int) -> str:
        payload = json.dumps({"u": user, "exp": int(time.time()) + SESSION_DAYS * 86400,
                              "sid": secrets.token_urlsafe(12), "v": version}).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + self._sign(payload)

    def read(self, cookie: str) -> dict | None:
        try:
            body, sig = cookie.split(".", 1)
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        except (ValueError, TypeError):
            return None
        if not token_matches(sig, self._sign(payload)):
            return None
        data = json.loads(payload)
        return data if data.get("exp", 0) > time.time() else None

    def csrf(self, session: dict) -> str:
        return hmac.new(self.key, ("csrf:" + session["sid"]).encode(), hashlib.sha256).hexdigest()[:32]


class Throttle:
    """After 5 failed logins per user or address, wait 5 minutes."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = {}

    def blocked(self, *keys: str) -> bool:
        now = time.time()
        for k in keys:
            recent = [t for t in self.failures.get(k, []) if now - t < self.window]
            self.failures[k] = recent
            if len(recent) >= self.limit:
                return True
        return False

    def fail(self, *keys: str) -> None:
        for k in keys:
            self.failures.setdefault(k, []).append(time.time())

    def clear(self, *keys: str) -> None:
        for k in keys:
            self.failures.pop(k, None)

"""Employee login: scrypt over the client-derived auth key, signed session cookies, login throttle.

The browser never sends the password itself: it sends PBKDF2(password, "<kennung>-auth:" + benutzer), so the portal
can verify a login without being able to unwrap the employee's document key (which uses a different salt).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field

SESSION_DAYS = 60
MAX_FAILS = 5
LOCK_SECONDS = 300


def hash_secret(value: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_secret(value: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        # constant-ish work so a missing account does not answer faster
        hashlib.scrypt(value.encode(), salt=b"0" * 16, n=2 ** 14, r=8, p=1, dklen=32)
        return False
    _, salt, digest = stored.split("$")
    got = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), digest)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_session(secret: bytes, person: str, gen: int, days: int = SESSION_DAYS) -> str:
    body = _b64(json.dumps({"p": person, "g": gen, "e": int(time.time()) + days * 86400}).encode())
    mac = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def read_session(secret: bytes, token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    want = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, want):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    return data if data.get("e", 0) > time.time() else None


def bearer_ok(header: str | None, token_sha256: str) -> bool:
    if not token_sha256 or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(sha256(header[7:].strip()), token_sha256)


@dataclass
class Throttle:
    """At most MAX_FAILS failed attempts per key (user or address), then LOCK_SECONDS of silence."""
    fails: dict[str, list[float]] = field(default_factory=dict)

    def locked(self, *keys: str) -> bool:
        now = time.time()
        for key in keys:
            recent = [t for t in self.fails.get(key, []) if now - t < LOCK_SECONDS]
            self.fails[key] = recent
            if len(recent) >= MAX_FAILS:
                return True
        return False

    def fail(self, *keys: str) -> None:
        for key in keys:
            self.fails.setdefault(key, []).append(time.time())

    def clear(self, *keys: str) -> None:
        for key in keys:
            self.fails.pop(key, None)


def new_secret() -> str:
    return secrets.token_urlsafe(32)

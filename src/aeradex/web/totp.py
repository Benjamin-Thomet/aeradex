"""Second factor for server mode: time-based one-time codes (TOTP, RFC 6238) from an authenticator app.

Only the standard library: HMAC-SHA1, 30-second steps, 6 digits, one step of clock drift either way. Recovery
codes are shown once and stored as SHA-256 only; each works once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
DRIFT = 1                 # accept the previous and the next 30-second step
RECOVERY_CODES = 10


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def counter(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def verify(secret: str, code: str, now: float | None = None, last_used: int = -1) -> int | None:
    """The matched time step, or None. A step at or before `last_used` is refused, so a code works only once."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    current = counter(now)
    for step in range(current - DRIFT, current + DRIFT + 1):
        if step > last_used and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str, issuer: str) -> str:
    """The otpauth:// link an authenticator app reads from the QR code."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def qr_svg(text: str) -> str:
    """The link as an inline SVG QR code (qrcode comes with qrbill)."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    svg = img.to_string(encoding="unicode")
    return svg[svg.index("<svg"):]


def recovery_codes() -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(RECOVERY_CODES)]


def recovery_hash(code: str) -> str:
    return hashlib.sha256("".join(ch for ch in (code or "").lower() if ch.isalnum()).encode()).hexdigest()

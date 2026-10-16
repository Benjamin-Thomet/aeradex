"""Documents for one employee, readable only with their key (protocol `<kennung>-doc-v1`).

ECDH P-256 (ephemeral key × the employee's public key from the browser) → HKDF-SHA256 → AES-256-GCM with
"{person}/{doc_id}" as associated data, so a document cannot be moved to another person or slot. The Kennung
(salt and info of the HKDF) is the portal's, so its phones derive the same key.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _key(shared: bytes, kennung: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=f"{kennung}-doc-salt-v1".encode(),
                info=f"{kennung}-doc-v1".encode()).derive(shared)


def encrypt(pubkey_b64: str, person: str, doc_id: str, data: bytes, kennung: str = "aeradex") -> dict:
    public = serialization.load_der_public_key(base64.b64decode(pubkey_b64))
    if not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != "secp256r1":
        raise ValueError("Schlüssel ist kein P-256-Schlüssel")
    eph = ec.generate_private_key(ec.SECP256R1())
    key = _key(eph.exchange(ec.ECDH(), public), kennung)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, data, f"{person}/{doc_id}".encode())
    epk = eph.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"epk": base64.b64encode(epk).decode(), "iv": base64.b64encode(iv).decode(),
            "ct": base64.b64encode(ct).decode()}


def decrypt(private: ec.EllipticCurvePrivateKey, person: str, doc_id: str, envelope: dict,
            kennung: str = "aeradex") -> bytes:
    """What the browser does; used by the tests."""
    epk = serialization.load_der_public_key(base64.b64decode(envelope["epk"]))
    key = _key(private.exchange(ec.ECDH(), epk), kennung)
    return AESGCM(key).decrypt(base64.b64decode(envelope["iv"]), base64.b64decode(envelope["ct"]),
                               f"{person}/{doc_id}".encode())

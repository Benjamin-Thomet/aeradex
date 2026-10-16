"""Helpers for tests of the portal and of the aeradex plugin «team» (an in-process portal, phones)."""
import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from . import auth

TOKEN = "sync-token-for-tests"
H = {"x-aeradex": "1"}


def auth_key(password: str, benutzer: str, kennung: str = "aeradex") -> str:
    """What the browser sends instead of the password (app.js authKey)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), f"{kennung}-auth:{benutzer}".encode(), 100_000, 32).hex()


def client(app) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def invite(pc, person="M0001", benutzer="anna", name="Anna Muster", code="ABCD2345"):
    r = pc.put("/app/api/sync/accounts", json={
        "accounts": [{"person": person, "benutzer": benutzer, "name": name, "aktiv": True}],
        "invites": [{"person": person, "code_sha256": auth.sha256(code), "expires": "2999-01-01"}]})
    assert r.status_code == 200, r.text
    return code


def activate(app, benutzer="anna", code="ABCD-2345", password="geheim-1234", kennung="aeradex"):
    """An employee phone: activates with a fresh key pair and stays logged in."""
    phone = client(app)
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    r = phone.post("/app/api/aktivieren", headers={f"x-{kennung}": "1"}, json={
        "benutzer": benutzer, "code": code, "auth": auth_key(password, benutzer, kennung),
        "pubkey": base64.b64encode(spki).decode(), "wrapped": {"salt": "s", "iv": "i", "ct": "c", "iter": 600000}})
    assert r.status_code == 200, r.text
    phone.private_key = priv
    return phone

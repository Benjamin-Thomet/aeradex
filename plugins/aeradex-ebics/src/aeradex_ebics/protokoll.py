"""EBICS 3.0 (H005) client, independent of aeradex: the keys, the messages, the crypto.

What it does: initialise a participant (INI, HIA), fetch the bank's keys (HPB) and download
documents (BTD, e.g. camt.053 «EOP»). Uploads (payments) are not part of it.

The three participant keys:

- A006  electronic signature (RSASSA-PSS, SHA-256) — only sent with INI, needed for uploads later
- X002  authentication (RSA PKCS#1 v1.5, SHA-256): signs every request (XML-DSig over `authenticate="true"`)
- E002  encryption: the bank encrypts the order data with AES-128-CBC and the AES key with this RSA key

H005 carries every key in a self-signed X.509 certificate (allowed by the Swiss market practice).
The «hash» on the initialisation letter and in BankPubKeyDigests is SHA-256 over the DER certificate;
the letter shows the older key hash (SHA-256 over "<exponent hex> <modulus hex>") too.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import os
import secrets
import ssl
import urllib.request
import zlib
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import NameOID
from lxml import etree

NS = "urn:org:ebics:H005"
DS = "http://www.w3.org/2000/09/xmldsig#"
S002 = "http://www.ebics.org/S002"
H000 = "http://www.ebics.org/H000"
NSMAP = {None: NS, "ds": DS}
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
KEY_SIZE = 2048

# Return codes worth a sentence in German; everything else shows the bank's own text.
CODES = {
    "000000": "OK",
    "011000": "Abholung bestätigt",
    "061001": "Authentifizierung fehlgeschlagen (Signatur der Anfrage ungültig)",
    "061002": "Die Bank kann die Anfrage nicht lesen",
    "061099": "Interner Fehler der Bank",
    "090003": "Dieser Auftrag ist für den Teilnehmer nicht freigeschaltet",
    "090004": "Die Auftragsdaten sind ungültig",
    "090005": "Keine neuen Daten zum Abholen",
    "091002": "Teilnehmer noch nicht freigeschaltet oder gesperrt (Initialisierungsbrief schon bei der Bank?)",
    "091003": "Unbekannter oder nicht berechtigter Teilnehmer",
    "091004": "Teilnehmer noch nicht freigeschaltet (unterschriebener Initialisierungsbrief schon bei der Bank?)",
    "091005": "Auftragsart nicht erlaubt",
    "091008": "Die Bankschlüssel stimmen nicht mehr (neu mit HPB holen)",
    "091010": "Die Schlüsselversion wird von der Bank nicht unterstützt",
    "091011": "Der Teilnehmer ist nicht freigeschaltet oder gesperrt",
    "091116": "Die Bank hat diesen Auftrag schon (doppelt)",
    "091118": "Die Bank hat den Schlüssel schon (INI/HIA wurde schon gesendet)",
    "091120": "Der Teilnehmer ist schon initialisiert",
}


class EbicsFehler(RuntimeError):
    def __init__(self, code: str, text: str = ""):
        self.code, self.text = code, text
        known = CODES.get(code, "")
        super().__init__(f"EBICS {code}: {known or text or 'Fehler'}" + (f" ({text})" if known and text else ""))


# ---------- keys and certificates ----------

@dataclass
class Schluessel:
    """The participant's three keys with their certificates, and — after HPB — the bank's two certificates."""
    a006: rsa.RSAPrivateKey
    x002: rsa.RSAPrivateKey
    e002: rsa.RSAPrivateKey
    cert_a: x509.Certificate
    cert_x: x509.Certificate
    cert_e: x509.Certificate
    bank_x: x509.Certificate | None = None
    bank_e: x509.Certificate | None = None


def _name(partner: str, user: str, firma: str = "") -> x509.Name:
    parts = [x509.NameAttribute(NameOID.COMMON_NAME, user)]
    if firma:
        parts.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, firma[:64]))
    parts += [x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, partner), x509.NameAttribute(NameOID.COUNTRY_NAME, "CH")]
    return x509.Name(parts)


def zertifikat(key: rsa.RSAPrivateKey, name: x509.Name) -> x509.Certificate:
    """A self-signed certificate. Validity until 9999-12-31: the Swiss guidelines' «unlimited»
    (an expired certificate would mean initialising with the bank again)."""
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(secrets.randbits(63) | 1).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(dt.datetime(9999, 12, 31, 23, 59, 59, tzinfo=dt.UTC))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))


def neue_schluessel(partner: str, user: str, firma: str = "", bits: int = KEY_SIZE) -> Schluessel:
    keys = [rsa.generate_private_key(public_exponent=65537, key_size=bits) for _ in range(3)]
    name = _name(partner, user, firma)
    return Schluessel(*keys, *(zertifikat(k, name) for k in keys))


def key_hash(public_key) -> bytes:
    """SHA-256 over "<exponent hex> <modulus hex>" (lower case, no leading zeros) — the EBICS key hash."""
    nums = public_key.public_numbers()
    return hashlib.sha256(f"{nums.e:x} {nums.n:x}".encode()).digest()


def cert_hash(cert: x509.Certificate) -> bytes:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()


def hex_paare(digest: bytes) -> str:
    return " ".join(f"{b:02X}" for b in digest)


def normalise_hash(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch in "0123456789abcdef")


# ---------- XML ----------

def _el(parent, tag, text=None, ns=NS, **attrs):
    el = etree.SubElement(parent, f"{{{ns}}}{tag}", {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _root(tag: str) -> etree._Element:
    return etree.Element(f"{{{NS}}}{tag}", {"Version": "H005", "Revision": "1"}, nsmap=NSMAP)


def _xml(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _x509_data(parent, cert: x509.Certificate):
    data = _el(parent, "X509Data", ns=DS)
    serial = _el(data, "X509IssuerSerial", ns=DS)
    _el(serial, "X509IssuerName", cert.issuer.rfc4514_string(), ns=DS)
    _el(serial, "X509SerialNumber", cert.serial_number, ns=DS)
    _el(data, "X509Certificate", base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode(), ns=DS)


@dataclass
class Teilnehmer:
    url: str
    host_id: str
    partner_id: str
    user_id: str
    produkt: str = "aeradex"
    sprache: str = "de"


def _static(header, t: Teilnehmer, *, nonce: bool = True, ids: bool = True):
    static = _el(header, "static")
    _el(static, "HostID", t.host_id)
    if nonce:
        _el(static, "Nonce", secrets.token_hex(16).upper())
        _el(static, "Timestamp", dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    if ids:
        _el(static, "PartnerID", t.partner_id)
        _el(static, "UserID", t.user_id)
        _el(static, "Product", t.produkt, Language=t.sprache)
    return static


def unsigned_order_data(xml_root) -> str:
    """Order data of INI/HIA: compressed, base64, not encrypted."""
    return base64.b64encode(zlib.compress(_xml(xml_root))).decode()


def ini_order_data(t: Teilnehmer, k: Schluessel):
    root = etree.Element(f"{{{S002}}}SignaturePubKeyOrderData", nsmap={None: S002, "ds": DS})
    info = _el(root, "SignaturePubKeyInfo", ns=S002)
    _x509_data(info, k.cert_a)
    _el(info, "SignatureVersion", "A006", ns=S002)
    _el(root, "PartnerID", t.partner_id, ns=S002)
    _el(root, "UserID", t.user_id, ns=S002)
    return root


def hia_order_data(t: Teilnehmer, k: Schluessel):
    root = etree.Element(f"{{{NS}}}HIARequestOrderData", nsmap=NSMAP)
    auth = _el(root, "AuthenticationPubKeyInfo")
    _x509_data(auth, k.cert_x)
    _el(auth, "AuthenticationVersion", "X002")
    enc = _el(root, "EncryptionPubKeyInfo")
    _x509_data(enc, k.cert_e)
    _el(enc, "EncryptionVersion", "E002")
    _el(root, "PartnerID", t.partner_id)
    _el(root, "UserID", t.user_id)
    return root


def unsecured_request(t: Teilnehmer, k: Schluessel, order_type: str) -> bytes:
    """INI or HIA: ebicsUnsecuredRequest, no signature (the letter on paper is the proof)."""
    root = _root("ebicsUnsecuredRequest")
    header = _el(root, "header", authenticate="true")
    static = _static(header, t, nonce=False)
    details = _el(static, "OrderDetails")
    _el(details, "AdminOrderType", order_type)
    _el(static, "SecurityMedium", "0000")
    _el(header, "mutable")
    body = _el(root, "body")
    transfer = _el(body, "DataTransfer")
    data = ini_order_data(t, k) if order_type == "INI" else hia_order_data(t, k)
    _el(transfer, "OrderData", unsigned_order_data(data))
    return _xml(root)


def _c14n(el) -> bytes:
    """Inclusive C14N 1.0 of the subtree at `el`, with every namespace in scope on it — as libxml2 and the
    banks compute it. (lxml's own c14n of a non-root element writes xmlns="" on the children: wrong.)"""
    top = etree.Element(el.tag, attrib=dict(el.attrib), nsmap=el.nsmap)
    top.text = el.text
    for child in el:
        top.append(copy.deepcopy(child))
    return etree.tostring(top, method="c14n", exclusive=False, with_comments=False)


def sign(root, x002: rsa.RSAPrivateKey) -> None:
    """Insert AuthSignature right after the header: XML-DSig over all elements with authenticate="true"."""
    digest = hashlib.sha256(b"".join(_c14n(el) for el in root.xpath("//*[@authenticate='true']"))).digest()
    header = root.find(f"{{{NS}}}header")
    auth = etree.Element(f"{{{NS}}}AuthSignature")
    header.addnext(auth)
    info = _el(auth, "SignedInfo", ns=DS)
    _el(info, "CanonicalizationMethod", ns=DS, Algorithm=C14N)
    _el(info, "SignatureMethod", ns=DS, Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")
    ref = _el(info, "Reference", ns=DS, URI="#xpointer(//*[@authenticate='true'])")
    _el(_el(ref, "Transforms", ns=DS), "Transform", ns=DS, Algorithm=C14N)
    _el(ref, "DigestMethod", ns=DS, Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
    _el(ref, "DigestValue", base64.b64encode(digest).decode(), ns=DS)
    value = x002.sign(_c14n(info), padding.PKCS1v15(), hashes.SHA256())
    _el(auth, "SignatureValue", base64.b64encode(value).decode(), ns=DS)


def hpb_request(t: Teilnehmer, k: Schluessel) -> bytes:
    root = _root("ebicsNoPubKeyDigestsRequest")
    header = _el(root, "header", authenticate="true")
    static = _static(header, t)
    _el(_el(static, "OrderDetails"), "AdminOrderType", "HPB")
    _el(static, "SecurityMedium", "0000")
    _el(header, "mutable")
    _el(root, "body")
    sign(root, k.x002)
    return _xml(root)


@dataclass
class Dienst:
    """A BTF service (EBICS 3.0), e.g. camt.053 end-of-day statements in Switzerland: EOP / CH / ZIP."""
    name: str = "EOP"
    msg: str = "camt.053"
    scope: str = "CH"
    option: str = ""
    container: str = "ZIP"
    version: str = ""


CAMT053 = Dienst()


# Swiss uploads and status reports (AEK: XE2 / Z01)
PAIN001 = Dienst(name="MCT", msg="pain.001", scope="CH", container="", version="09")
PAIN002 = Dienst(name="PSR", msg="pain.002", scope="CH", container="ZIP")
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
SEGMENT = 750_000          # encrypted bytes per segment: base64 stays below the 1 MB EBICS limit


def _service(params, dienst: Dienst):
    service = _el(params, "Service")
    _el(service, "ServiceName", dienst.name)
    if dienst.scope:
        _el(service, "Scope", dienst.scope)
    if dienst.option:
        _el(service, "ServiceOption", dienst.option)
    if dienst.container:
        _el(service, "Container", containerType=dienst.container)
    msg = _el(service, "MsgName", dienst.msg)
    if dienst.version:
        msg.set("version", dienst.version)


def _bank_digests(static, k: Schluessel):
    if k.bank_x is None or k.bank_e is None:
        raise EbicsFehler("------", "Die Bankschlüssel fehlen noch (zuerst HPB)")
    digests = _el(static, "BankPubKeyDigests")
    _el(digests, "Authentication", base64.b64encode(cert_hash(k.bank_x)).decode(), Version="X002", Algorithm=SHA256)
    _el(digests, "Encryption", base64.b64encode(cert_hash(k.bank_e)).decode(), Version="E002", Algorithm=SHA256)


def btd_request(t: Teilnehmer, k: Schluessel, dienst: Dienst, von: dt.date | None = None,
                bis: dt.date | None = None) -> bytes:
    root = _root("ebicsRequest")
    header = _el(root, "header", authenticate="true")
    static = _static(header, t)
    details = _el(static, "OrderDetails")
    _el(details, "AdminOrderType", "BTD")
    params = _el(details, "BTDOrderParams")
    _service(params, dienst)
    if von and bis:
        rng = _el(params, "DateRange")
        _el(rng, "Start", von.isoformat())
        _el(rng, "End", bis.isoformat())
    _bank_digests(static, k)
    _el(static, "SecurityMedium", "0000")
    _el(_el(header, "mutable"), "TransactionPhase", "Initialisation")
    _el(root, "body")
    sign(root, k.x002)
    return _xml(root)


# ---------- upload ----------

def order_digest(data: bytes) -> bytes:
    """SHA-256 of the order data for the electronic signature: CR and LF are left out (EBICS rule; the bank
    strips them before verifying). The data itself travels unchanged."""
    return hashlib.sha256(data.replace(b"\r", b"").replace(b"\n", b"")).digest()


def a006(key: rsa.RSAPrivateKey, digest: bytes) -> bytes:
    """A006: RSASSA-PSS, SHA-256, MGF1-SHA-256, salt 32 — over the order digest (as epics and ebics-client-php)."""
    return key.sign(digest, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())


def user_signature_data(t: Teilnehmer, k: Schluessel, digest: bytes) -> bytes:
    root = etree.Element(f"{{{S002}}}UserSignatureData", nsmap={None: S002})
    data = _el(root, "OrderSignatureData", ns=S002)
    _el(data, "SignatureVersion", "A006", ns=S002)
    _el(data, "SignatureValue", base64.b64encode(a006(k.a006, digest)).decode(), ns=S002)
    _el(data, "PartnerID", t.partner_id, ns=S002)
    _el(data, "UserID", t.user_id, ns=S002)
    return _xml(root)


def aes_encrypt(key: bytes, data: bytes) -> bytes:
    """zlib, then AES-128-CBC (IV zero) with EBICS padding (zeros, last byte = number of padding bytes)."""
    raw = zlib.compress(data)
    pad = 16 - len(raw) % 16
    raw += b"\0" * (pad - 1) + bytes([pad])
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return enc.update(raw) + enc.finalize()


def btu_request(t: Teilnehmer, k: Schluessel, dienst: Dienst, dateiname: str, transaction_key: bytes,
                digest: bytes, segmente: int) -> bytes:
    root = _root("ebicsRequest")
    header = _el(root, "header", authenticate="true")
    static = _static(header, t)
    details = _el(static, "OrderDetails")
    _el(details, "AdminOrderType", "BTU")
    params = _el(details, "BTUOrderParams", fileName=dateiname[:250])
    _service(params, dienst)
    _el(params, "SignatureFlag")                     # the signature is in this order (AEK: mandatory)
    _bank_digests(static, k)
    _el(static, "SecurityMedium", "0000")
    _el(static, "NumSegments", segmente)
    _el(_el(header, "mutable"), "TransactionPhase", "Initialisation")
    body = _el(root, "body")
    transfer = _el(body, "DataTransfer")
    info = _el(transfer, "DataEncryptionInfo", authenticate="true")
    _el(info, "EncryptionPubKeyDigest", base64.b64encode(cert_hash(k.bank_e)).decode(), Version="E002",
        Algorithm=SHA256)
    tk = k.bank_e.public_key().encrypt(transaction_key, padding.PKCS1v15())
    _el(info, "TransactionKey", base64.b64encode(tk).decode())
    sig = aes_encrypt(transaction_key, user_signature_data(t, k, digest))
    _el(transfer, "SignatureData", base64.b64encode(sig).decode(), authenticate="true")
    _el(transfer, "DataDigest", base64.b64encode(digest).decode(), SignatureVersion="A006")
    sign(root, k.x002)
    return _xml(root)


def upload_transfer_request(t: Teilnehmer, k: Schluessel, tid: str, segment: int, last: bool, data: bytes) -> bytes:
    root, mutable = _transaction_request(t, k, tid, "Transfer")
    _el(mutable, "SegmentNumber", segment, lastSegment="true" if last else "false")
    _el(_el(_el(root, "body"), "DataTransfer"), "OrderData", base64.b64encode(data).decode())
    sign(root, k.x002)
    return _xml(root)


def _transaction_request(t: Teilnehmer, k: Schluessel, tid: str, phase: str) -> tuple:
    root = _root("ebicsRequest")
    header = _el(root, "header", authenticate="true")
    static = _el(header, "static")
    _el(static, "HostID", t.host_id)
    _el(static, "TransactionID", tid)
    mutable = _el(header, "mutable")
    _el(mutable, "TransactionPhase", phase)
    return root, mutable


def transfer_request(t: Teilnehmer, k: Schluessel, tid: str, segment: int, last: bool) -> bytes:
    root, mutable = _transaction_request(t, k, tid, "Transfer")
    _el(mutable, "SegmentNumber", segment, lastSegment="true" if last else "false")
    _el(root, "body")
    sign(root, k.x002)
    return _xml(root)


def receipt_request(t: Teilnehmer, k: Schluessel, tid: str, ok: bool = True) -> bytes:
    root, _ = _transaction_request(t, k, tid, "Receipt")
    body = _el(root, "body")
    _el(_el(body, "TransferReceipt", authenticate="true"), "ReceiptCode", "0" if ok else "1")
    sign(root, k.x002)
    return _xml(root)


def hev_request(host_id: str) -> bytes:
    root = etree.Element(f"{{{H000}}}ebicsHEVRequest", nsmap={None: H000})
    etree.SubElement(root, f"{{{H000}}}HostID").text = host_id
    return _xml(root)


# ---------- responses ----------

@dataclass
class Antwort:
    technisch: str
    fachlich: str
    text: str
    transaction_id: str = ""
    segmente: int = 1
    segment: int = 1
    letztes: bool = True
    transaction_key: bytes = b""
    order_data: bytes = b""
    order_id: str = ""
    xml: bytes = field(default=b"", repr=False)

    @property
    def code(self) -> str:
        """The code that matters: a technical error first, else the business one."""
        if self.technisch not in ("", "000000", "011000"):
            return self.technisch
        return self.fachlich or self.technisch or "000000"


def _find(root, path: str) -> str:
    found = root.find(path, {"e": NS, "ds": DS})
    return (found.text or "").strip() if found is not None and found.text else ""


def parse(xml: bytes) -> Antwort:
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
    except etree.XMLSyntaxError as exc:
        raise EbicsFehler("------", f"Keine gültige Antwort der Bank: {exc}") from None
    if etree.QName(root).namespace == H000:
        code = _find(root, "{%s}SystemReturnCode/{%s}ReturnCode" % (H000, H000))
        return Antwort(code, "", _find(root, "{%s}SystemReturnCode/{%s}ReportText" % (H000, H000)), xml=xml)
    seg = root.find("e:header/e:mutable/e:SegmentNumber", {"e": NS})
    tk = _find(root, "e:body/e:DataTransfer/e:DataEncryptionInfo/e:TransactionKey")
    data = _find(root, "e:body/e:DataTransfer/e:OrderData")
    return Antwort(
        technisch=_find(root, "e:header/e:mutable/e:ReturnCode"),
        fachlich=_find(root, "e:body/e:ReturnCode"),
        text=_find(root, "e:header/e:mutable/e:ReportText"),
        transaction_id=_find(root, "e:header/e:static/e:TransactionID"),
        segmente=int(_find(root, "e:header/e:static/e:NumSegments") or 1),
        segment=int(seg.text) if seg is not None and seg.text else 1,
        letztes=seg is None or seg.get("lastSegment") in ("true", "1"),
        transaction_key=base64.b64decode(tk) if tk else b"",
        order_data=base64.b64decode(data) if data else b"",
        order_id=_find(root, "e:header/e:mutable/e:OrderID"),
        xml=xml)


def decrypt(order_data: bytes, transaction_key: bytes, e002: rsa.RSAPrivateKey) -> bytes:
    """AES-128-CBC (IV zero) with the RSA-decrypted transaction key, then zlib."""
    key = e002.decrypt(transaction_key, padding.PKCS1v15())
    if len(order_data) % 16:
        raise EbicsFehler("------", "Die Auftragsdaten der Bank sind unvollständig")
    plain = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    data = plain.update(order_data) + plain.finalize()
    pad = data[-1] if data else 0
    if 1 <= pad <= 16:
        data = data[:-pad]
    out = zlib.decompressobj()
    return out.decompress(data) + out.flush()


def encrypt(data: bytes, bank_e002_public) -> tuple[bytes, bytes]:
    """The bank's side (used by the test bank): zlib, AES with padding (ANSI X9.23), key under E002."""
    key = os.urandom(16)
    return aes_encrypt(key, data), bank_e002_public.encrypt(key, padding.PKCS1v15())


def bank_zertifikate(order_data_xml: bytes) -> dict[str, x509.Certificate]:
    """HPBResponseOrderData → {"X002": cert, "E002": cert}."""
    root = etree.fromstring(order_data_xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    out = {}
    for info, version in (("AuthenticationPubKeyInfo", "AuthenticationVersion"),
                          ("EncryptionPubKeyInfo", "EncryptionVersion")):
        node = root.find(f"e:{info}", {"e": NS})
        if node is None:
            continue
        der = _find(node, "ds:X509Data/ds:X509Certificate")
        if not der:
            raise EbicsFehler("------", "Die Bank hat keine Zertifikate geliefert (EBICS 3.0 braucht sie)")
        out[_find(node, f"e:{version}")] = x509.load_der_x509_certificate(base64.b64decode(der))
    if not {"X002", "E002"} <= set(out):
        raise EbicsFehler("------", f"Unerwartete Bankschlüssel: {', '.join(out) or 'keine'}")
    return out


# ---------- the client ----------

def http_post(url: str, body: bytes, timeout: float = 90) -> bytes:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "text/xml; charset=UTF-8", "User-Agent": "aeradex-ebics"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return resp.read()


class Client:
    def __init__(self, t: Teilnehmer, k: Schluessel | None, post=None):
        self.t, self.k = t, k
        self.post = post or (lambda body: http_post(t.url, body))

    def _send(self, body: bytes, ok=("000000",)) -> Antwort:
        answer = parse(self.post(body))
        if answer.code not in ok:
            raise EbicsFehler(answer.code, answer.text)
        return answer

    def hev(self) -> list[str]:
        answer = parse(self.post(hev_request(self.t.host_id)))
        if answer.technisch != "000000":
            raise EbicsFehler(answer.technisch, answer.text)
        root = etree.fromstring(answer.xml)
        return [el.get("ProtocolVersion") for el in root.iter(f"{{{H000}}}VersionNumber")]

    def ini(self) -> Antwort:
        return self._send(unsecured_request(self.t, self.k, "INI"))

    def hia(self) -> Antwort:
        return self._send(unsecured_request(self.t, self.k, "HIA"))

    def hpb(self) -> dict[str, x509.Certificate]:
        answer = self._send(hpb_request(self.t, self.k))
        return bank_zertifikate(decrypt(answer.order_data, answer.transaction_key, self.k.e002))

    def download(self, dienst: Dienst = CAMT053, von: dt.date | None = None, bis: dt.date | None = None):
        """(transaction id, data) or None when the bank has nothing new. The caller confirms with `quittung`
        once the data is safe — only then does the bank mark it as fetched."""
        first = self._send(btd_request(self.t, self.k, dienst, von, bis), ok=("000000", "090005"))
        if first.code == "090005":
            return None
        parts = [first.order_data]
        for n in range(2, first.segmente + 1):
            parts.append(self._send(transfer_request(self.t, self.k, first.transaction_id, n,
                                                     n == first.segmente)).order_data)
        try:
            data = decrypt(b"".join(parts), first.transaction_key, self.k.e002)
        except EbicsFehler:
            self.quittung(first.transaction_id, False)
            raise
        except Exception as exc:
            self.quittung(first.transaction_id, False)
            raise EbicsFehler("------", f"Die Daten der Bank liessen sich nicht entschlüsseln: {exc}") from None
        return first.transaction_id, data

    def upload(self, dienst: Dienst, data: bytes, dateiname: str) -> tuple[str, str]:
        """Send an order (e.g. pain.001) with the participant's A006 signature. Returns (transaction id, order id)."""
        key = os.urandom(16)
        encrypted = aes_encrypt(key, data)
        parts = [encrypted[i:i + SEGMENT] for i in range(0, len(encrypted), SEGMENT)]
        first = self._send(btu_request(self.t, self.k, dienst, dateiname, key, order_digest(data), len(parts)))
        order_id = first.order_id
        for n, part in enumerate(parts, 1):
            answer = self._send(upload_transfer_request(self.t, self.k, first.transaction_id, n, n == len(parts), part))
            order_id = answer.order_id or order_id
        return first.transaction_id, order_id

    def quittung(self, tid: str, ok: bool = True) -> None:
        try:
            self._send(receipt_request(self.t, self.k, tid, ok), ok=("000000", "011000", "011001"))
        except EbicsFehler:
            if ok:
                raise

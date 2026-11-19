"""A small EBICS H005 bank for the tests: validates every request against the official schemas,
verifies the X002 signature with the key the participant sent in HIA, and answers like a bank would."""
from __future__ import annotations

import base64
import hashlib
import zlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

from aeradex_ebics import protokoll as p

XSD = Path(__file__).parent / "xsd"
_schemas: dict[str, etree.XMLSchema] = {}


def schema(name: str) -> etree.XMLSchema:
    if name not in _schemas:
        _schemas[name] = etree.XMLSchema(etree.parse(str(XSD / name)))
    return _schemas[name]


def validate(xml: bytes | etree._Element, name: str = "ebics_H005.xsd") -> etree._Element:
    root = etree.fromstring(xml) if isinstance(xml, bytes) else xml
    s = schema(name)
    if not s.validate(root):
        raise AssertionError(f"{name}: {s.error_log}")
    return root


E = {"e": p.NS, "ds": p.DS}


class Testbank:
    __test__ = False
    def __init__(self, host="TESTBANK", segment_size: int = 0):
        name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Testbank")])
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.x = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.e = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.cert_x, self.cert_e = p.zertifikat(self.x, name), p.zertifikat(self.e, name)
        self.host = host
        self.user_x = self.user_e = self.user_a = None
        self.aktiv = False                  # the bank releases the participant after the letter
        self.dateien: list[bytes] = []      # what waits to be fetched
        self.status: list[bytes] = []       # pain.002 status reports waiting
        self.transaktionen: dict[str, dict] = {}
        self.log: list[str] = []
        self.segment_size = segment_size
        self.btd_params: list[etree._Element] = []
        self.uploads: list[dict] = []          # what arrived complete and correctly signed
        self.upload_fehler = ""                # set to make the bank refuse the next upload

    # --- answers ---
    def _keymgmt(self, code="000000", body_code="000000", data: bytes | None = None) -> bytes:
        root = etree.Element(f"{{{p.NS}}}ebicsKeyManagementResponse", {"Version": "H005", "Revision": "1"}, nsmap=p.NSMAP)
        header = p._el(root, "header", authenticate="true")
        p._el(header, "static")
        mut = p._el(header, "mutable")
        p._el(mut, "ReturnCode", code)
        p._el(mut, "ReportText", "[EBICS_OK] OK" if code == "000000" else "[ERR]")
        body = p._el(root, "body")
        if data is not None:
            enc, tk = p.encrypt(data, self.user_e.public_key())
            dt_ = p._el(body, "DataTransfer")
            info = p._el(dt_, "DataEncryptionInfo", authenticate="true")
            p._el(info, "EncryptionPubKeyDigest", base64.b64encode(p.cert_hash(self.user_e)).decode(),
                  Version="E002", Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
            p._el(info, "TransactionKey", base64.b64encode(tk).decode())
            p._el(dt_, "OrderData", base64.b64encode(enc).decode())
        p._el(body, "ReturnCode", body_code, authenticate="true")
        validate(root)
        return p._xml(root)

    def _response(self, phase, code="000000", body_code="000000", tid="", segs=None, seg=None, last=True,
                  tk=None, data=None, order_id="") -> bytes:
        root = etree.Element(f"{{{p.NS}}}ebicsResponse", {"Version": "H005", "Revision": "1"}, nsmap=p.NSMAP)
        header = p._el(root, "header", authenticate="true")
        static = p._el(header, "static")
        if tid:
            p._el(static, "TransactionID", tid)
        if segs:
            p._el(static, "NumSegments", segs)
        mut = p._el(header, "mutable")
        p._el(mut, "TransactionPhase", phase)
        if seg:
            p._el(mut, "SegmentNumber", seg, lastSegment="true" if last else "false")
        if order_id:
            p._el(mut, "OrderID", order_id)
        p._el(mut, "ReturnCode", code)
        p._el(mut, "ReportText", "[EBICS_OK] OK")
        body = p._el(root, "body")
        if data is not None:
            dt_ = p._el(body, "DataTransfer")
            if tk is not None:
                info = p._el(dt_, "DataEncryptionInfo", authenticate="true")
                p._el(info, "EncryptionPubKeyDigest", base64.b64encode(p.cert_hash(self.user_e)).decode(),
                      Version="E002", Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
                p._el(info, "TransactionKey", base64.b64encode(tk).decode())
            p._el(dt_, "OrderData", base64.b64encode(data).decode())
        p._el(body, "ReturnCode", body_code, authenticate="true")
        p.sign(root, self.x)               # the bank signs its answers with its X002
        validate(root)
        return p._xml(root)

    # --- checks ---
    def _verify(self, root):
        digest = hashlib.sha256(b"".join(p._c14n(el) for el in root.xpath("//*[@authenticate='true']"))).digest()
        auth = root.find("e:AuthSignature", E)
        assert auth is not None, "AuthSignature fehlt"
        assert base64.b64decode(auth.findtext("ds:SignedInfo/ds:Reference/ds:DigestValue", namespaces=E)) == digest
        value = base64.b64decode(auth.findtext("ds:SignatureValue", namespaces=E))
        self.user_x.public_key().verify(value, p._c14n(auth.find("ds:SignedInfo", E)), padding.PKCS1v15(),
                                        hashes.SHA256())

    def __call__(self, body: bytes) -> bytes:
        root = etree.fromstring(body)
        tag = etree.QName(root).localname
        if tag == "ebicsHEVRequest":
            return (b'<?xml version="1.0" encoding="UTF-8"?><ebicsHEVResponse xmlns="http://www.ebics.org/H000">'
                    b'<SystemReturnCode><ReturnCode>000000</ReturnCode><ReportText>[EBICS_OK] OK</ReportText>'
                    b'</SystemReturnCode><VersionNumber ProtocolVersion="H005">03.00</VersionNumber></ebicsHEVResponse>')
        validate(root)
        assert root.findtext("e:header/e:static/e:HostID", namespaces=E) == self.host
        order = root.findtext("e:header/e:static/e:OrderDetails/e:AdminOrderType", namespaces=E)
        if tag == "ebicsUnsecuredRequest":
            raw = zlib.decompress(base64.b64decode(root.findtext("e:body/e:DataTransfer/e:OrderData", namespaces=E)))
            data = etree.fromstring(raw)
            self.log.append(order)
            if order == "INI":
                validate(data, "ebics_signature_S002.xsd")
                if self.user_a is not None:
                    return self._keymgmt("091118")
                self.user_a = _cert(data)
            else:
                validate(data)
                if self.user_x is not None:
                    return self._keymgmt("091118")
                certs = data.findall(".//ds:X509Certificate", E)
                self.user_x = x509.load_der_x509_certificate(base64.b64decode(certs[0].text))
                self.user_e = x509.load_der_x509_certificate(base64.b64decode(certs[1].text))
            return self._keymgmt()
        self._verify(root)
        if tag == "ebicsNoPubKeyDigestsRequest":
            self.log.append(order)
            if not self.aktiv:
                return self._keymgmt("091002")
            out = etree.Element(f"{{{p.NS}}}HPBResponseOrderData", nsmap=p.NSMAP)
            a = p._el(out, "AuthenticationPubKeyInfo")
            p._x509_data(a, self.cert_x)
            p._el(a, "AuthenticationVersion", "X002")
            e = p._el(out, "EncryptionPubKeyInfo")
            p._x509_data(e, self.cert_e)
            p._el(e, "EncryptionVersion", "E002")
            p._el(out, "HostID", self.host)
            validate(out)
            return self._keymgmt(data=p._xml(out))
        phase = root.findtext("e:header/e:mutable/e:TransactionPhase", namespaces=E)
        if phase == "Initialisation" and order == "BTU":
            return self._btu_init(root)
        if phase == "Initialisation":
            self.log.append(order)
            digests = root.find("e:header/e:static/e:BankPubKeyDigests", E)
            assert base64.b64decode(digests.findtext("e:Authentication", namespaces=E)) == p.cert_hash(self.cert_x)
            assert base64.b64decode(digests.findtext("e:Encryption", namespaces=E)) == p.cert_hash(self.cert_e)
            params = root.find("e:header/e:static/e:OrderDetails/e:BTDOrderParams", E)
            self.btd_params.append(params)
            queue = self.status if params.findtext("e:Service/e:MsgName", namespaces=E) == "pain.002" else self.dateien
            if not queue:
                return self._response(phase, body_code="090005")
            data = queue[0]
            enc, tk = p.encrypt(data, self.user_e.public_key())
            size = self.segment_size or len(enc)
            parts = [enc[i:i + size] for i in range(0, len(enc), size)]
            tid = hashlib.md5(enc).hexdigest().upper()
            self.transaktionen[tid] = {"parts": parts, "quittung": None, "queue": queue}
            return self._response(phase, tid=tid, segs=len(parts), seg=1, last=len(parts) == 1, tk=tk, data=parts[0])
        tid = root.findtext("e:header/e:static/e:TransactionID", namespaces=E)
        tx = self.transaktionen[tid]
        if phase == "Transfer" and tx.get("upload"):
            return self._btu_transfer(root, tid, tx)
        if phase == "Transfer":
            n = int(root.findtext("e:header/e:mutable/e:SegmentNumber", namespaces=E))
            return self._response(phase, tid=tid, seg=n, last=n == len(tx["parts"]), data=tx["parts"][n - 1])
        code = root.findtext("e:body/e:TransferReceipt/e:ReceiptCode", namespaces=E)
        tx["quittung"] = code
        if code == "0":
            tx["queue"].pop(0)
            return self._response(phase, code="011000")
        return self._response(phase, code="011001")


def _decrypt(self, tk_b64: str, data: bytes) -> bytes:
    key = self.e.decrypt(base64.b64decode(tk_b64), padding.PKCS1v15())
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    raw = dec.update(data) + dec.finalize()
    return zlib.decompress(raw[:-raw[-1]])


def _btu_init(self, root):
    self.log.append("BTU")
    params = root.find("e:header/e:static/e:OrderDetails/e:BTUOrderParams", E)
    assert params.find("e:SignatureFlag", E) is not None
    info = root.find("e:body/e:DataTransfer/e:DataEncryptionInfo", E)
    assert base64.b64decode(info.findtext("e:EncryptionPubKeyDigest", namespaces=E)) == p.cert_hash(self.cert_e)
    tk = info.findtext("e:TransactionKey", namespaces=E)
    sig = etree.fromstring(_decrypt(self, tk, base64.b64decode(
        root.findtext("e:body/e:DataTransfer/e:SignatureData", namespaces=E))))
    validate(sig, "ebics_signature_S002.xsd")
    s2 = {"s": p.S002}
    value = base64.b64decode(sig.findtext("s:OrderSignatureData/s:SignatureValue", namespaces=s2))
    digest = base64.b64decode(root.findtext("e:body/e:DataTransfer/e:DataDigest", namespaces=E))
    assert root.find("e:body/e:DataTransfer/e:DataDigest", E).get("SignatureVersion") == "A006"
    self.user_a.public_key().verify(value, digest, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                                    hashes.SHA256())
    segs = int(root.findtext("e:header/e:static/e:NumSegments", namespaces=E))
    tid = hashlib.md5(value).hexdigest().upper()
    self.transaktionen[tid] = {"upload": True, "tk": tk, "digest": digest, "segs": segs, "parts": [],
                               "params": params, "order_id": f"A{len(self.uploads) + 1:03d}"}
    if self.upload_fehler:
        code, self.upload_fehler = self.upload_fehler, ""
        return self._response("Initialisation", body_code=code)
    return self._response("Initialisation", tid=tid)


def _btu_transfer(self, root, tid, tx):
    n = int(root.findtext("e:header/e:mutable/e:SegmentNumber", namespaces=E))
    assert n == len(tx["parts"]) + 1
    tx["parts"].append(base64.b64decode(root.findtext("e:body/e:DataTransfer/e:OrderData", namespaces=E)))
    if n < tx["segs"]:
        return self._response("Transfer", tid=tid, seg=n, last=False)
    data = _decrypt(self, tx["tk"], b"".join(tx["parts"]))
    assert p.order_digest(data) == tx["digest"], "Unterschrift passt nicht zu den Daten"
    self.uploads.append({"data": data, "order_id": tx["order_id"], "params": tx["params"]})
    return self._response("Transfer", tid=tid, seg=n, last=True, order_id=tx["order_id"])


def _cert(data):
    return x509.load_der_x509_certificate(base64.b64decode(data.find(".//ds:X509Certificate", E).text))


Testbank._btu_init = _btu_init
Testbank._btu_transfer = _btu_transfer

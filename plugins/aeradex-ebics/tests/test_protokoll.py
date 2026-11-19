import datetime as dt
import io
import os
import zipfile

import pytest

from aeradex_ebics import protokoll as p

from testbank import Testbank, validate


@pytest.fixture(scope="module")
def keys():
    return p.neue_schluessel("PARTNER1", "USER1", "Muster GmbH")


def _client(bank, keys):
    t = p.Teilnehmer("https://bank.example/ebics", bank.host, "PARTNER1", "USER1")
    return p.Client(t, keys, post=bank)


def test_requests_match_the_official_schemas(keys):
    t = p.Teilnehmer("https://x", "HOST", "PARTNER1", "USER1")
    validate(p.unsecured_request(t, keys, "INI"))
    validate(p.unsecured_request(t, keys, "HIA"))
    validate(p.hpb_request(t, keys))
    validate(p.ini_order_data(t, keys), "ebics_signature_S002.xsd")
    validate(p.hia_order_data(t, keys))
    k = p.Schluessel(**{**keys.__dict__, "bank_x": keys.cert_x, "bank_e": keys.cert_e})
    validate(p.btd_request(t, k, p.CAMT053))
    validate(p.btd_request(t, k, p.Dienst(version="08"), dt.date(2026, 1, 1), dt.date(2026, 1, 31)))
    validate(p.transfer_request(t, k, "ABCDEF0123456789ABCDEF0123456789", 2, True))
    validate(p.receipt_request(t, k, "ABCDEF0123456789ABCDEF0123456789"))


def test_key_hash_is_the_ebics_hash(keys):
    nums = keys.x002.public_key().public_numbers()
    import hashlib
    assert p.key_hash(keys.x002.public_key()) == hashlib.sha256(f"{nums.e:x} {nums.n:x}".encode()).digest()
    assert f"{nums.e:x}" == "10001"
    assert p.normalise_hash("E5 3e:96") == "e53e96"


def test_whole_life_cycle_against_the_test_bank():
    keys = p.neue_schluessel("PARTNER1", "USER1")
    bank = Testbank(segment_size=700)
    c = _client(bank, keys)
    assert c.hev() == ["H005"]
    c.ini()
    c.hia()
    assert bank.log == ["INI", "HIA"]
    with pytest.raises(p.EbicsFehler) as err:
        c.ini()                                      # a second INI: the bank already has the key
    assert err.value.code == "091118"
    with pytest.raises(p.EbicsFehler) as err:
        c.hpb()                                      # not released before the letter arrived
    assert err.value.code == "091002"
    bank.aktiv = True
    certs = c.hpb()
    assert p.cert_hash(certs["X002"]) == p.cert_hash(bank.cert_x)
    keys.bank_x, keys.bank_e = certs["X002"], certs["E002"]
    assert c.download() is None                      # nothing waiting
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.bin", os.urandom(5000))
    bank.dateien.append(buf.getvalue())
    tid, data = c.download()
    assert data == buf.getvalue()
    assert len(bank.transaktionen[tid]["parts"]) > 1     # segmented transfer
    assert bank.transaktionen[tid]["quittung"] is None
    c.quittung(tid)
    assert bank.transaktionen[tid]["quittung"] == "0" and not bank.dateien


def test_a_wrong_signature_is_refused():
    keys = p.neue_schluessel("PARTNER1", "USER1")
    other = p.neue_schluessel("PARTNER1", "USER1")
    bank = Testbank()
    _client(bank, keys).hia()
    bank.aktiv = True
    with pytest.raises(Exception):
        _client(bank, other).hpb()


@pytest.mark.parametrize("name", ["cd1_init_response", "cdb_init_response", "upload_init_response"])
def test_digest_matches_what_real_banks_signed(name):
    """Responses signed by real EBICS servers (fixtures of the Ruby client epics, MIT): our C14N must
    give the bank's DigestValue — the test bank alone could not tell, it uses the same function."""
    import base64
    import hashlib
    from pathlib import Path

    from lxml import etree
    root = etree.parse(str(Path(__file__).parent / "fixtures" / f"{name}.xml")).getroot()
    expected = root.xpath("//*[local-name()='DigestValue']")[0].text.strip()
    got = hashlib.sha256(b"".join(p._c14n(e) for e in root.xpath("//*[@authenticate='true']"))).digest()
    assert base64.b64encode(got).decode() == expected

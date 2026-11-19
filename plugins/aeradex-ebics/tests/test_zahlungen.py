import datetime as dt

import pytest
from aeradex import api
from aeradex.book import Book, BookError
from aeradex.testing import make_book

from aeradex_ebics import ebics, zahlungen
from aeradex_ebics import protokoll as p
from testbank import Testbank

IBAN = "CH9300762011623852957"
HEUTE = dt.date(2026, 3, 10)


@pytest.fixture
def bank(monkeypatch):
    b = Testbank(host="TESTHOST")
    monkeypatch.setattr(ebics, "_POST", b)
    return b


@pytest.fixture
def book(tmp_path, bank):
    b = make_book(tmp_path, plugins=["ebics"], iban=IBAN, eroeffnung={"1020": 20000, "2800": -20000}, git=True,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern")
    api.supplier_add(Book(b.root), name="Swisscom", iban="CH5604835012345678009", konto="6510")
    api.bill_add(Book(b.root), "L0001", "59.00", datum="2026-03-05", referenz="RF18539007547034")
    api.payment_run(Book(b.root), ["E-2026-0001"], "2026-03-20")
    b = Book(b.root)
    ebics.einrichten(b, "https://bank.example/ebics", "TESTHOST", "P1", "U1", "Testbank",
                     {"x002_zertifikat": p.cert_hash(bank.cert_x).hex(), "e002_zertifikat": p.cert_hash(bank.cert_e).hex(),
                      "x002_schluessel": p.key_hash(bank.cert_x.public_key()).hex(),
                      "e002_schluessel": p.key_hash(bank.cert_e.public_key()).hex()})
    ebics.senden(b)
    bank.aktiv = True
    ebics.bankschluessel(b)
    return b


def _pain002(msg_id, status, tx=()):
    txs = "".join(f"<TxInfAndSts><OrgnlEndToEndId>{e}</OrgnlEndToEndId><TxSts>{s}</TxSts>"
                  f"<StsRsnInf><Rsn><Cd>AC01</Cd></Rsn><AddtlInf>IBAN ungültig</AddtlInf></StsRsnInf></TxInfAndSts>"
                  for e, s in tx)
    return (f'<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.002.001.10"><CstmrPmtStsRpt>'
            f"<GrpHdr><MsgId>S1</MsgId></GrpHdr><OrgnlGrpInfAndSts><OrgnlMsgId>{msg_id}</OrgnlMsgId>"
            f"<OrgnlMsgNmId>pain.001.001.09</OrgnlMsgNmId>{'<GrpSts>' + status + '</GrpSts>' if not tx else ''}"
            f"</OrgnlGrpInfAndSts><OrgnlPmtInfAndSts><OrgnlPmtInfId>P1</OrgnlPmtInfId>{txs}</OrgnlPmtInfAndSts>"
            f"</CstmrPmtStsRpt></Document>").encode()


def test_payment_run_goes_to_the_bank_once(book, bank):
    files = zahlungen.dateien(book)
    assert len(files) == 1 and files[0]["art"] == "Kreditoren" and files[0]["total"] == "59.00"
    pfad = files[0]["pfad"]
    with pytest.raises(BookError, match="Betrag"):
        zahlungen.senden(book, pfad, "58.00", heute=HEUTE)          # what the person saw must match
    with pytest.raises(BookError, match="Vergangenheit"):
        zahlungen.senden(book, pfad, "59.00", heute=dt.date(2026, 3, 21))
    sent = zahlungen.senden(book, pfad, "59.00", "delia", heute=HEUTE)
    assert sent["auftrag"] == "A001" and sent["von"] == "delia"
    assert bank.uploads[0]["data"] == (book.root / pfad).read_bytes()      # exactly the file, signed
    with pytest.raises(BookError, match="schon"):
        zahlungen.senden(book, pfad, "59.00", heute=HEUTE)
    assert len(bank.uploads) == 1
    with pytest.raises(BookError, match="nicht gefunden"):
        zahlungen.senden(book, "../../etc/passwd", "1", heute=HEUTE)
    # the bank's status report comes with the next fetch
    msg = files[0]["msg_id"]
    bank.status.append(_pain002(msg, "ACSC"))
    out = ebics.abholen(book)
    assert out["statusmeldungen"] == 1
    assert zahlungen.dateien(book)[0]["ebics"]["status"] == "ausgeführt"
    assert not bank.status


def test_rejected_payment_shows_up(book, bank):
    pfad = zahlungen.dateien(book)[0]["pfad"]
    zahlungen.senden(book, pfad, "59.00", heute=HEUTE)
    msg = zahlungen.dateien(book)[0]["msg_id"]
    bank.status.append(_pain002(msg, "", tx=[("E-2026-0001", "RJCT")]))
    ebics.abholen(book)
    z = zahlungen.dateien(book)[0]["ebics"]
    assert z["status"] == "abgelehnt" and z["abgelehnt"][0]["grund"] == "AC01 IBAN ungültig"
    from aeradex_ebics import aeradex_todos
    assert any("abgelehnt" in t.titel and t.level == "red" for t in aeradex_todos(book))


def test_a_changed_file_is_not_sent(book, bank):
    pfad = zahlungen.dateien(book)[0]["pfad"]
    path = book.root / pfad
    path.write_bytes(path.read_bytes().replace(b"59.00", b"95.00"))
    with pytest.raises(BookError, match="Betrag"):
        zahlungen.senden(book, pfad, "59.00", heute=HEUTE)
    assert not bank.uploads


def test_refused_upload_is_not_recorded(book, bank):
    pfad = zahlungen.dateien(book)[0]["pfad"]
    bank.upload_fehler = "091116"
    with pytest.raises(p.EbicsFehler):
        zahlungen.senden(book, pfad, "59.00", heute=HEUTE)
    assert zahlungen.dateien(book)[0]["ebics"] is None


def test_salary_file_must_match_its_manifest(book, bank):
    b = Book(book.root)
    api.employee_add(b, "Lea", "Muster", monatslohn=5000, iban="CH5604835012345678009",
                     strasse="Gasse", nr="2", plz="3600", ort="Thun")
    api.payroll_run(Book(book.root), "2026-03")
    api.payslip_close(Book(book.root), "2026-03", "M0001")
    api.payroll_payment_export(Book(book.root), "2026-03", "2026-03-25")
    b = Book(book.root)
    lohn = next(d for d in zahlungen.dateien(b) if d["art"] == "Lohn")
    assert not lohn.get("fehler")
    path = b.root / lohn["pfad"]
    path.write_bytes(path.read_bytes() + b"\n")
    lohn = next(d for d in zahlungen.dateien(b) if d["art"] == "Lohn")
    assert "verändert" in lohn["fehler"]
    with pytest.raises(BookError, match="verändert"):
        zahlungen.senden(b, lohn["pfad"], lohn["total"], heute=HEUTE)


def test_page_sends_with_confirmation(book, bank):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/p/ebics/ebics").text
        assert "Zahlungen an die Bank" in page
        pfad = zahlungen.dateien(book)[0]["pfad"]
        r = c.post("/p/ebics/ebics/zahlung", headers=h, data={"pfad": pfad, "total": "59.00"})
        # the run's execution date (2026-03-20) may already be past on the machine running the test
        if dt.date.today().isoformat() <= "2026-03-20":
            assert r.status_code == 204, r.text
        else:
            assert "Vergangenheit" in r.text and not bank.uploads

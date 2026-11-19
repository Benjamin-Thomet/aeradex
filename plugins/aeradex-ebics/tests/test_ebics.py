import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from aeradex import api
from aeradex.book import BookError
from aeradex.testing import assert_clean, make_book

from aeradex_ebics import ebics
from aeradex_ebics import protokoll as p
from testbank import Testbank

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from camt_sample import entry, statement  # noqa: E402

IBAN = "CH9300762011623852957"


@pytest.fixture
def bank(monkeypatch):
    b = Testbank(host="TESTHOST")
    monkeypatch.setattr(ebics, "_POST", b)
    return b


def _book(tmp_path):
    return make_book(tmp_path, plugins=["ebics"], iban=IBAN, eroeffnung={"1020": 1000, "2800": -1000}, git=True)


def _setup(book, bank, erwartet=True):
    hashes = {}
    if erwartet:
        hashes = {"x002_schluessel": p.hex_paare(p.key_hash(bank.cert_x.public_key())),
                  "e002_schluessel": p.key_hash(bank.cert_e.public_key()).hex(),
                  "x002_zertifikat": p.cert_hash(bank.cert_x).hex(), "e002_zertifikat": p.cert_hash(bank.cert_e).hex()}
    ebics.einrichten(book, "https://bank.example/ebics", "testhost", "partner1", "user1", "Testbank", hashes)


def _zip(*files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, data in enumerate(files, 1):
            z.writestr(f"camt053_{i}.xml", data)
    return buf.getvalue()


def test_from_setup_to_imported_statement(tmp_path, bank):
    book = _book(tmp_path)
    assert ebics.status(book)["schritt"] == "einrichten"
    _setup(book, bank)
    keyfile = ebics.schluessel_datei(book)
    assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600
    assert ".aeradex/lokal/" in str(keyfile.relative_to(book.root)) + "/"
    assert ebics.status(book)["schritt"] == "senden"
    ebics.senden(book)
    assert bank.log == ["INI", "HIA"]
    ebics.senden(book)                                   # nothing sent twice
    assert bank.log == ["INI", "HIA"]
    from aeradex_ebics import brief
    assert brief.pdf(book)[:4] == b"%PDF"
    with pytest.raises(p.EbicsFehler):
        ebics.bankschluessel(book)                       # bank has not released yet
    assert ebics.status(book)["schritt"] == "freischaltung"
    with pytest.raises(BookError):
        ebics.abholen(book)
    bank.aktiv = True
    assert ebics.bankschluessel(book)["schritt"] == "aktiv"    # letter hashes match → confirmed
    assert ebics.abholen(book) == {"dateien": 0, "importe": [], "fehler": []}
    camt = statement(IBAN, "1000.00", [(entry("250.00", "CRDT", "2026-03-02", party="Kunde AG", ustrd="Rechnung 7"), "250.00")],
                     "2026-03-02", "2026-03-02", "S-1")
    bank.dateien.append(_zip(camt))
    out = ebics.abholen(book)
    assert out["dateien"] == 1 and len(out["importe"]) == 1 and not out["fehler"]
    assert not bank.dateien                              # confirmed to the bank
    from aeradex import bank as core_bank
    rows = core_bank.transactions(book)
    assert [r["Betrag"] for r in rows] == ["250.00"]
    assert list((book.root / "bank" / "auszuege").rglob("*.xml"))
    # the same statement again (bank delivers it twice): nothing new
    bank.dateien.append(_zip(camt))
    ebics.abholen(book)
    assert len(core_bank.transactions(book)) == 1
    assert_clean(book)
    # keys never in git
    import subprocess
    tracked = subprocess.run(["git", "-C", str(book.root), "ls-files"], capture_output=True, text=True).stdout
    assert "schluessel" not in tracked and "ebics" not in tracked


def test_bank_keys_that_differ_from_the_letter_are_refused(tmp_path, bank):
    book = _book(tmp_path)
    ebics.einrichten(book, "https://bank.example/ebics", "TESTHOST", "PARTNER1", "USER1", "",
                     {"x002_zertifikat": "00" * 32})
    ebics.senden(book)
    bank.aktiv = True
    with pytest.raises(BookError, match="NICHT"):
        ebics.bankschluessel(book)
    assert ebics.laden_schluessel(book).bank_x is None
    assert ebics.status(book)["schritt"] == "freischaltung"


def test_without_letter_hashes_a_person_confirms(tmp_path, bank):
    book = _book(tmp_path)
    _setup(book, bank, erwartet=False)
    ebics.senden(book)
    bank.aktiv = True
    assert ebics.bankschluessel(book)["schritt"] == "pruefen"
    with pytest.raises(BookError):
        ebics.abholen(book)
    assert ebics.bestaetigen(book, "ben")["schritt"] == "aktiv"


def test_failed_import_waits_and_is_retried(tmp_path, bank):
    book = _book(tmp_path)
    _setup(book, bank)
    ebics.senden(book)
    bank.aktiv = True
    ebics.bankschluessel(book)
    other = "CH5604835012345678009"                      # an account the book does not know yet
    camt = statement(other, "0.00", [(entry("10.00", "CRDT", "2026-03-03"), "10.00")], "2026-03-03", "2026-03-03", "S-2")
    bank.dateien.append(_zip(camt))
    out = ebics.abholen(book)
    assert out["fehler"] and not bank.dateien            # fetched and confirmed, but not importable yet
    assert ebics.status(book)["offen"]
    s = book.settings.data
    s["bankkonten"] = {other: "1020"}
    import yaml
    (book.root / "aeradex.yaml").write_text(yaml.safe_dump(s, allow_unicode=True), encoding="utf-8")
    import subprocess
    subprocess.run(["git", "-C", str(book.root), "commit", "-qam", "bankkonten"], check=True)
    book.reload()
    assert len(ebics.importieren(book)["importe"]) == 1
    assert not ebics.status(book)["offen"]


def test_keys_encrypted_with_a_password(tmp_path, bank, monkeypatch):
    monkeypatch.setenv("AERADEX_EBICS_PASSWORT", "geheim-geheim")
    book = _book(tmp_path)
    _setup(book, bank)
    assert b"ENCRYPTED" in ebics.schluessel_datei(book).read_bytes()
    ebics.laden_schluessel(book)
    monkeypatch.setenv("AERADEX_EBICS_PASSWORT", "falsch")
    with pytest.raises(BookError, match="Passwort|PASSWORT"):
        ebics.laden_schluessel(book)


def test_participant_cannot_change_after_sending(tmp_path, bank):
    book = _book(tmp_path)
    _setup(book, bank)
    ebics.senden(book)
    with pytest.raises(BookError, match="schon bei der Bank"):
        ebics.einrichten(book, "https://bank.example/ebics", "TESTHOST", "PARTNER1", "ANDERER", "")


def test_page_through_all_steps(tmp_path, bank):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app
    book = _book(tmp_path)
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        page = c.get("/p/ebics/ebics")
        assert page.status_code == 200 and "Verbindung zur Bank" in page.text
        assert 'href="/p/ebics/ebics"' in c.get("/bank").text                     # tab under Bank
        r = c.post("/p/ebics/ebics/einrichten", headers=h,
                   data={"url": "https://bank.example/ebics", "host_id": "TESTHOST", "partner_id": "P1", "user_id": "U1",
                         "bank": "Testbank", "x002_zertifikat": p.hex_paare(p.cert_hash(bank.cert_x))})
        assert r.status_code == 204, r.text
        assert "Schlüssel an die Bank senden" in c.get("/p/ebics/ebics").text
        assert c.post("/p/ebics/ebics/senden", headers=h).status_code == 204
        page = c.get("/p/ebics/ebics").text
        assert "Initialisierungsbrief (PDF)" in page and "Bankschlüssel holen" in page
        pdf = c.get("/x/ebics/brief")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        r = c.post("/p/ebics/ebics/bankschluessel", headers=h)
        assert r.status_code != 204 and "091002" in r.text                       # not released yet: error box
        bank.aktiv = True
        assert c.post("/p/ebics/ebics/bankschluessel", headers=h).status_code == 204
        page = c.get("/p/ebics/ebics").text
        assert "gleich" in page and "Stimmt, bestätigen" in page                 # only one hash given → a person confirms
        assert c.post("/p/ebics/ebics/bestaetigen", headers=h).status_code == 204
        bank.dateien.append(_zip(statement(IBAN, "1000.00", [(entry("5.00", "CRDT", "2026-03-02"), "5.00")],
                                           "2026-03-02", "2026-03-02", "S-9")))
        r = c.post("/p/ebics/ebics/abholen", headers=h)
        assert r.status_code == 204, r.text
        page = c.get("/p/ebics/ebics").text
        assert "Jetzt abholen" in page and "import" in page and "Zeitraum abholen" in page
        bank.dateien.append(_zip(statement(IBAN, "1005.00", [(entry("6.00", "CRDT", "2026-03-04"), "6.00")],
                                           "2026-03-04", "2026-03-04", "S-10")))
        r = c.post("/p/ebics/ebics/zeitraum", headers=h, data={"von": "2026-01-01", "bis": "2026-03-31"})
        assert r.status_code == 204, r.text
        params = bank.btd_params[-1]
        assert params.findtext("{urn:org:ebics:H005}DateRange/{urn:org:ebics:H005}Start") == "2026-01-01"


def test_statements_before_the_first_year_are_only_archived(tmp_path, bank):
    book = _book(tmp_path)                               # first year 2026
    _setup(book, bank)
    ebics.senden(book)
    bank.aktiv = True
    ebics.bankschluessel(book)
    old = statement(IBAN, "0.00", [(entry("7.00", "CRDT", "2025-12-30"), "7.00")], "2025-12-30", "2025-12-30", "S-old")
    bank.dateien.append(_zip(old))
    out = ebics.abholen(book)
    assert out["dateien"] == 1 and not out["importe"] and not out["fehler"]
    from aeradex import bank as core_bank
    assert core_bank.transactions(book) == []
    assert not ebics.status(book)["offen"] and list((ebics.ordner(book) / "archiv").rglob("*.xml"))

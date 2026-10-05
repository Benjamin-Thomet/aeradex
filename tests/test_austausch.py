"""The .batzen file: a book goes out and comes back identical — files, history,
balances — and a tampered, foreign or hostile file is refused."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from batzen import api, austausch, check, gitlog
from batzen.book import Book, BookError
from batzen.ledger import trial_balance


@pytest.fixture
def book(tmp_path: Path) -> Book:
    root = tmp_path / "buch"
    api.init_book(root, "Test GmbH", 2026, iban="CH93 0076 2011 6238 5295 7")
    text = (root / "kontenplan.yaml").read_text()
    text = text.replace('{nr: "1020", name: Bank, klasse: aktiv}',
                        '{nr: "1020", name: Bank, klasse: aktiv, eroeffnung: 20000}')
    text = text.replace('{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv}',
                        '{nr: "2800", name: Stammkapital / Aktienkapital, klasse: passiv, eroeffnung: -20000}')
    (root / "kontenplan.yaml").write_text(text)
    gitlog.commit(root, "Eröffnung")
    receipt = root / "inbox" / "quittung.pdf"
    receipt.write_bytes(b"%PDF-1.4 Quittung")
    api.post_entry(Book(root), "2026-01-05", "6500", "1020", "45.80", "Papeterie", datei="inbox/quittung.pdf")
    api.post_entry(Book(root), "2026-02-01", "6000", "1020", "1800", "Miete")
    return Book(root)


def files_of(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in austausch.book_files(Book(root))}


def test_roundtrip_keeps_files_history_and_balances(book, tmp_path):
    (book.root / "batzen.yaml").write_text((book.root / "batzen.yaml").read_text() + "\n")   # uncommitted change
    res = austausch.export_book(book, tmp_path / "out" / "Test GmbH")
    path = Path(res["datei"])
    assert path.name == "Test GmbH.batzen" and res["historie"] and res["uncommittete_aenderungen"]
    z = zipfile.ZipFile(path)
    assert z.namelist()[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert z.read("mimetype") == austausch.MIMETYPE.encode()

    target = tmp_path / "kopie"
    out = austausch.import_book(path, target)
    assert out["ok"] and out["historie"] and out["fehlende_plugins"] == []
    assert files_of(target) == files_of(book.root)
    log = [e["nachricht"] for e in gitlog.log(target, 20)]
    assert log[0].startswith("batzen: Import aus") and any("Papeterie" in m for m in log)
    assert gitlog._git(target, "remote").stdout.strip() == ""
    assert (target / ".git" / "hooks" / "pre-commit").exists()
    copy = Book(target)
    assert trial_balance(copy, 2026) == trial_balance(Book(book.root), 2026)
    assert not [i for i in check.run(copy) if i.level == "fehler"]
    assert (target / ".batzen" / "belegnummern.yaml").exists()


def test_without_history_and_inbox(book, tmp_path):
    (book.root / "inbox" / "offen.pdf").write_bytes(b"%PDF-1.4 offen")
    path = Path(austausch.export_book(book, tmp_path / "x.batzen", with_inbox=False, with_history=False)["datei"])
    names = zipfile.ZipFile(path).namelist()
    assert "historie.bundle" not in names and "buch/inbox/offen.pdf" not in names
    out = austausch.import_book(path, tmp_path / "neu")
    assert out["ok"] and not out["historie"]
    assert [e["nachricht"] for e in gitlog.log(tmp_path / "neu", 5)] == ["batzen: Buch importiert aus x.batzen"]


def test_password(book, tmp_path):
    path = Path(austausch.export_book(book, tmp_path / "geheim.batzen", password="s3cret")["datei"])
    z = zipfile.ZipFile(path)
    assert sorted(z.namelist()) == ["manifest.json", "mimetype", "payload.enc"]
    header = json.loads(z.read("manifest.json"))
    assert header["verschluesselt"] and "firma" not in header and b"Test GmbH" not in path.read_bytes()
    with pytest.raises(BookError, match="verschlüsselt"):
        austausch.import_book(path, tmp_path / "a")
    with pytest.raises(BookError, match="Falsches Passwort"):
        austausch.import_book(path, tmp_path / "b", "falsch")
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()
    assert austausch.import_book(path, tmp_path / "c", "s3cret")["ok"]
    assert austausch.inspect(path, "s3cret")["firma"] == "Test GmbH"


def _rewrite(path: Path, change) -> Path:
    """Copy a container with `change(name, data)` applied; returning None drops the entry."""
    src = zipfile.ZipFile(path)
    entries = [(n, src.read(n)) for n in src.namelist()]
    entries = [(n, d) for n, d in ((n, change(n, d)) for n, d in entries) if d is not None]
    out = path.with_name("verändert.batzen")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in entries:
            z.writestr(n, d)
    out.write_bytes(buf.getvalue())
    return out


def test_tampered_and_hostile_files_are_refused(book, tmp_path):
    path = Path(austausch.export_book(book, tmp_path / "x.batzen")["datei"])
    tampered = _rewrite(path, lambda n, d: d.replace(b"1800.00", b"18.00") if n.startswith("buch/journal/") else d)
    with pytest.raises(BookError, match="Prüfsumme"):
        austausch.import_book(tampered, tmp_path / "t1")

    def evil(n, d):
        if n == "manifest.json":
            m = json.loads(d)
            m["dateien"]["../ausserhalb.txt"] = "0" * 64
            return json.dumps(m).encode()
        return d
    slip = _rewrite(path, evil)
    with zipfile.ZipFile(slip, "a") as z:
        z.writestr("buch/../ausserhalb.txt", b"boom")
    with pytest.raises(BookError, match="Ungültiger Pfad"):
        austausch.import_book(slip, tmp_path / "t2")
    assert not (tmp_path / "ausserhalb.txt").exists()

    newer = _rewrite(path, lambda n, d: json.dumps({**json.loads(d), "version": 99}).encode()
                     if n == "manifest.json" else d)
    with pytest.raises(BookError, match="Formatversion 99"):
        austausch.import_book(newer, tmp_path / "t3")

    plain_zip = tmp_path / "kein.batzen"
    with zipfile.ZipFile(plain_zip, "w") as z:
        z.writestr("hallo.txt", "x")
    with pytest.raises(BookError, match="mimetype"):
        austausch.import_book(plain_zip, tmp_path / "t4")

    (tmp_path / "voll").mkdir()
    (tmp_path / "voll" / "etwas.txt").write_text("x")
    with pytest.raises(BookError, match="nicht leer"):
        austausch.import_book(path, tmp_path / "voll")


def test_export_refuses_broken_book(book, tmp_path):
    month = book.root / "journal" / "2026" / "2026-02.md"
    month.write_text(month.read_text().replace("| 6000 ", "| 9999 "))
    with pytest.raises(BookError, match="Fehler"):
        austausch.export_book(Book(book.root), tmp_path / "x.batzen")


def test_cli(book, tmp_path, capsys, monkeypatch):
    from batzen.cli import main
    target = tmp_path / "cli.batzen"
    assert main(["--buch", str(book.root), "export-buch", str(target)]) == 0
    assert main(["import-buch", str(target)]) == 0
    assert '"firma": "Test GmbH"' in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))
    assert main(["--buch", str(book.root), "export-buch", str(tmp_path / "enc.batzen"), "--passwort-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))
    assert main(["import-buch", str(tmp_path / "enc.batzen"), str(tmp_path / "neu"), "--passwort-stdin"]) == 0
    assert (tmp_path / "neu" / "batzen.yaml").exists()

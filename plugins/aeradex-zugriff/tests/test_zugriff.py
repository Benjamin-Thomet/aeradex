"""Access for Claude Code: ssh keys managed in aeradex, each locked to the forced command."""
import subprocess

import pytest
from starlette.testclient import TestClient

from aeradex.book import BookError
from aeradex.testing import make_book
from aeradex_zugriff import schluessel, ssh_script

HAND = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwnTAlBMR45aTasy0pSrzyNOnyBKqIl3bZNoFwZOUDX admin-by-hand"


def _keypair(tmp_path, name):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(tmp_path / name)], check=True)
    return (tmp_path / f"{name}.pub").read_text().strip()


@pytest.fixture
def keys(tmp_path, monkeypatch):
    path = tmp_path / "ssh" / "authorized_keys"
    path.parent.mkdir()
    monkeypatch.setenv("AERADEX_ZUGRIFF_KEYS", str(path))
    monkeypatch.setenv("AERADEX_CONFIG", str(tmp_path / "cfg"))
    monkeypatch.delenv("AERADEX_ZUGRIFF_BEFEHL", raising=False)
    return path


def test_access_keys_are_locked_to_the_forced_command(tmp_path, keys):
    keys.write_text(HAND + "\n")
    anna = _keypair(tmp_path, "anna-buchhaltung")
    res = schluessel.add("anna", "Anna Muster", anna)
    line = keys.read_text().splitlines()[-1]
    assert line.startswith('restrict,command="/usr/local/sbin/aeradex-ssh anna" ssh-ed25519 ')
    assert oct(keys.stat().st_mode)[-3:] == "600"
    assert [e["name"] for e in schluessel.entries()] == ["Anna Muster"] and res["fingerprint"].startswith("SHA256:")
    assert (tmp_path / "cfg" / "zugriff.yaml").read_text() == "anna: Anna Muster\n"
    # no options smuggled in, no broken keys, no duplicates, no odd names
    for bad in ('command="/bin/sh" ' + anna, "ssh-ed25519 kaputt", "ssh-rsa " + anna.split()[1]):
        with pytest.raises(BookError):
            schluessel.add("eve", "Eve", bad)
    with pytest.raises(BookError):
        schluessel.add("anna", "Anna Muster", anna)
    with pytest.raises(BookError):
        schluessel.add("../root", "x", _keypair(tmp_path, "other"))
    # removing touches only that key; the line written by hand stays
    schluessel.remove(res["fingerprint"])
    assert keys.read_text().splitlines() == [HAND]


def test_own_forced_command_path(tmp_path, keys, monkeypatch):
    monkeypatch.setenv("AERADEX_ZUGRIFF_BEFEHL", "/opt/buch/ssh-tor")
    schluessel.add("ben", "Ben Beispiel", _keypair(tmp_path, "ben"))
    assert keys.read_text().startswith('restrict,command="/opt/buch/ssh-tor ben" ')
    assert [e["wer"] for e in schluessel.entries()] == ["ben"]
    monkeypatch.setenv("AERADEX_ZUGRIFF_BEFEHL", "/usr/local/sbin/aeradex-ssh")
    assert schluessel.entries() == []          # only lines with this installation's command count as ours


def test_ssh_script_names_the_person_and_allows_only_mcp_and_inbox(tmp_path, keys):
    """The forced command reads the display name written by the Zugriff page, for the git author."""
    schluessel.add("ben", "Ben Beispiel", _keypair(tmp_path, "ben"))
    script = tmp_path / "aeradex-ssh"
    script.write_text(ssh_script())
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "aeradex").write_text('#!/bin/sh\necho "AUTHOR=$AERADEX_AUTHOR CONFIG=$AERADEX_CONFIG ARGS=$*"\n')
    (venv / "aeradex").chmod(0o755)
    conf = tmp_path / "aeradex-ssh.conf"
    conf.write_text(f"BUCH={tmp_path / 'buch'}\nVENV={tmp_path / 'venv'}\nCONFIG={tmp_path / 'cfg'}\nDOMAIN=buch.example.ch\n")

    def run(command, who="ben", stdin=b""):
        env = {"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": command, "AERADEX_SSH_CONF": str(conf)}
        return subprocess.run(["bash", str(script), who], capture_output=True, input=stdin, env=env)

    out = run("mcp")
    assert out.returncode == 0, out.stderr
    assert b"AUTHOR=Ben Beispiel (Claude Code) <ben@buch.example.ch>" in out.stdout
    assert out.stdout.rstrip().endswith(b"mcp")
    assert run("mcp", who="x;rm").returncode == 1
    assert run("bash -i").returncode == 1
    up = run('inbox "Quittung Migros.pdf"', stdin=b"%PDF-1.4 receipt")
    assert up.returncode == 0, up.stderr
    assert up.stdout.strip() == b"inbox/Quittung_Migros.pdf"
    assert (tmp_path / "buch" / "inbox" / "Quittung_Migros.pdf").read_bytes() == b"%PDF-1.4 receipt"
    assert run("inbox ../../etc/passwd.pdf", stdin=b"x").stdout.strip() == b"inbox/etc_passwd.pdf"
    assert run("inbox script.sh", stdin=b"x").returncode == 1


def test_access_page_renders(tmp_path, keys, monkeypatch):
    from aeradex.web.app import create_app
    monkeypatch.setenv("AERADEX_ZUGRIFF_SERVER", "buch.example.ch")
    book = make_book(tmp_path, plugins=["zugriff"])
    schluessel.add("anna", "Anna Muster", _keypair(tmp_path, "anna"))
    ui = TestClient(create_app(book.root, token="tok"))
    ui.get("/?t=tok")
    page = ui.get("/p/zugriff/schluessel").text
    assert "Anna Muster" in page and "SHA256:" in page and "claude mcp add" in page and "buch.example.ch" in page

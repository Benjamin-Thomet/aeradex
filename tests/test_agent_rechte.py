"""The agent can do what aeradex can: employees from a contract (record, notes, document), the other operations
as tools, and files the person attaches to a chat message."""
from __future__ import annotations

import io
import queue
import zipfile
from pathlib import Path

import pytest

from aeradex import api, datensicherung, tools
from aeradex.book import Book
from aeradex.testing import assert_clean, make_book


def _docx(*paragraphs: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


@pytest.fixture
def book(tmp_path, monkeypatch):
    b = make_book(tmp_path)
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    return b


def test_employee_from_a_contract(book):
    res = tools.add_employee("Anna", "Muster", strasse="Seeweg", nr="2", plz="3600", ort="Thun",
                             geburtsdatum="1990-04-01", geschlecht="w", eintritt="2026-11-01", monatslohn="5200",
                             pensum="80", aufenthalt="B", qst_tarif="a0n", iban="CH93 0076 2011 6238 5295 7",
                             notizen="Funktion: Malerin EFZ\nProbezeit 3 Monate, 13. Monatslohn")
    assert res["ok"], res
    nr = res["mitarbeiter"]["nummer"]
    emp = tools.employee_details(nr)
    assert emp["qst"]["code"] == "A0N" and float(emp["pensum"]) == 80 and emp["adresse"]["ort"] == "Thun"
    assert "13. Monatslohn" in emp["notizen"] and emp["dokumente"] == []

    api.inbox_add(Book(book.root), "Arbeitsvertrag Muster.pdf", b"%PDF-1.4 vertrag")
    filed = tools.file_employee_document(nr, "inbox/arbeitsvertrag-muster.pdf")
    assert filed["ok"], filed
    assert not (book.root / "inbox" / "arbeitsvertrag-muster.pdf").exists()          # moved, not copied
    docs = tools.employee_details(nr)["dokumente"]
    assert docs == [f"personal/dokumente/{nr}-anna-muster/arbeitsvertrag-muster.pdf"]
    assert not [p for p in datensicherung.ungesichert(Book(book.root)) if p.startswith("personal/")]

    upd = tools.update_employee(nr, {"pensum": "100", "notizen": "Probezeit vorbei", "qst_tarif": ""})
    assert upd["ok"], upd
    emp = tools.employee_details(nr)
    assert float(emp["pensum"]) == 100 and emp["notizen"] == "Probezeit vorbei" and emp["qst"] is None
    assert tools.add_employee("Ben", "Test", qst_kanton="BE")["ok"] is False      # a canton needs a tariff
    assert_clean(Book(book.root))


def test_notes_placeholder_reads_as_empty(book):
    nr = api.employee_add(Book(book.root), "Ida", "Leer")["mitarbeiter"]["nummer"]
    assert tools.employee_details(nr)["notizen"] == ""


def test_new_tools_are_registered_with_valid_schemas(book):
    pytest.importorskip("anthropic")
    from anthropic import beta_tool
    names = [fn.__name__ for fn in tools.shared_for(book.root) + tools.CHAT_ONLY]
    assert len(names) == len(set(names))
    for fn in tools.SHARED + tools.CHAT_ONLY:
        beta_tool(fn).to_dict()
    for name in ("add_employee", "update_employee", "file_employee_document", "add_account", "update_settings",
                 "create_reminder", "pay_supplier_bill", "import_bank_statement", "attachment_to_inbox",
                 "book_closing_entry", "set_budget"):
        assert name in names


def test_settings_tool_cannot_change_the_agent_mode(book):
    import inspect
    assert "agent_modus" not in inspect.signature(tools.update_settings).parameters
    assert tools.update_settings(telefon="033 000 00 00")["ok"]
    assert Book(book.root).settings.get("telefon") == "033 000 00 00"


def test_bookings_stay_with_a_person_in_proposal_mode(book):
    assert tools.book_bill_draft("ENT-0001")["ok"] is False
    assert "vorschlag" in tools.amend_entry("1", "2026-01-01", "x", [])["fehler"]


def test_attachments_are_readable_and_can_go_to_the_inbox(book):
    folder = book.root / tools.ATTACHMENTS / "abc"
    folder.mkdir(parents=True)
    (folder / "vertrag.docx").write_bytes(_docx("Arbeitsvertrag", "Lohn CHF 5200 pro Monat"))
    (folder / "quittung.pdf").write_bytes(b"%PDF-1.4 x")
    text = tools.read_inbox_file(".aeradex/lokal/chat/abc/vertrag.docx")
    assert "Lohn CHF 5200" in text
    assert tools.read_inbox_file("aeradex.yaml").startswith("Fehler")
    assert tools.read_inbox_file(".aeradex/lokal/chat/../../../aeradex.yaml").startswith("Fehler")
    moved = tools.attachment_to_inbox(".aeradex/lokal/chat/abc/quittung.pdf")
    assert moved["ok"] and (book.root / "inbox" / "quittung.pdf").exists()


def test_chat_send_takes_files(book, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web import chat
    from aeradex.web.app import create_app

    prompts = []

    class Fake(chat.BaseAgent):
        def run(self, events: queue.Queue, prompt: str) -> None:
            prompts.append(prompt)

    monkeypatch.setitem(chat.AGENTS, "fake", Fake)
    monkeypatch.setattr(chat, "backend", lambda preferred=None: "fake")
    monkeypatch.setattr(chat, "credentials_status", lambda preferred=None: {"ok": True})
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf}
        r = c.post("/chat/send", data={"message": "Bitte erfassen", "page": "/lohn"},
                   files=[("dateien", ("Arbeits Vertrag.pdf", b"%PDF-1.4 a", "application/pdf"))], headers=h)
        assert r.json()["ok"], r.text
        events = app.state.ui.chat.turns[r.json()["turn"]]
        while events.get(timeout=5)["type"] != "done":
            pass
        assert ".aeradex/lokal/chat/" in prompts[0] and "arbeits-vertrag.pdf" in prompts[0]
        saved = next((book.root / tools.ATTACHMENTS).rglob("*.pdf"))
        assert saved.read_bytes() == b"%PDF-1.4 a"
        assert "📎" in c.get("/chat/verlauf").json()["items"][0]["text"]
        only_file = c.post("/chat/send", data={"message": ""}, files=[("dateien", ("a.png", b"x", "image/png"))], headers=h)
        assert only_file.json()["ok"]
        events = app.state.ui.chat.turns[only_file.json()["turn"]]
        while events.get(timeout=5)["type"] != "done":
            pass
        assert c.post("/chat/neu", headers=h).status_code == 200
        assert not list((book.root / tools.ATTACHMENTS).rglob("*.*"))
        assert c.post("/chat/send", data={"message": ""}, headers=h).json()["ok"] is False


def test_employee_form_keeps_notes_and_files_a_document(book):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from aeradex.web.app import create_app

    nr = api.employee_add(Book(book.root), "Eva", "Beispiel")["mitarbeiter"]["nummer"]
    app = create_app(book.root, token="tok")
    with TestClient(app) as c:
        c.get("/?t=tok", follow_redirects=False)
        h = {"X-CSRF": app.state.ui.csrf, "HX-Request": "true"}
        r = c.post(f"/lohn/mitarbeiter/{nr}", data={"vorname": "Eva", "nachname": "Beispiel", "lohnart": "monat",
                                                   "notizen": "Kündigungsfrist 2 Monate", "aktiv": "1"},
                   files={"dokument": ("Vertrag.pdf", b"%PDF-1.4 v", "application/pdf")}, headers=h)
        assert r.status_code == 204, r.text
        emp = api.employee_show(Book(book.root), nr)
        assert emp["notizen"] == "Kündigungsfrist 2 Monate" and emp["dokumente"][0].endswith("/vertrag.pdf")
        page = c.get(f"/lohn/mitarbeiter?edit={nr}").text
        assert "Kündigungsfrist 2 Monate" in page and "vertrag.pdf" in page
    assert_clean(Book(book.root))


def test_attachments_stay_out_of_the_exchange_file(book):
    from aeradex import austausch
    folder = book.root / tools.ATTACHMENTS / "t1"
    folder.mkdir(parents=True)
    (folder / "vertrag.pdf").write_bytes(b"%PDF-1.4 x")
    files = [p.relative_to(book.root).as_posix() for p in austausch.book_files(Book(book.root))]
    assert files and not [f for f in files if "lokal/chat" in f]


def test_mcp_reads_files_on_the_server(book):
    """Claude Code on a PC (MCP over ssh) cannot read the server's files itself: the MCP server reads them."""
    pytest.importorskip("mcp")
    import asyncio
    from aeradex import mcp_server
    (book.root / "inbox").mkdir(exist_ok=True)
    (book.root / "inbox" / "notiz.txt").write_text("Rechnung Muster AG 120.00", encoding="utf-8")
    (book.root / "inbox" / "foto.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                      "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
    server = mcp_server.build_server()

    async def call(datei):
        return (await server.call_tool("read_inbox_file", {"datei": datei})).content[0]

    assert "Muster AG 120.00" in asyncio.run(call("inbox/notiz.txt")).text
    assert asyncio.run(call("inbox/foto.png")).type == "image"
    assert asyncio.run(call("aeradex.yaml")).text.startswith("Fehler")

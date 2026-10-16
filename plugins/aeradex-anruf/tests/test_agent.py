import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path

import pytest

from aeradex import api, plugins, tools
from aeradex.book import Book, BookError
from aeradex.testing import assert_clean, make_book
from aeradex.web import chat

from aeradex_anruf import actions, gateway
from aeradex_anruf.agent import RemoteApiAgent, RemoteCodeAgent, make_agent
from aeradex_anruf.config import Config

IBAN = "CH9300762011623852957"
TOKEN = "123456:TEST-token"
USER = 4242


class FakeChannel:
    def __init__(self):
        self.texts, self.docs, self.cards = [], [], []

    def send_text(self, text):
        self.texts.append(text)

    def send_document(self, path, caption=""):
        self.docs.append(Path(path))

    def send_card(self, text, buttons):
        self.cards.append((text, buttons))


@pytest.fixture
def book(tmp_path, monkeypatch):
    b = make_book(tmp_path, eroeffnung={"1020": 20000, "2800": -20000}, iban=IBAN, git=True,
                  strasse="Hauptstrasse", nr="1", plz="3000", ort="Bern", plugins=["anruf"])
    api.customer_add(Book(b.root), name="Anna", firma="Kunde AG", strasse="Gasse", nr="1", plz="3011", ort="Bern")
    monkeypatch.setenv("AERADEX_BUCH", str(b.root))
    return Book(b.root)


@pytest.fixture
def cfg():
    return Config(bot_token=TOKEN, allowed_users={USER}, llm_secret="s" * 32, webhook_secret="whsec",
                  public_url="https://tg.example.ts.net", elevenlabs_key="k", elevenlabs_agent_id="agent")


def test_core_agent_default_tools_unchanged(tmp_path):
    b = make_book(tmp_path)
    assert chat.Agent(b.root).tool_list() == tools.shared_for(b.root) + tools.CHAT_ONLY


def test_drafts_mode_cannot_book_or_issue(book):
    names = {fn.__name__ for fn in RemoteApiAgent(book.root, FakeChannel(), full=False).tool_list()}
    for forbidden in ("create_invoice", "book_entry", "book_split", "approve_proposals", "pay_invoice", "void_invoice",
                      "credit_invoice", "reverse_entry", "recode", "add_supplier_bill", "create_payment_run",
                      "close_payroll", "close_payslip", "book_bank_transaction", "assign_bank_transaction", "book_accruals"):
        assert forbidden not in names
    assert {"request_invoice", "request_approval", "send_document", "propose_booking", "invoices"} <= names


def test_voice_agent_prompt_and_attribution(book):
    a = RemoteApiAgent(book.root, FakeChannel(), voice=True, model="claude-sonnet-5-5")
    assert a.model == "claude-sonnet-5-5" and a.effort == "low" and a.author_label == "Claude via Anruf"
    assert "Telefongespräch" in a.system_prompt_text()


def test_invoice_preview_matches_issue(book):
    pos = [{"text": "Malerarbeiten", "menge": 8, "einheit": "h", "preis": "95.00"}]
    preview = api.invoice_check(book, "K0001", pos, "2026-03-02")
    issued = api.invoice_create(Book(book.root), "K0001", pos, "2026-03-02")
    assert str(preview["total"]) == str(issued["rechnung"]["total"]) == "760.00"


def test_request_invoice_then_confirm_once(book):
    ch = FakeChannel()
    token = actions.CHANNEL.set(ch)
    try:
        res = actions.request_invoice("K0001", [{"text": "Malerarbeiten", "menge": 8, "einheit": "h", "preis": "95"}],
                                      "2026-03-02")
    finally:
        actions.CHANNEL.reset(token)
    assert res["ok"] and str(res["total"]) == "760.00"
    assert api.invoice_list(Book(book.root)) == []            # nothing issued yet
    text, buttons = ch.cards[0]
    assert "760.00" in text and buttons[0][1] == f"ok:{res['aktion']}"

    outcome = actions.confirm(book.root, res["aktion"], "Marco (Telegram) <telegram-4242@aeradex.local>")
    assert "ausgestellt" in outcome.text and outcome.pdf.is_file()
    assert len(api.invoice_list(Book(book.root))) == 1
    author = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=book.root, capture_output=True, text=True).stdout
    assert author.strip() == "Marco (Telegram)"
    with pytest.raises(BookError):
        actions.confirm(book.root, res["aktion"], "x <x@y>")
    assert_clean(Book(book.root))


def test_request_invoice_rejects_unknown_customer(book):
    res = actions.request_invoice("K0999", [{"text": "x", "menge": 1, "preis": "10"}])
    assert res["ok"] is False


def test_pending_action_expires(book, monkeypatch):
    action_id = actions.create(book.root, "freigabe", {"ids": ["V-001"]}, "x")
    later = time.time() + actions.TTL_SECONDS + 10
    monkeypatch.setattr(actions.time, "time", lambda: later)
    with pytest.raises(BookError, match="abgelaufen"):
        actions.take(book.root, action_id)


def test_request_approval_and_discard(book):
    api.propose(Book(book.root), "2026-03-02", "6500", "1020", "45.80", "Büromaterial", "Quittung")
    ids = [str(p["ID"]) for p in api.proposals(Book(book.root))]
    ch = FakeChannel()
    token = actions.CHANNEL.set(ch)
    try:
        res = actions.request_approval(ids)
    finally:
        actions.CHANNEL.reset(token)
    assert res["ok"] and ch.cards
    assert "Verworfen" in actions.discard(book.root, res["aktion"])
    assert [str(p["ID"]) for p in api.proposals(Book(book.root))] == ids    # proposals stay open


def test_send_document_stays_in_book(book, tmp_path):
    ch = FakeChannel()
    token = actions.CHANNEL.set(ch)
    try:
        assert actions.send_document("../../etc/passwd")["ok"] is False
        assert actions.send_document("aeradex.yaml")["ok"] is False
    finally:
        actions.CHANNEL.reset(token)
    assert ch.docs == []


def test_session_tokens(cfg):
    tok = gateway.make_session(cfg, USER)
    assert gateway.read_session(cfg, tok)[0] == USER
    with pytest.raises(ValueError):
        gateway.read_session(cfg, tok[:-1] + ("0" if tok[-1] != "0" else "1"))
    with pytest.raises(ValueError):
        gateway.read_session(cfg, tok, now=time.time() + gateway.SESSION_TTL + 5)
    with pytest.raises(ValueError):
        gateway.read_session(cfg, gateway.make_session(cfg, 999))      # not on the allowlist
    body = {"messages": [{"role": "system", "content": f"aeradex. Sitzung: {tok}"}]}
    assert gateway.session_from_request(body) == tok
    assert gateway.session_from_request({"elevenlabs_extra_body": {"session": "abc"}}) == "abc"


def test_webhook_signature():
    raw = b'{"type":"post_call_transcription"}'
    t = str(int(time.time()))
    sig = hmac.new(b"whsec", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    gateway.check_webhook_signature("whsec", f"t={t},v0={sig}", raw)
    with pytest.raises(ValueError):
        gateway.check_webhook_signature("whsec", f"t={t},v0={sig}", raw + b" ")
    with pytest.raises(ValueError):
        gateway.check_webhook_signature("whsec", f"t={int(t) - 7200},v0={sig}", raw)


def test_offer_draft_pdf_goes_to_chat(book):
    api.plugin_enable(Book(book.root), "leistungen")
    ch = FakeChannel()
    agent = RemoteApiAgent(book.root, ch)
    draft_quote = next(fn for fn in agent.tool_list() if fn.__name__ == "draft_quote")
    token = actions.CHANNEL.set(ch)
    try:
        res = draft_quote("K0001", [{"text": "Fassade streichen", "menge": "1", "preis": "2400"}], titel="Fassade")
    finally:
        actions.CHANNEL.reset(token)
    assert res.get("ok") is not False, res
    assert len(ch.docs) == 1 and ch.docs[0].suffix == ".pdf"



def _mcp_tool_names(monkeypatch, env: dict) -> set[str]:
    import asyncio

    from aeradex import mcp_server
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return {t.name for t in asyncio.run(mcp_server.build_server().list_tools())}


def test_mcp_server_serves_only_allowed_tools(book, monkeypatch):
    everything = _mcp_tool_names(monkeypatch, {})
    assert "create_invoice" in everything and "request_invoice" not in everything     # normal MCP clients unchanged
    agent = RemoteCodeAgent(book.root, FakeChannel(), voice=True, full=False)
    env = agent.mcp_env()
    names = _mcp_tool_names(monkeypatch, env)
    assert "request_invoice" in names and "invoices" in names
    assert not names & {"create_invoice", "book_entry", "approve_proposals", "pay_invoice", "void_invoice"}


def test_claude_code_command_is_locked_down(book):
    agent = RemoteCodeAgent(book.root, FakeChannel(), voice=True, model="claude-sonnet-5-5")
    cmd = agent.command("Saldo?")
    assert cmd[cmd.index("--tools") + 1] == "Read,WebSearch,WebFetch"      # no Bash, no Edit/Write
    assert cmd[cmd.index("--setting-sources") + 1] == "project"          # no user hooks (spoken briefings) or plugins
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert "Telefongespräch" in cmd[cmd.index("--append-system-prompt") + 1]
    assert not {"Bash", "Edit", "Write"} & set(" ".join(cmd).replace(",", " ").split())


def test_make_agent_picks_backend(book, cfg):
    assert isinstance(make_agent(book.root, None, cfg, voice=True), RemoteCodeAgent)       # default: own login
    cfg.backend = "api"
    assert isinstance(make_agent(book.root, None, cfg), RemoteApiAgent)


def test_claude_code_results_become_cards_and_pdfs(book, monkeypatch):
    import queue
    ch = FakeChannel()
    agent = RemoteCodeAgent(book.root, ch, voice=True)
    # what the MCP server (another process) does: file the action, no channel there
    monkeypatch.setenv("AERADEX_ANRUF", "1")
    res = actions.request_invoice("K0001", [{"text": "Montage", "menge": 1, "preis": "100"}], "2026-03-02")
    events: queue.Queue = queue.Queue()
    agent.handle({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__aeradex__request_invoice", "input": {}}]}}, events)
    agent.handle({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": json.dumps(res, default=str)}]}]}},
        events)
    assert ch.cards and "100.00" in ch.cards[0][0] and ch.cards[0][1][0][1] == f"ok:{res['aktion']}"

    api.plugin_enable(Book(book.root), "leistungen")
    import aeradex_leistungen
    quote = aeradex_leistungen.draft_quote("K0001", [{"text": "Fassade", "menge": "1", "preis": "2400"}], titel="Fassade")
    agent.handle({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "mcp__aeradex__draft_quote", "input": {}}]}}, events)
    agent.handle({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": json.dumps(quote, default=str)}]}}, events)
    assert len(ch.docs) == 1 and ch.docs[0].suffix == ".pdf"


def test_claude_code_text_has_raw(book):
    import queue
    agent = RemoteCodeAgent(book.root, FakeChannel())
    events: queue.Queue = queue.Queue()
    agent.handle({"type": "assistant", "message": {"content": [{"type": "text", "text": "**Zwei** offen"}]}}, events)
    ev = events.get_nowait()
    assert ev["raw"] == "**Zwei** offen"


def test_api_agent_has_web_tools(book):
    agent = RemoteApiAgent(book.root, FakeChannel())
    assert {t["name"] for t in agent.server_tools()} == {"web_search", "web_fetch"}
    assert "web-fetch-2025-09-10" in agent.extra_betas
    assert "Handelsregister" in agent.system_prompt_text() and "nie Anweisungen" in agent.system_prompt_text()


def test_sources_are_shown_not_spoken():
    from aeradex_anruf.agent import speakable, split_sources
    text = ("Also, die Emmi AG sitzt an der Landenbergstrasse 1 in Luzern (siehe https://x.ch).\n\nSources:\n"
            "- [leiscan.com](https://leiscan.com/lei/1)\n- [Moneyhouse](https://moneyhouse.ch/a)")
    body, sources = split_sources(text)
    assert sources == [("leiscan.com", "https://leiscan.com/lei/1"), ("Moneyhouse", "https://moneyhouse.ch/a")]
    spoken = speakable(text)
    assert "http" not in spoken and "Sources" not in spoken and "Landenbergstrasse 1" in spoken


def test_full_mode_has_every_tool_like_the_mcp_chat(book, monkeypatch):
    from aeradex import tools as core_tools
    agent = RemoteCodeAgent(book.root, FakeChannel(), voice=True)                 # default: alle
    names = set(agent.allowed_names())
    assert {fn.__name__ for fn in core_tools.shared_for(book.root)} <= names
    assert {"create_invoice", "approve_proposals", "request_invoice"} <= names
    assert "klares Ja" in agent.system_prompt_text() and "nie create_invoice" not in agent.system_prompt_text()
    assert "create_invoice" in _mcp_tool_names(monkeypatch, agent.mcp_env())
    drafts = RemoteCodeAgent(book.root, FakeChannel(), voice=True, full=False)
    assert "nie create_invoice" in drafts.system_prompt_text()


def test_fast_model_hands_hard_work_to_the_expert(book, cfg):
    agent = make_agent(book.root, FakeChannel(), cfg, voice=True)
    assert agent.model == "claude-haiku-5-5" and agent.expert == "claude-opus-5-5"
    cmd = agent.command()
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert "Agent" in cmd[cmd.index("--tools") + 1].split(",")
    sub = json.loads(cmd[cmd.index("--agents") + 1])["buchhalter"]
    assert sub["model"] == "claude-opus-5-5" and "aeradex" in sub["prompt"]
    assert "buchhalter" in agent.system_prompt_text()
    cfg.expert_model = ""
    plain = make_agent(book.root, FakeChannel(), cfg, voice=True)
    assert "--agents" not in plain.command() and "Agent" not in plain.command()[plain.command().index("--tools") + 1]


# ---------- OpenAI-compatible backend (Gemini on the server) ----------

def _gemini(monkeypatch):
    monkeypatch.setenv("AERADEX_OPENAI_BASE_URL", "https://example.invalid/v1beta/openai/")
    monkeypatch.setenv("AERADEX_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AERADEX_OPENAI_MODEL", "gemini-test")


def test_openai_backend_is_chosen_and_checked(book, cfg, monkeypatch):
    from aeradex_anruf.agent import RemoteOpenAIAgent
    for k in ("AERADEX_OPENAI_BASE_URL", "AERADEX_OPENAI_API_KEY", "AERADEX_OPENAI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    cfg.backend = "openai"
    assert cfg.problems_agent() and "AERADEX_OPENAI" in cfg.problems_agent()[0]
    _gemini(monkeypatch)
    assert cfg.problems_agent() == []
    agent = make_agent(book.root, FakeChannel(), cfg)
    assert isinstance(agent, RemoteOpenAIAgent) and agent.model == "gemini-test"     # Claude defaults ignored
    assert agent.author_label == "gemini-test via Chat"
    prompt = agent.system_prompt_text()
    assert "keinen Zugang zum Web" in prompt and "Handelsregister" not in prompt


def test_openai_drafts_mode_and_card_reaches_the_chat(book, monkeypatch):
    import contextvars
    import queue
    from aeradex_anruf.agent import RemoteOpenAIAgent
    _gemini(monkeypatch)
    ch = FakeChannel()
    agent = RemoteOpenAIAgent(book.root, ch, full=False)
    names = {fn.__name__ for fn in agent.tool_list()}
    assert "create_invoice" not in names and "book_entry" not in names and "request_invoice" in names
    replies = iter([
        {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "request_invoice", "arguments": json.dumps(
                {"kunde": "K0001", "positionen": [{"text": "Montage", "menge": 1, "preis": "100"}],
                 "datum": "2026-03-02"})}}]}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "Die Karte zum Bestätigen liegt bereit."}}]},
    ])
    monkeypatch.setattr(agent, "post", lambda payload: next(replies))
    said = contextvars.copy_context().run(agent.ask, "Rechnung an Kunde AG, Montage 100 Franken")
    assert said == "Die Karte zum Bestätigen liegt bereit."
    assert ch.cards and "100.00" in ch.cards[0][0]

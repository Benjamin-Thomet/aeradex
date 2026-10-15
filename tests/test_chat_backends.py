"""Event parsing of the chat backends (no model calls)."""
from __future__ import annotations

import contextvars
import queue
from pathlib import Path

import pytest

pytest.importorskip("starlette")
from aeradex.web import chat  # noqa: E402


def drain(q: queue.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get())
    return out


def test_opencode_events(tmp_path: Path):
    agent = chat.OpencodeAgent(tmp_path)
    q = queue.Queue()
    events = [  # recorded from `opencode run --format json` (opencode 1.18)
        {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
        {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "aeradex_status",
                                                           "state": {"status": "completed", "input": {}, "output": "{}"}}},
        {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "Flüssige Mittel: **16'616.54 CHF**"}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish", "reason": "stop"}},
    ]
    results = [agent.handle(e, q) for e in events]
    out = drain(q)
    assert agent.session_id == "ses_1"
    assert out[0] == {"type": "tool", "text": "status()"}
    assert "<strong>16&#x27;616.54 CHF</strong>" in out[1]["html"]
    assert results[-1] == {"error": ""}
    assert "--session" in agent.command("weiter") and "ses_1" in agent.command("weiter")
    cfg = (agent.tmp / "opencode.json").read_text()
    assert '"edit": "deny"' in cfg and '"bash": "deny"' in cfg and "AERADEX_AUTHOR" in cfg


def test_codex_events(tmp_path: Path):
    agent = chat.CodexAgent(tmp_path)
    q = queue.Queue()
    events = [  # `codex exec --json` event stream
        {"type": "thread.started", "thread_id": "th_9"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "i1", "type": "mcp_tool_call", "server": "aeradex", "tool": "accounts",
                                          "arguments": {"suche": "büro"}, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "mcp_tool_call", "tool": "accounts", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Vorschlag V-002 erstellt."}},
        {"type": "turn.completed", "usage": {}},
    ]
    results = [agent.handle(e, q) for e in events]
    out = drain(q)
    assert agent.session_id == "th_9"
    assert out[0]["text"] == "accounts(suche='büro')" and out[1]["type"] == "text"
    assert results[-1] == {"error": ""}
    cmd = agent.command("weiter")
    assert cmd[cmd.index("--sandbox") + 1] == "read-only" and cmd[-3:] == ["resume", "th_9", "weiter"]
    failed = agent.handle({"type": "turn.failed", "error": {"message": "401 Unauthorized"}}, q)
    assert failed == {"error": "401 Unauthorized"}


def test_first_prompt_carries_the_rules_once(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Regel X")
    agent = chat.OpencodeAgent(tmp_path)
    assert "Regel X" in agent.first_prompt("Hallo")
    agent.session_id = "s"
    assert agent.first_prompt("Hallo") == "Hallo"


def test_backend_choice(monkeypatch):
    monkeypatch.setattr(chat, "available", lambda: {"claude-code": False, "codex": False, "opencode": True, "api": False})
    monkeypatch.delenv("AERADEX_CHAT_BACKEND", raising=False)
    assert chat.backend("auto") == "opencode"
    assert chat.backend("codex") == "codex"
    assert chat.credentials_status("codex")["ok"] is False


# ---------- OpenAI-compatible endpoint (Gemini, Mistral …) ----------

def _openai_env(monkeypatch):
    monkeypatch.setenv("AERADEX_OPENAI_BASE_URL", "https://example.invalid/v1beta/openai/")
    monkeypatch.setenv("AERADEX_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AERADEX_OPENAI_MODEL", "gemini-test")


def test_openai_backend_needs_all_three_settings(monkeypatch):
    for k in ("AERADEX_OPENAI_BASE_URL", "AERADEX_OPENAI_API_KEY", "AERADEX_OPENAI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert chat.openai_settings() is None and chat.available()["openai"] is False
    _openai_env(monkeypatch)
    assert chat.openai_settings() == {"base": "https://example.invalid/v1beta/openai", "key": "test-key",
                                      "model": "gemini-test"}
    assert chat.available()["openai"] is True


def test_openai_agent_runs_tools_and_answers(tmp_path: Path, monkeypatch):
    from aeradex.testing import make_book
    _openai_env(monkeypatch)
    book = make_book(tmp_path, firma="Muster GmbH")
    agent = chat.OpenAIAgent(book.root)
    specs = {s["function"]["name"]: s for s in agent.tool_specs()}
    assert "status" in specs and specs["book_entry"]["function"]["parameters"]["type"] == "object"
    sent = []
    replies = iter([
        {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"id": "", "type": "function", "function": {"name": "status", "arguments": "{}"}},
            {"id": "x2", "type": "function", "function": {"name": "gibt_es_nicht", "arguments": "{}"}}]}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "Die **Muster GmbH** ist bereit."}}]},
    ])
    monkeypatch.setattr(agent, "post", lambda payload: (sent.append(payload), next(replies))[1])
    q: queue.Queue = queue.Queue()
    # like in the app: the run sets the book and the git author in its own context, not the test's
    contextvars.copy_context().run(agent.run, q, "Wie steht das Buch?")
    events = drain(q)
    assert [e["type"] for e in events] == ["tool", "tool", "tool_error", "text"]
    assert events[-1]["raw"] == "Die **Muster GmbH** ist bereit." and "<strong>" in events[-1]["html"]
    tool_msgs = [m for m in sent[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and "Muster GmbH" in tool_msgs[0]["content"]       # status really ran
    assert all(m["tool_call_id"] for m in tool_msgs)                                # empty ids are filled in
    assert sent[0]["messages"][0]["role"] == "system" and sent[0]["model"] == "gemini-test"


def test_openai_agent_forgets_a_failed_turn(tmp_path: Path, monkeypatch):
    from aeradex.testing import make_book
    _openai_env(monkeypatch)
    agent = chat.OpenAIAgent(make_book(tmp_path).root)

    def boom(payload):
        raise RuntimeError("gemini-test: HTTP 500")
    monkeypatch.setattr(agent, "post", boom)
    with pytest.raises(RuntimeError):
        contextvars.copy_context().run(agent.run, queue.Queue(), "Hallo")
    assert [m["role"] for m in agent.messages] == ["system"]


def test_openai_attachment_from_inbox_file():
    parts = chat.OpenAIAgent._attachment([
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "text", "text": "Inhalt von inbox/q.png"}])
    assert parts[0]["image_url"]["url"] == "data:image/png;base64,AAAA" and parts[1]["text"].startswith("Inhalt")


def test_tools_name_written_files_relative_to_the_book(tmp_path: Path, monkeypatch):
    """An agent on another machine (MCP over ssh) gets paths it can look up in the web UI (/datei/<path>)."""
    from aeradex import tools
    from aeradex.testing import make_book
    book = make_book(tmp_path, firma="Muster GmbH")
    monkeypatch.setenv("AERADEX_BUCH", str(book.root))
    res = tools.closing_documents(teile=["kontenplan"], formate=["pdf"])
    assert res["ok"] and res["datei"].startswith("berichte/") and (book.root / res["datei"]).exists()

"""Event parsing of the chat backends (no model calls)."""
from __future__ import annotations

import queue
from pathlib import Path

import pytest

pytest.importorskip("starlette")
from batzen.web import chat  # noqa: E402


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
        {"type": "tool_use", "sessionID": "ses_1", "part": {"type": "tool", "tool": "batzen_status",
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
    assert '"edit": "deny"' in cfg and '"bash": "deny"' in cfg and "BATZEN_AUTHOR" in cfg


def test_codex_events(tmp_path: Path):
    agent = chat.CodexAgent(tmp_path)
    q = queue.Queue()
    events = [  # `codex exec --json` event stream
        {"type": "thread.started", "thread_id": "th_9"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "i1", "type": "mcp_tool_call", "server": "batzen", "tool": "accounts",
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
    monkeypatch.delenv("BATZEN_CHAT_BACKEND", raising=False)
    assert chat.backend("auto") == "opencode"
    assert chat.backend("codex") == "codex"
    assert chat.credentials_status("codex")["ok"] is False

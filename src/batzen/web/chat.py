"""The agent drawer: Claude with the batzen tools, inside the UI.

Uses the Anthropic SDK's tool runner over the same tool registry as the MCP
server (`batzen.tools`). Each turn runs in a worker thread; its progress
(tool calls, text, errors) is streamed to the browser over SSE. Commits the
agent makes are attributed to it in git, and in `agent_modus: vorschlag` it
can only propose bookings, which then appear in Prüfen for a person to approve.
"""
from __future__ import annotations

import asyncio
import functools
import html
import json
import os
import queue
import re
import threading
import uuid
from datetime import date
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from .. import gitlog, tools

MODEL = os.environ.get("BATZEN_MODEL", "claude-opus-5-5")
MAX_ITERATIONS = 16

PAGE_LABELS = {"/": "Übersicht", "/pruefen": "Prüfen", "/journal": "Journal", "/konten": "Konten",
               "/debitoren": "Debitoren", "/lohn": "Lohn", "/abschluss": "Abschluss", "/verlauf": "Verlauf",
               "/einstellungen": "Einstellungen"}


def credentials_status() -> dict:
    """Whether the SDK will find credentials, without calling the API."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return {"ok": False, "quelle": None, "hinweis": "Das Paket anthropic fehlt: pip install 'batzen[ui]'"}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": True, "quelle": "ANTHROPIC_API_KEY", "hinweis": ""}
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {"ok": True, "quelle": "ANTHROPIC_AUTH_TOKEN", "hinweis": ""}
    if (Path.home() / ".config" / "anthropic").exists():
        return {"ok": True, "quelle": "ant-Profil (~/.config/anthropic)", "hinweis": ""}
    return {"ok": False, "quelle": None,
            "hinweis": "Kein API-Zugang gefunden. Im Terminal `ant auth login` ausführen oder "
                       "ANTHROPIC_API_KEY setzen und `batzen ui` neu starten."}


def page_label(path: str) -> str:
    for prefix in sorted(PAGE_LABELS, key=len, reverse=True):
        if path == prefix or (prefix != "/" and path.startswith(prefix)):
            return PAGE_LABELS[prefix]
    return "Übersicht"


# ---------- markdown-lite for answers ----------

def render_text(text: str) -> str:
    """Escape, then allow **bold**, `code`, bullet lists and paragraphs."""
    out, items = [], []

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for block in re.split(r"\n\s*\n", text.strip()):
        for line in block.splitlines():
            if re.match(r"^\s*[-*•]\s+", line):
                items.append(re.sub(r"^\s*[-*•]\s+", "", line))
            else:
                flush()
                if line.strip():
                    out.append(f"<p>{inline(line)}</p>")
        flush()
    return "".join(out)


def describe_call(name: str, args: dict) -> str:
    shown = ", ".join(f"{k}={v!r}" if not isinstance(v, (list, dict)) else f"{k}=…" for k, v in args.items() if v not in ("", None))
    return f"{name}({shown})"[:160]


# ---------- the agent ----------

class Agent:
    """One running conversation for this UI session (single user, local)."""

    def __init__(self, root: Path):
        self.root = root
        self.messages: list[dict] = []
        self.turns: dict[str, queue.Queue] = {}
        self.busy = False
        self.lock = threading.Lock()

    def system_prompt(self) -> list[dict]:
        book_rules = ""
        agents_md = self.root / "AGENTS.md"
        if agents_md.exists():
            book_rules = agents_md.read_text(encoding="utf-8")
        text = (tools.INSTRUCTIONS + "\n\nDu arbeitest in der batzen-Oberfläche; der Mensch sieht deine "
                "Vorschläge sofort unter «Prüfen». Antworte knapp auf Deutsch (Schweizer Schreibweise, kein ß), "
                "nenne Belegnummern und Beträge, die die Tools zurückgeben.\n\n" + book_rules)
        return [{"type": "text", "text": text}]

    def tool_functions(self, events: queue.Queue):
        from anthropic import beta_tool

        wrapped = []
        for fn in tools.SHARED + tools.CHAT_ONLY:
            @functools.wraps(fn)
            def call(*args, __fn=fn, **kwargs):
                events.put({"type": "tool", "text": describe_call(__fn.__name__, kwargs)})
                result = __fn(*args, **kwargs)
                if isinstance(result, (list, str)) and __fn is tools.read_inbox_file:
                    return result
                if isinstance(result, dict) and result.get("ok") is False:
                    events.put({"type": "tool_error", "text": result.get("fehler", "")[:300]})
                return json.dumps(result, ensure_ascii=False, default=str)
            wrapped.append(beta_tool(call))
        return wrapped

    def start(self, message: str, page: str) -> str:
        with self.lock:
            if self.busy:
                raise RuntimeError("Der Agent arbeitet noch an der letzten Anfrage.")
            self.busy = True
        turn = uuid.uuid4().hex[:12]
        events: queue.Queue = queue.Queue()
        self.turns[turn] = events
        context = f"[Heute: {date.today().isoformat()} · Seite: {page_label(page)}]\n"
        self.messages.append({"role": "user", "content": context + message})
        threading.Thread(target=self._run, args=(events,), daemon=True).start()
        return turn

    def _run(self, events: queue.Queue) -> None:
        tools.BOOK_ROOT.set(self.root)
        gitlog.AUTHOR.set(gitlog.AGENT_AUTHOR)
        try:
            import anthropic
            client = anthropic.Anthropic()
            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=16000,
                max_iterations=MAX_ITERATIONS,
                system=self.system_prompt(),
                tools=self.tool_functions(events),
                messages=list(self.messages),
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            for message in runner:
                # Mirror the history unchanged (append-only keeps thinking blocks valid).
                self.messages.append({"role": "assistant", "content": message.content})
                for block in message.content:
                    if block.type == "text" and block.text.strip():
                        events.put({"type": "text", "html": render_text(block.text)})
                if message.stop_reason == "refusal":
                    events.put({"type": "error", "text": "Claude hat diese Anfrage abgelehnt."})
                    break
                if message.stop_reason == "max_tokens":
                    events.put({"type": "error", "text": "Die Antwort war zu lang und wurde abgeschnitten."})
                    break
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    self.messages.append(tool_response)
        except Exception as exc:  # surfaced to the user, never swallowed
            name = type(exc).__name__
            hint = ""
            if "auth" in name.lower() or "credential" in str(exc).lower() or "api_key" in str(exc).lower():
                hint = " — API-Zugang prüfen (Einstellungen)."
            events.put({"type": "error", "text": f"{name}: {str(exc)[:300]}{hint}"})
            # Drop a dangling user turn so the next request starts clean.
            while self.messages and self.messages[-1]["role"] != "assistant":
                self.messages.pop()
        finally:
            with self.lock:
                self.busy = False
            events.put({"type": "done"})

    def reset(self) -> None:
        with self.lock:
            if not self.busy:
                self.messages.clear()


def agent(ui) -> Agent:
    if ui.chat is None:
        ui.chat = Agent(ui.root)
    return ui.chat


def routes(ui) -> list[Route]:
    async def send(request: Request):
        form = await request.form()
        message = (form.get("message") or "").strip()
        if not message:
            return JSONResponse({"ok": False, "fehler": "Leere Nachricht"}, status_code=400)
        cred = credentials_status()
        if not cred["ok"]:
            return JSONResponse({"ok": False, "fehler": cred["hinweis"]}, status_code=400)
        try:
            turn = agent(ui).start(message, form.get("page") or "/")
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "fehler": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, "turn": turn})

    async def stream(request: Request):
        events = agent(ui).turns.get(request.path_params["turn"])
        if events is None:
            return JSONResponse({"ok": False}, status_code=404)

        async def gen():
            while True:
                try:
                    event = await asyncio.to_thread(events.get, True, 1.0)
                except queue.Empty:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] == "done":
                    agent(ui).turns.pop(request.path_params["turn"], None)
                    break
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def reset(request: Request):
        agent(ui).reset()
        return HTMLResponse("")

    async def history(request: Request):
        """The visible transcript, so the drawer survives page loads."""
        out = []
        for m in agent(ui).messages:
            content = m["content"]
            if m["role"] == "user" and isinstance(content, str):
                out.append({"type": "me", "text": content.split("]\n", 1)[-1]})
            elif m["role"] == "assistant":
                for block in content:
                    if getattr(block, "type", None) == "text" and block.text.strip():
                        out.append({"type": "text", "html": render_text(block.text)})
                    elif getattr(block, "type", None) == "tool_use":
                        out.append({"type": "tool", "text": describe_call(block.name, dict(block.input or {}))})
        return JSONResponse({"items": out, "busy": agent(ui).busy})

    return [
        Route("/chat/send", send, methods=["POST"]),
        Route("/chat/stream/{turn:str}", stream),
        Route("/chat/neu", reset, methods=["POST"]),
        Route("/chat/verlauf", history),
    ]

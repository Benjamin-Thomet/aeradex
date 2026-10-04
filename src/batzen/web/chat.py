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
import shutil
import subprocess
import sys
import tempfile
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


def _api_credentials() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN"
    if (Path.home() / ".config" / "anthropic").exists():
        return "ant-Profil (~/.config/anthropic)"
    return None


BACKENDS = {"claude-code": "Claude Code", "codex": "Codex", "opencode": "opencode", "api": "Claude API"}


def _codex_logged_in() -> bool:
    try:
        r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "not logged in" not in (r.stdout + r.stderr).lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def available() -> dict[str, bool]:
    """Which agent backends can run on this machine."""
    api = False
    if _api_credentials():
        try:
            import anthropic  # noqa: F401
            api = True
        except ImportError:
            pass
    return {"claude-code": bool(shutil.which("claude")),
            "codex": bool(shutil.which("codex")) and _codex_logged_in(),
            "opencode": bool(shutil.which("opencode")),
            "api": api}


def backend(preferred: str | None = None) -> str | None:
    """The drawer's agent: BATZEN_CHAT_BACKEND, else the book's `agent_backend` setting,
    else the first available of Claude API, Claude Code, Codex, opencode."""
    choice = os.environ.get("BATZEN_CHAT_BACKEND") or preferred or "auto"
    if choice in BACKENDS:
        return choice
    avail = available()
    for name in ("api", "claude-code", "codex", "opencode"):
        if avail[name]:
            return name
    return None


def credentials_status(preferred: str | None = None) -> dict:
    """Whether the drawer can run, and on what — without calling a model."""
    kind = backend(preferred)
    avail = available()
    if kind is None:
        return {"ok": False, "quelle": None, "verfuegbar": avail,
                "hinweis": "Kein Agent verfügbar. Claude Code, Codex oder opencode installieren und anmelden, "
                           "oder ANTHROPIC_API_KEY setzen, dann `batzen ui` neu starten."}
    if not avail.get(kind):
        return {"ok": False, "quelle": BACKENDS[kind], "verfuegbar": avail,
                "hinweis": f"{BACKENDS[kind]} ist gewählt, aber nicht bereit (installiert und angemeldet?)."
                           + (" Für Codex: `codex login`." if kind == "codex" else "")}
    label = {"api": f"Claude API ({_api_credentials()})", "claude-code": "Claude Code (dein Login)",
             "codex": "Codex (dein Login)", "opencode": "opencode (deine Anbieter)"}[kind]
    return {"ok": True, "quelle": label, "verfuegbar": avail, "hinweis": ""}


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

class BaseAgent:
    """One running conversation for this UI session (single user, local)."""

    def __init__(self, root: Path):
        self.root = root
        self.turns: dict[str, queue.Queue] = {}
        self.items: list[dict] = []          # visible transcript, restored on page load
        self.busy = False
        self.lock = threading.Lock()

    def emit(self, events: queue.Queue, event: dict) -> None:
        if event["type"] in ("text", "tool", "tool_error", "error", "me"):
            self.items.append(event)
        events.put(event)

    def start(self, message: str, page: str) -> str:
        with self.lock:
            if self.busy:
                raise RuntimeError("Der Agent arbeitet noch an der letzten Anfrage.")
            self.busy = True
        turn = uuid.uuid4().hex[:12]
        events: queue.Queue = queue.Queue()
        self.turns[turn] = events
        self.items.append({"type": "me", "text": message})
        context = f"[Heute: {date.today().isoformat()} · Seite: {page_label(page)}]\n"
        threading.Thread(target=self._guarded, args=(events, context + message), daemon=True).start()
        return turn

    def _guarded(self, events: queue.Queue, prompt: str) -> None:
        try:
            self.run(events, prompt)
        except Exception as exc:  # surfaced to the user, never swallowed
            self.emit(events, {"type": "error", "text": f"{type(exc).__name__}: {str(exc)[:300]}"})
        finally:
            with self.lock:
                self.busy = False
            events.put({"type": "done"})

    def run(self, events: queue.Queue, prompt: str) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        with self.lock:
            if not self.busy:
                self.items.clear()
                self.forget()

    def forget(self) -> None:
        pass

    def system_prompt_text(self) -> str:
        book_rules = ""
        agents_md = self.root / "AGENTS.md"
        if agents_md.exists():
            book_rules = agents_md.read_text(encoding="utf-8")
        return (tools.INSTRUCTIONS + "\n\nDu arbeitest in der batzen-Oberfläche; der Mensch sieht deine "
                "Vorschläge sofort unter «Prüfen». Antworte knapp auf Deutsch (Schweizer Schreibweise, kein ß), "
                "nenne Belegnummern und Beträge, die die Tools zurückgeben.\n\n" + book_rules)


class Agent(BaseAgent):
    """Claude via the Anthropic API (SDK tool runner over batzen.tools)."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.messages: list[dict] = []

    def forget(self) -> None:
        self.messages.clear()

    def system_prompt(self) -> list[dict]:
        return [{"type": "text", "text": self.system_prompt_text()}]

    def tool_functions(self, events: queue.Queue):
        from anthropic import beta_tool

        wrapped = []
        for fn in tools.SHARED + tools.CHAT_ONLY:
            @functools.wraps(fn)
            def call(*args, __fn=fn, **kwargs):
                self.emit(events, {"type": "tool", "text": describe_call(__fn.__name__, kwargs)})
                result = __fn(*args, **kwargs)
                if isinstance(result, (list, str)) and __fn is tools.read_inbox_file:
                    return result
                if isinstance(result, dict) and result.get("ok") is False:
                    self.emit(events, {"type": "tool_error", "text": result.get("fehler", "")[:300]})
                return json.dumps(result, ensure_ascii=False, default=str)
            wrapped.append(beta_tool(call))
        return wrapped

    def run(self, events: queue.Queue, prompt: str) -> None:
        tools.BOOK_ROOT.set(self.root)
        gitlog.AUTHOR.set(gitlog.agent_author("Claude API"))
        import anthropic
        self.messages.append({"role": "user", "content": prompt})
        try:
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
                        self.emit(events, {"type": "text", "html": render_text(block.text)})
                if message.stop_reason == "refusal":
                    self.emit(events, {"type": "error", "text": "Claude hat diese Anfrage abgelehnt."})
                    break
                if message.stop_reason == "max_tokens":
                    self.emit(events, {"type": "error", "text": "Die Antwort war zu lang und wurde abgeschnitten."})
                    break
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    self.messages.append(tool_response)
        except Exception:
            # Drop a dangling user turn so the next request starts clean.
            while self.messages and self.messages[-1]["role"] != "assistant":
                self.messages.pop()
            raise


class CLIAgent(BaseAgent):
    """An agent that runs as a local CLI in headless mode, with the batzen MCP
    server as its tools and the person's own login. It can read files in the
    book, but change the book only through batzen."""

    label = "Agent"

    def __init__(self, root: Path):
        super().__init__(root)
        self.session_id: str | None = None
        self.tmp = Path(tempfile.mkdtemp(prefix="batzen-chat-"))

    def mcp_command(self) -> list[str]:
        return [sys.executable, "-m", "batzen", "--buch", str(self.root), "mcp"]

    def mcp_env(self) -> dict:
        """Passed to the MCP server so its commits name the agent that made them."""
        return {"BATZEN_AUTHOR": gitlog.agent_author(self.label)}

    def forget(self) -> None:
        self.session_id = None

    def first_prompt(self, prompt: str) -> str:
        """CLIs without a system-prompt flag get the rules with the first message."""
        if self.session_id:
            return prompt
        return ("Regeln für diese Sitzung (batzen-Buchhaltung):\n" + self.system_prompt_text()
                + "\n\n---\n\n" + prompt)

    def command(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict:
        return dict(os.environ)

    def handle(self, event: dict, events: queue.Queue) -> dict | None:
        """Translate one JSON line; return it when it is the final result."""
        raise NotImplementedError

    def run(self, events: queue.Queue, prompt: str) -> None:
        proc = subprocess.Popen(self.command(prompt), cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=self.env())
        final = None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            result = self.handle(event, events)
            if result is not None:
                final = result
        proc.wait()
        if final is None and proc.returncode != 0:
            err = (proc.stderr.read() or "").strip()
            raise RuntimeError(f"{self.label} hat abgebrochen (Exit {proc.returncode}). {err[-300:]}")
        if final and final.get("error"):
            self.emit(events, {"type": "error", "text": f"{self.label}: {final['error'][:300]}"})


class CodeAgent(CLIAgent):
    """Claude Code (`claude -p`)."""

    label = "Claude Code"

    def __init__(self, root: Path):
        super().__init__(root)
        self._config = self.tmp / "mcp.json"
        self._config.write_text(json.dumps({"mcpServers": {"batzen": {
            "command": self.mcp_command()[0], "args": self.mcp_command()[1:], "env": self.mcp_env()}}}))

    def command(self, prompt: str) -> list[str]:
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--mcp-config", str(self._config), "--strict-mcp-config",
               "--allowedTools", "mcp__batzen__*", "Read(./inbox/**)", "Read(./belege/**)",
               "--append-system-prompt", self.system_prompt_text(),
               "--max-turns", str(MAX_ITERATIONS * 2)]
        if os.environ.get("BATZEN_CHAT_MODEL"):
            cmd += ["--model", os.environ["BATZEN_CHAT_MODEL"]]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def handle(self, event: dict, events: queue.Queue) -> dict | None:
        kind = event.get("type")
        if event.get("session_id"):
            self.session_id = event["session_id"]
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.emit(events, {"type": "text", "html": render_text(block["text"])})
                elif block.get("type") == "tool_use" and block.get("name") != "ToolSearch":
                    name = block["name"].removeprefix("mcp__batzen__")
                    self.emit(events, {"type": "tool", "text": describe_call(name, block.get("input") or {})})
        elif kind == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    self.emit(events, {"type": "tool_error", "text": text[:300]})
        elif kind == "result":
            failed = event.get("is_error") or event.get("subtype") not in ("success", None)
            return {"error": f"{event.get('subtype')} {str(event.get('result') or '')}" if failed else ""}
        return None


class CodexAgent(CLIAgent):
    """OpenAI Codex (`codex exec --json`), read-only sandbox: it may read the
    book but change it only through the batzen MCP tools."""

    label = "Codex"

    def command(self, prompt: str) -> list[str]:
        mcp = self.mcp_command()
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only",
               "-C", str(self.root),
               "-c", f"mcp_servers.batzen.command={json.dumps(mcp[0])}",
               "-c", f"mcp_servers.batzen.args={json.dumps(mcp[1:])}",
               "-c", f"mcp_servers.batzen.env={{BATZEN_AUTHOR={json.dumps(self.mcp_env()['BATZEN_AUTHOR'])}}}"]
        if os.environ.get("BATZEN_CHAT_MODEL"):
            cmd += ["-m", os.environ["BATZEN_CHAT_MODEL"]]
        if self.session_id:
            return cmd + ["resume", self.session_id, prompt]
        return cmd + [self.first_prompt(prompt)]

    def handle(self, event: dict, events: queue.Queue) -> dict | None:
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            self.session_id = event["thread_id"]
        elif kind in ("item.started", "item.completed"):
            item = event.get("item") or {}
            itype = item.get("type")
            if kind == "item.completed" and itype == "agent_message" and (item.get("text") or "").strip():
                self.emit(events, {"type": "text", "html": render_text(item["text"])})
            elif itype == "mcp_tool_call" and kind == "item.started":
                args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
                self.emit(events, {"type": "tool", "text": describe_call(item.get("tool", "?"), args)})
            elif itype == "mcp_tool_call" and kind == "item.completed" and item.get("status") == "failed":
                self.emit(events, {"type": "tool_error", "text": str(item.get("error") or "Tool fehlgeschlagen")[:300]})
            elif itype == "command_execution" and kind == "item.started":
                self.emit(events, {"type": "tool", "text": f"liest: {str(item.get('command'))[:120]}"})
        elif kind == "turn.completed":
            return {"error": ""}
        elif kind == "turn.failed":
            return {"error": str((event.get("error") or {}).get("message") or "Abbruch")}
        return None


class OpencodeAgent(CLIAgent):
    """opencode (`opencode run --format json`) with a private config: batzen
    MCP server, editing, shell and web denied."""

    label = "opencode"

    def __init__(self, root: Path):
        super().__init__(root)
        self._config = self.tmp / "opencode.json"
        self._config.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {"batzen": {"type": "local", "command": self.mcp_command(), "enabled": True,
                               "environment": self.mcp_env()}},
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        }))

    def env(self) -> dict:
        return {**os.environ, "OPENCODE_CONFIG": str(self._config)}

    def command(self, prompt: str) -> list[str]:
        cmd = ["opencode", "run", "--format", "json", "--dir", str(self.root)]
        if os.environ.get("BATZEN_CHAT_MODEL"):
            cmd += ["-m", os.environ["BATZEN_CHAT_MODEL"]]
        if self.session_id:
            cmd += ["--session", self.session_id]
        return cmd + [self.first_prompt(prompt)]

    def handle(self, event: dict, events: queue.Queue) -> dict | None:
        if event.get("sessionID"):
            self.session_id = event["sessionID"]
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "text" and (part.get("text") or "").strip():
            self.emit(events, {"type": "text", "html": render_text(part["text"])})
        elif kind == "tool_use":
            state = part.get("state") or {}
            name = str(part.get("tool", "?")).removeprefix("batzen_")
            self.emit(events, {"type": "tool", "text": describe_call(name, state.get("input") or {})})
            if state.get("status") == "error":
                self.emit(events, {"type": "tool_error", "text": str(state.get("error") or "")[:300]})
        elif kind == "error":
            err = event.get("error") or {}
            message = err.get("data", {}).get("message") if isinstance(err, dict) else str(err)
            return {"error": str(message or err)}
        elif kind == "step_finish" and part.get("reason") == "stop":
            return {"error": ""}
        return None


AGENTS = {"api": Agent, "claude-code": CodeAgent, "codex": CodexAgent, "opencode": OpencodeAgent}


def agent(ui) -> BaseAgent:
    """The drawer's agent for this book; switching the backend in the settings starts a new conversation."""
    from ..book import Book
    kind = backend(Book(ui.root).settings.get("agent_backend"))
    if ui.chat is None or getattr(ui.chat, "kind", None) != kind:
        ui.chat = AGENTS.get(kind, CodeAgent)(ui.root)
        ui.chat.kind = kind
    return ui.chat


def routes(ui) -> list[Route]:
    async def send(request: Request):
        user = getattr(request.state, "user", None)
        if user is not None and not user.can_write:
            return JSONResponse({"ok": False, "fehler": "Der Agent ist mit Leserecht nicht verfügbar."}, status_code=403)
        form = await request.form()
        message = (form.get("message") or "").strip()
        if not message:
            return JSONResponse({"ok": False, "fehler": "Leere Nachricht"}, status_code=400)
        from ..book import Book
        cred = credentials_status(Book(ui.root).settings.get("agent_backend"))
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
        return JSONResponse({"items": agent(ui).items, "busy": agent(ui).busy})

    return [
        Route("/chat/send", send, methods=["POST"]),
        Route("/chat/stream/{turn:str}", stream),
        Route("/chat/neu", reset, methods=["POST"]),
        Route("/chat/verlauf", history),
    ]

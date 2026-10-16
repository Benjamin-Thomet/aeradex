"""The HTTP side: the local call page, the ElevenLabs Custom LLM endpoint, the post-call webhook.

Who may call: the local call page, opened with the access key printed at start
(`aeradex anruf serve`); other plugins (aeradex-telegram) add their own entry with
`extra_routes`. The gateway issues a short-lived session token and an ElevenLabs signed
URL (the ElevenLabs agent requires auth, so nobody else can start a call). ElevenLabs
passes the token back on every LLM request, so each call maps to one person and one
agent, and results go to that person's channel (call page or chat).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from . import actions, elevenlabs, gedaechtnis
from .agent import Registry, make_agent, speakable, split_sources
from .anruf import Feed, PageChannel
from .config import Config

log = logging.getLogger("aeradex.anruf")

SESSION_TTL = 2 * 3600
LOCAL_USER = 0          # the person at the call page (no Telegram account involved)
# ElevenLabs drops the call when an answer pauses too long (cascade_timeout_seconds, at most 15 s), so the stream
# never stays silent for long: a filler after FIRST_WORDS seconds, then a short line every STILL_WORKING seconds.
FILLER = "Moment."
STILL_WORKING_LINES = ["Einen Augenblick noch.", "Ich bin dran.", "Dauert noch einen Moment.", "Gleich hab ich's."]
FIRST_WORDS = 3.5          # Haiku usually starts within 1 to 3 s; the filler is only for slow answers
STILL_WORKING = 6.0
MAX_FILLERS = 10
TOOL_LINES = {"WebSearch": "Ich schau kurz im Internet nach.", "WebFetch": "Ich lese kurz die Seite.",
              "web_search": "Ich schau kurz im Internet nach.", "web_fetch": "Ich lese kurz die Seite."}
BUSY_WAIT = 20.0
TICKET_TTL = 60.0              # the aeradex page's «Anrufen» → call page
BROWSER_TTL = 12 * 3600.0      # how long a browser that redeemed a ticket may use the call page
BROWSER_COOKIE = "aeradex_anruf"
NOTE_PROMPT = ("[Der Anruf ist beendet. Schreib für dein Gedächtnis EINEN Satz: was ihr besprochen und erledigt habt "
               "und was noch offen ist, mit Namen und Nummern. Nur die Notiz, ohne Einleitung. War nichts Wichtiges, "
               "schreib nur: nichts]")


# ---------- session tokens ----------

def _session_key(cfg: Config) -> bytes:
    return hashlib.sha256(b"aeradex-anruf-session:" + cfg.llm_secret.encode()).digest()


def make_session(cfg: Config, user_id: int, now: float | None = None) -> str:
    body = f"{int(user_id)}.{int((now or time.time()) + SESSION_TTL)}.{secrets.token_hex(8)}"
    sig = hmac.new(_session_key(cfg), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_session(cfg: Config, token: str, now: float | None = None, local: bool = False) -> tuple[int, str]:
    """→ (user id, call id); ValueError when forged, expired or the user is no longer allowed.
    `local`: the call page is served, so LOCAL_USER is accepted too."""
    parts = (token or "").strip().split(".")
    if len(parts) != 4:
        raise ValueError("Sitzung ungültig")
    uid, exp, nonce, sig = parts
    expected = hmac.new(_session_key(cfg), f"{uid}.{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        raise ValueError("Sitzung ungültig")
    if (now or time.time()) > int(exp):
        raise ValueError("Sitzung abgelaufen")
    if int(uid) not in cfg.allowed_users and not (local and int(uid) == LOCAL_USER):
        raise ValueError("Benutzer nicht berechtigt")
    return int(uid), nonce


def session_from_request(body: dict) -> str:
    extra = body.get("elevenlabs_extra_body") or {}
    if isinstance(extra, dict) and extra.get("session"):
        return str(extra["session"])
    for m in body.get("messages") or []:
        if m.get("role") == "system":
            found = re.search(r"Sitzung:\s*(\S+)", _content(m))
            if found:
                return found.group(1)
    return ""


def _content(message: dict) -> str:
    c = message.get("content")
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def check_webhook_signature(secret: str, header: str, raw: bytes, now: float | None = None, tolerance: int = 1800) -> None:
    """ElevenLabs-Signature: t=<unix>,v0=<hex HMAC-SHA256(secret, f"{t}.{body}")>."""
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    t, v0 = parts.get("t", ""), parts.get("v0", "")
    if not t.isdigit() or not v0:
        raise ValueError("Signatur fehlt")
    if abs((now or time.time()) - int(t)) > tolerance:
        raise ValueError("Signatur abgelaufen")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v0):
        raise ValueError("Signatur ungültig")


# ---------- the app ----------

def _chunk(text: str = "", finish: str | None = None, first: bool = False) -> str:
    delta = {"content": text} if text else {}
    if first:
        delta["role"] = "assistant"
    return "data: " + json.dumps({"id": "chatcmpl-aeradex", "object": "chat.completion.chunk", "created": int(time.time()),
                                  "model": "aeradex", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                                 ensure_ascii=False) + "\n\n"


GREETINGS = {"morgen": ["Guten Morgen {chef}!", "Morgen {chef}, schön dich zu hören."],
             "tag": ["Hoi {chef}, was kann ich für dich tun?", "Hallo {chef}! Was steht an?"],
             "abend": ["Guten Abend {chef}, noch fleissig?", "Hoi {chef}, so spät noch dran?"]}


def greeting(cfg: Config, now: float | None = None, name: str = "") -> str:
    """Her first sentence (ElevenLabs first_message = {{begruessung}}): by time of day, with the caller's first name
    (`name`, e.g. from Telegram), else the boss's."""
    hour = time.localtime(now).tm_hour
    part = "morgen" if 5 <= hour < 11 else "tag" if hour < 18 else "abend"
    options = GREETINGS[part]
    text = options[int(now or time.time()) // 60 % len(options)]
    return text.format(chef=name or cfg.boss_name).replace(" !", "!").replace("  ", " ").replace(" ,", ",")


async def signed_response(cfg: Config, uid: int, extra: dict | None = None, warm=None) -> JSONResponse:
    """An ElevenLabs signed URL plus our session token for `uid` (used by every call entry).
    `warm(session)` starts the call's agent right away, while ElevenLabs is still connecting."""
    if not cfg.voice_ready():
        return JSONResponse({"ok": False, "fehler": "Anrufen ist nicht eingerichtet (PUBLIC_URL, ELEVENLABS_API_KEY, "
                                                    "ELEVENLABS_AGENT_ID)"}, status_code=503)
    try:
        url = await asyncio.to_thread(elevenlabs.signed_url, cfg)
    except Exception as exc:
        return JSONResponse({"ok": False, "fehler": f"ElevenLabs: {exc}"}, status_code=502)
    session = make_session(cfg, uid)
    if warm is not None:
        warm(session)
    return JSONResponse({"ok": True, "signed_url": url, "session": session, "begruessung": greeting(cfg, name=str((extra or {}).get("name") or "")),
                         **(extra or {})})


def create_app(root: Path, cfg: Config, feed: Feed | None = None, page_key: str = "", registry: Registry | None = None,
               channel_for=None, notify=None, extra_routes: list | tuple = ()) -> Starlette:
    """`feed` + `page_key`: the local call page. `channel_for(uid)` / `notify(uid, text)`: where results and notes
    for other users go (aeradex-telegram: their chat); `extra_routes`: their entry points (Mini App)."""
    registry = registry or Registry(root)
    local = feed is not None

    def is_page(uid: int) -> bool:
        return local and (uid == LOCAL_USER or channel_for is None)

    def channel(uid: int):
        return PageChannel(feed) if is_page(uid) else channel_for(uid)

    def call_agent(uid: int, call_id: str):
        return registry.get(f"call:{call_id}", lambda: make_agent(root, channel(uid), cfg, voice=True))

    def warm(session: str) -> None:
        """Boot the call's Claude Code process and read its context now, so the first answer is fast."""
        uid, call_id = read_session(cfg, session, local=local)
        agent = call_agent(uid, call_id)
        if hasattr(agent, "warm_up"):
            threading.Thread(target=agent.warm_up, daemon=True).start()

    def tell(uid: int, text: str) -> None:
        """A note for the person: on the call page, or through the plugin that owns the user."""
        if is_page(uid):
            feed.push({"type": "hinweis", "text": text})
        elif notify is not None:
            notify(uid, text)

    tickets: dict[str, float] = {}     # one-time tickets from the aeradex page → expiry
    browsers: dict[str, float] = {}    # cookies of browsers that redeemed a ticket → expiry

    def key_ok(request: Request, given: str | None = None) -> bool:
        if not (local and page_key):
            return False
        given = given if given is not None else (request.query_params.get("key") or request.headers.get("x-aeradex-key") or "")
        if given and hmac.compare_digest(given.encode(), page_key.encode()):
            return True
        cookie = request.cookies.get(BROWSER_COOKIE, "")
        return bool(cookie) and browsers.get(cookie, 0) > time.time()

    def _fresh(store: dict[str, float], ttl: float) -> str:
        now = time.time()
        for k in [k for k, exp in store.items() if exp < now]:
            del store[k]
        token = secrets.token_urlsafe(32)
        store[token] = now + ttl
        return token

    def static(name: str) -> str:
        return resources.files("aeradex_anruf").joinpath(f"static/{name}").read_text(encoding="utf-8")

    # ---- the local call page ----

    async def page_ticket(request: Request):
        """The aeradex page (behind its login) asks for a one-time ticket with the page key, so the call page can be
        opened from another machine (aeradex on a server) without the long-lived key ever reaching a browser."""
        if not hmac.compare_digest((request.headers.get("x-aeradex-key") or "").encode(), page_key.encode()):
            return JSONResponse({"ok": False}, status_code=403)
        return JSONResponse({"ok": True, "ticket": _fresh(tickets, TICKET_TTL)})

    async def page(request: Request):
        ticket = request.query_params.get("ticket") or ""
        if ticket:
            exp = tickets.pop(ticket, 0)       # single use
            if exp < time.time():
                return PlainTextResponse("Der Link ist abgelaufen. In aeradex nochmals auf «Anrufen» klicken.",
                                         status_code=403)
            resp = RedirectResponse("/anruf", status_code=303)
            https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            resp.set_cookie(BROWSER_COOKIE, _fresh(browsers, BROWSER_TTL), max_age=int(BROWSER_TTL), path="/anruf",
                            httponly=True, secure=https, samesite="strict")
            return resp
        if not key_ok(request):
            return PlainTextResponse("Zugang nur über «Anrufen» in aeradex.", status_code=403)
        return HTMLResponse(static("anruf.html"), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def page_session(request: Request):
        if not key_ok(request):
            return JSONResponse({"ok": False, "fehler": "Zugang verweigert"}, status_code=403)
        return await signed_response(cfg, LOCAL_USER, warm=warm)

    async def page_events(request: Request):
        if not key_ok(request):
            return JSONResponse({"ok": False}, status_code=403)
        n = int(request.query_params.get("ab") or 0)

        async def gen():
            nonlocal n
            while not await request.is_disconnected():
                events = await asyncio.to_thread(feed.since, n, 15.0)
                if not events:
                    yield ": ping\n\n"
                    continue
                for ev in events:
                    n = ev["n"] + 1
                    yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def page_action(request: Request):
        form = await request.json()
        if not key_ok(request, str(form.get("key", ""))):
            return JSONResponse({"ok": False, "fehler": "Zugang verweigert"}, status_code=403)
        verb, _, action_id = str(form.get("data", "")).partition(":")
        try:
            if verb == "ok":
                outcome = await asyncio.to_thread(actions.confirm, root, action_id, None)   # as the book's git user
                feed.push({"type": "erledigt", "aktion": action_id, "text": "✅ " + outcome.text})
                if outcome.pdf and outcome.pdf.is_file():
                    PageChannel(feed).send_document(outcome.pdf)
            elif verb == "no":
                text = await asyncio.to_thread(actions.discard, root, action_id)
                feed.push({"type": "erledigt", "aktion": action_id, "text": "✖ " + text})
            else:
                return JSONResponse({"ok": False, "fehler": "Unbekannt"}, status_code=400)
        except Exception as exc:
            feed.push({"type": "erledigt", "aktion": action_id, "text": f"⚠️ {exc}"})
            return JSONResponse({"ok": False, "fehler": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})

    def end_call(session: str) -> bool:
        """A call ended (any entry): she writes a one-line note for next time, then the agent ends."""
        try:
            uid, call_id = read_session(cfg, session, local=local)
        except ValueError:
            return False
        agent = registry.peek(f"call:{call_id}")
        if agent is not None:
            threading.Thread(target=note_and_close, args=(agent, f"call:{call_id}", uid), daemon=True).start()
        return True

    async def page_end(request: Request):
        form = await request.json()
        if not key_ok(request, str(form.get("key", ""))):
            return JSONResponse({"ok": False}, status_code=403)
        ok = end_call(str(form.get("session", "")))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 403)

    def note_and_close(agent, key: str, uid: int) -> None:
        deadline = time.time() + 60
        while agent.busy and time.time() < deadline:
            time.sleep(0.5)
        try:
            spoken = [i for i in agent.items if i.get("type") == "me" and not str(i.get("text", "")).startswith("[")]
            if spoken:              # only when something was actually said
                note = agent.ask(NOTE_PROMPT)
                if note.strip() and "nichts" not in note.strip().lower()[:12]:
                    gedaechtnis.add_call(root, note)
                    tell(uid, "📝 Notiz fürs nächste Mal: " + note.strip())
        except Exception as exc:
            log.warning("Notiz zum Anruf: %s", exc)
        finally:
            registry.drop(key)

    async def page_file(request: Request):
        from starlette.responses import FileResponse
        if not key_ok(request):
            return PlainTextResponse("", status_code=403)
        path = feed.files.get(request.path_params["id"])
        if path is None or not path.is_file():
            return PlainTextResponse("", status_code=404)
        return FileResponse(path, filename=path.name, content_disposition_type="inline")

    # ---- ElevenLabs ----

    async def completions(request: Request):
        auth = request.headers.get("authorization", "")
        if not cfg.llm_secret or not hmac.compare_digest(auth.encode(), f"Bearer {cfg.llm_secret}".encode()):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        body = await request.json()
        try:
            uid, call_id = read_session(cfg, session_from_request(body), local=local)
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, status_code=403)
        users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
        prompt = _content(users[-1]).strip() if users else ""
        agent = call_agent(uid, call_id)
        to_page = is_page(uid)
        loop = asyncio.get_running_loop()
        out: asyncio.Queue = asyncio.Queue()

        def on_event(ev: dict) -> None:
            if to_page and ev.get("type") in ("tool", "tool_error"):
                feed.push({"type": ev["type"], "text": ev.get("text", "")})
            if to_page and ev.get("type") == "text" and ev.get("raw"):
                _, sources = split_sources(ev["raw"])
                if sources:          # shown on the page, not read out
                    feed.push({"type": "quellen", "links": [{"titel": t, "url": u} for t, u in sources]})
            loop.call_soon_threadsafe(out.put_nowait, ev)

        # Latest wins: while the caller keeps talking, ElevenLabs sends the growing sentence again and drops the
        # earlier request. Only the newest one is answered; older ones that are still waiting are skipped.
        with agent.lock:
            agent.request_seq = getattr(agent, "request_seq", 0) + 1
            seq = agent.request_seq

        def work() -> None:
            deadline = time.time() + BUSY_WAIT
            while agent.busy and time.time() < deadline and agent.request_seq == seq:
                time.sleep(0.1)
            try:
                if agent.request_seq != seq:
                    pass                                         # superseded by a newer request: say nothing
                elif agent.busy:
                    on_event({"type": "text", "raw": "Einen Moment, ich bin noch an der letzten Sache."})
                elif prompt:
                    agent.ask(prompt, on_event)
            except Exception as exc:
                on_event({"type": "error", "text": str(exc)[:200]})
            finally:
                loop.call_soon_threadsafe(out.put_nowait, {"type": "end"})

        threading.Thread(target=work, daemon=True).start()

        async def stream():
            # ElevenLabs gives up when the first words take too long (about 7 s, then it retries the turn),
            # so say a short filler when Claude needs a moment, and once more if it takes much longer.
            yield _chunk(first=True)
            fillers = 0
            announced: set[str] = set()
            timeout = FIRST_WORDS
            while True:
                try:
                    ev = await asyncio.wait_for(out.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if fillers < MAX_FILLERS:
                        line = FILLER if fillers == 0 else STILL_WORKING_LINES[(fillers - 1) % len(STILL_WORKING_LINES)]
                        fillers += 1
                        yield _chunk(line + " ")
                    timeout = STILL_WORKING
                    continue
                timeout = STILL_WORKING
                kind = ev.get("type")
                if kind == "end":
                    break
                if kind == "text" and ev.get("raw"):
                    yield _chunk(speakable(ev["raw"]).strip() + " ")
                elif kind == "tool":
                    tool = str(ev.get("text", "")).split("(", 1)[0]
                    if tool in TOOL_LINES and tool not in announced:     # say what takes long, once per kind
                        announced.add(tool)
                        fillers += 1
                        yield _chunk(TOOL_LINES[tool] + " ")
                elif kind == "error":
                    yield _chunk("Das hat leider nicht geklappt, die Details siehst du auf dem Bildschirm. ")
                    try:
                        await asyncio.to_thread(tell, uid, f"⚠️ Anruf: {ev.get('text', '')}")
                    except Exception:
                        pass
            yield _chunk(finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def webhook(request: Request):
        raw = await request.body()
        try:
            check_webhook_signature(cfg.webhook_secret, request.headers.get("elevenlabs-signature", ""), raw)
        except ValueError as exc:
            return JSONResponse({"ok": False, "fehler": str(exc)}, status_code=401)
        event = json.loads(raw)
        if event.get("type") != "post_call_transcription":
            return JSONResponse({"ok": True})
        data = event.get("data") or {}
        dyn = ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {})
        session = str(dyn.get("session", ""))
        try:
            uid, call_id = read_session(cfg, session, local=local)
        except ValueError:
            # A call may end after the token expired; the signature already proves the event.
            head = session.split(".")[0]
            if not head.isdigit() or not (int(head) in cfg.allowed_users or (local and int(head) == LOCAL_USER)):
                return JSONResponse({"ok": True})
            uid, call_id = int(head), ""
        summary = ((data.get("analysis") or {}).get("transcript_summary") or "").strip()
        secs = (data.get("metadata") or {}).get("call_duration_secs")
        text = "📞 Anruf beendet" + (f" ({int(secs) // 60}:{int(secs) % 60:02d})" if isinstance(secs, (int, float)) else "")
        await asyncio.to_thread(tell, uid, text + (f"\n\n{summary}" if summary else ""))
        if call_id:
            registry.drop(f"call:{call_id}")
        return JSONResponse({"ok": True})

    async def health(request: Request):
        return PlainTextResponse("ok")

    routes = [
        # ElevenLabs documents both a base URL (it appends /chat/completions) and the full path; accept either.
        Route("/llm/v1/chat/completions", completions, methods=["POST"]),
        Route("/llm/v1", completions, methods=["POST"]),
        Route("/elevenlabs/webhook", webhook, methods=["POST"]),
        Route("/health", health),
    ]
    if local:
        routes += [Route("/anruf", page), Route("/anruf/ticket", page_ticket, methods=["POST"]),
                   Route("/anruf/session", page_session, methods=["POST"]),
                   Route("/anruf/events", page_events), Route("/anruf/aktion", page_action, methods=["POST"]),
                   Route("/anruf/ende", page_end, methods=["POST"]),
                   Route("/anruf/datei/{id:str}", page_file)]
    routes += list(extra_routes)
    app = Starlette(routes=routes)
    # for other entries (aeradex-telegram's Mini App): warm a call up, end it with a note
    app.state.calls = SimpleNamespace(warm=warm, end=end_call)
    return app

"""The Telegram bot: text and voice notes to the remote agent, receipts to the Belegeingang, confirmation buttons."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from aeradex import api
from aeradex.book import Book, BookError

import json
from importlib import resources

from aeradex_anruf import actions, elevenlabs, gateway, transkript
from aeradex_anruf.agent import Registry, RemoteAgent, make_agent, speakable as _speakable
from aeradex_anruf.config import Config
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .telegram import Bot, ChatChannel, TelegramError, check_init_data, httpx

log = logging.getLogger("aeradex.telegram")

HELP = ("Ich bin {name}, deine Assistentin für die Buchhaltung. Schreib oder sprich, z.B.:\n"
        "- «Welche Rechnungen sind überfällig?»\n"
        "- «Offerte für K0001: 8 Stunden Malerarbeiten à 95 Franken»\n"
        "- Foto oder PDF einer Quittung schicken → Belegeingang\n\n"
        "{anrufen}/offen was auf dich wartet · /neu neues Gespräch · /stimme an|aus Antwort vorlesen")


def help_text(cfg: Config) -> str:
    """/anrufen only where calls are set up, so nobody is offered what does not work."""
    return HELP.format(name=cfg.assistant_name or "dein aeradex-Agent",
                       anrufen="/anrufen live sprechen · " if cfg.voice_ready() else "")

UPLOAD_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp")


def human_author(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Telegram"
    return f"{name} (Telegram) <telegram-{user.get('id')}@aeradex.local>"


class TelegramBot:
    def __init__(self, root: Path, cfg: Config, bot: Bot | None = None, registry: Registry | None = None):
        self.root = root
        self.cfg = cfg
        self.bot = bot or Bot(cfg.bot_token)
        self.registry = registry or Registry(root)
        self.voice_replies: set[int] = set()
        self.stop = threading.Event()
        self.warned: set[int] = set()

    # ---- plumbing ----

    def allowed(self, user: dict | None) -> bool:
        return bool(user) and int(user.get("id", 0)) in self.cfg.allowed_users

    def agent_for(self, chat_id: int) -> RemoteAgent:
        return self.registry.get(f"chat:{chat_id}", lambda: make_agent(
            self.root, ChatChannel(self.bot, chat_id), self.cfg, voice=False))

    def setup_menu(self) -> None:
        """The chat's menu button opens the call Mini App; the command list offers /anrufen only when calls work."""
        commands = [{"command": "offen", "description": "Was auf deine Freigabe wartet"},
                    {"command": "neu", "description": "Neues Gespräch"},
                    {"command": "stimme", "description": "Antworten vorlesen an/aus"}]
        if not self.cfg.voice_ready():
            try:
                self.bot.call("setMyCommands", commands=commands)
            except (TelegramError, httpx.HTTPError) as exc:
                log.warning("Befehle nicht gesetzt: %s", exc)
            return
        try:
            button = {"type": "web_app", "text": "Anrufen", "web_app": {"url": f"{self.cfg.public_url}/app"}}
            self.bot.call("setChatMenuButton", menu_button=button)
            for uid in self.cfg.allowed_users:      # Telegram does not always apply the default; set it per chat
                self.bot.call("setChatMenuButton", chat_id=uid, menu_button=button)
            self.bot.call("setMyCommands", commands=[
                {"command": "anrufen", "description": "Live mit dem Agenten sprechen"}, *commands])
        except (TelegramError, httpx.HTTPError) as exc:
            log.warning("Menü-Knopf nicht gesetzt: %s", exc)

    def warm_chats(self) -> None:
        """Start the chat agent of every allowed user now, so the first message does not wait for it."""
        for uid in self.cfg.allowed_users:
            agent = self.agent_for(uid)
            if hasattr(agent, "warm_up"):
                threading.Thread(target=agent.warm_up, daemon=True).start()

    def poll_forever(self) -> None:
        offset = None
        self.setup_menu()
        self.warm_chats()
        while not self.stop.is_set():
            try:
                updates = self.bot.updates(offset)
            except (TelegramError, httpx.HTTPError) as exc:
                log.warning("getUpdates: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                threading.Thread(target=self.handle_safely, args=(update,), daemon=True).start()

    def handle_safely(self, update: dict) -> None:
        try:
            self.handle(update)
        except Exception as exc:  # one bad update must not stop the bot
            log.exception("Update %s", update.get("update_id"))
            chat_id = ((update.get("message") or {}).get("chat") or {}).get("id") \
                or (((update.get("callback_query") or {}).get("message") or {}).get("chat") or {}).get("id")
            if chat_id:
                try:
                    self.bot.send_text(chat_id, f"⚠️ {type(exc).__name__}: {str(exc)[:300]}")
                except (TelegramError, httpx.HTTPError):
                    pass

    # ---- dispatch ----

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            return self.on_button(update["callback_query"])
        msg = update.get("message") or {}
        user, chat_id = msg.get("from"), (msg.get("chat") or {}).get("id")
        if not chat_id:
            return
        if not self.allowed(user):
            log.warning("Abgewiesen: Telegram-Benutzer %s (%s)", (user or {}).get("id"), (user or {}).get("username"))
            if chat_id not in self.warned:
                self.warned.add(chat_id)
                self.bot.send_text(chat_id, f"Nicht berechtigt. Deine Telegram-ID: {(user or {}).get('id')}")
            return
        if msg.get("chat", {}).get("type") != "private":
            return                                      # books are not discussed in groups
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            return self.on_command(chat_id, text)
        if msg.get("voice") or msg.get("audio"):
            return self.on_voice(chat_id, msg.get("voice") or msg.get("audio"))
        if msg.get("document") or msg.get("photo"):
            return self.on_upload(chat_id, msg)
        if text:
            return self.on_text(chat_id, text)

    def on_command(self, chat_id: int, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/hilfe", "/help"):
            self.bot.send_text(chat_id, help_text(self.cfg))
        elif cmd == "/neu":
            self.registry.drop(f"chat:{chat_id}")
            self.bot.send_text(chat_id, "Neues Gespräch.")
        elif cmd == "/stimme":
            on = arg.strip().lower() not in ("aus", "off", "0")
            (self.voice_replies.add if on else self.voice_replies.discard)(chat_id)
            self.bot.send_text(chat_id, "Antworten werden vorgelesen." if on else "Antworten nur als Text.")
        elif cmd == "/offen":
            self.on_open(chat_id)
        elif cmd == "/anrufen":
            if not self.cfg.voice_ready():
                self.bot.send_text(chat_id, "Anrufen ist noch nicht eingerichtet. Schreib mir oder schick eine Sprachnachricht.")
            else:
                self.bot.call("sendMessage", chat_id=chat_id, text="Tippe, um mit dem Agenten zu sprechen:",
                              reply_markup={"inline_keyboard": [[{"text": "📞 Anrufen",
                                                                  "web_app": {"url": f"{self.cfg.public_url}/app"}}]]})
        else:
            self.on_text(chat_id, text)

    def on_open(self, chat_id: int) -> None:
        book = Book(self.root)
        cards = actions.pending(self.root)
        for a in cards:
            self.bot.send_text(chat_id, a["zusammenfassung"], actions.buttons(a))
        proposals = api.proposals(book)
        drafts = api.bill_drafts(book)
        lines = []
        if proposals:
            lines.append(f"{len(proposals)} Buchungsvorschläge: " + ", ".join(str(p.get("ID")) for p in proposals[:10]))
        if drafts:
            lines.append(f"{len(drafts)} Belegentwürfe im Eingang")
        if not (cards or lines):
            lines.append("Nichts offen. 👌")
        if lines:
            self.bot.send_text(chat_id, "\n".join(lines) + ("\n\nSag «Vorschläge zur Freigabe vorlegen», dann kommen sie als Karten."
                                                             if proposals else ""))

    def on_text(self, chat_id: int, text: str, speak: bool | None = None) -> None:
        agent = self.agent_for(chat_id)
        stop = threading.Event()

        def keep_typing():
            while not stop.wait(4.5):
                self.bot.typing(chat_id)
        self.bot.typing(chat_id)
        threading.Thread(target=keep_typing, daemon=True).start()
        deadline = time.time() + 120
        while agent.busy and time.time() < deadline:   # warm-up or the previous message: wait, don't refuse
            time.sleep(0.3)
        try:
            answer = agent.ask(text)
        except RuntimeError as exc:
            answer = str(exc)
        finally:
            stop.set()
        self.bot.send_text(chat_id, answer or "(keine Antwort)")
        if (speak if speak is not None else chat_id in self.voice_replies) and answer and self.cfg.elevenlabs_key:
            try:
                audio, name = elevenlabs.speak(self.cfg, _speakable(answer))
                self.bot.send_voice(chat_id, audio, name)
            except (elevenlabs.ElevenLabsError, httpx.HTTPError, TelegramError) as exc:
                log.warning("TTS: %s", exc)

    def on_voice(self, chat_id: int, voice: dict) -> None:
        if not transkript.available(self.cfg):
            self.bot.send_text(chat_id, "Sprachnachrichten sind noch nicht eingerichtet (ElevenLabs oder Gemini). "
                                        "Schreib mir solange.")
            return
        self.bot.typing(chat_id)
        audio, name = self.bot.download(voice["file_id"])
        try:
            text = transkript.transcribe(self.cfg, audio, name)
        except transkript.TranscriptionError as exc:
            log.warning("Sprachnachricht: %s", exc)
            self.bot.send_text(chat_id, "Die Sprachnachricht konnte ich nicht abhören. Versuch es nochmals oder schreib mir.")
            return
        if not text:
            self.bot.send_text(chat_id, "Ich habe nichts verstanden.")
            return
        self.bot.send_text(chat_id, f"🎙 «{text}»")
        self.on_text(chat_id, text, speak=True if chat_id in self.voice_replies else None)

    def on_upload(self, chat_id: int, msg: dict) -> None:
        """A receipt or invoice → inbox → Belegeingang (draft). Nothing is booked."""
        if msg.get("photo"):
            file_id, name = msg["photo"][-1]["file_id"], None
        else:
            file_id, name = msg["document"]["file_id"], msg["document"].get("file_name")
        data, server_name = self.bot.download(file_id)
        name = _safe_name(name or server_name)
        if not name.lower().endswith(UPLOAD_SUFFIXES):
            self.bot.send_text(chat_id, "Bitte PDF oder Foto schicken.")
            return
        inbox = self.root / "inbox"
        inbox.mkdir(exist_ok=True)
        target = inbox / name
        n = 1
        while target.exists():
            target = inbox / f"{Path(name).stem}-tg{n}{Path(name).suffix}"
            n += 1
        target.write_bytes(data)
        caption = (msg.get("caption") or "").strip()
        prompt = (f"Neuer Beleg aus Telegram: inbox/{target.name}. Lege ihn mit create_bill_draft an und kontiere ihn, "
                  "wenn nötig (bill_draft, complete_bill_draft). Sag mir kurz, was es ist und was ich noch prüfen muss.")
        self.on_text(chat_id, prompt + (f"\nHinweis von mir: {caption}" if caption else ""))

    def on_button(self, cb: dict) -> None:
        user = cb.get("from") or {}
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not self.allowed(user):
            self.bot.answer_callback(cb["id"], "Nicht berechtigt")
            return
        verb, _, action_id = (cb.get("data") or "").partition(":")
        try:
            if verb == "ok":
                self.bot.answer_callback(cb["id"], "Wird ausgeführt …")
                outcome = actions.confirm(self.root, action_id, human_author(user))
                if chat_id and msg.get("message_id"):
                    self.bot.drop_buttons(chat_id, msg["message_id"])
                self.bot.send_text(chat_id, "✅ " + outcome.text)
                if outcome.pdf and outcome.pdf.is_file():
                    self.bot.send_document(chat_id, outcome.pdf)
            elif verb == "no":
                text = actions.discard(self.root, action_id)
                self.bot.answer_callback(cb["id"], "Verworfen")
                if chat_id and msg.get("message_id"):
                    self.bot.drop_buttons(chat_id, msg["message_id"])
                self.bot.send_text(chat_id, "✖ " + text)
            else:
                self.bot.answer_callback(cb["id"])
        except BookError as exc:
            if chat_id and msg.get("message_id"):
                self.bot.drop_buttons(chat_id, msg["message_id"])
            self.bot.send_text(chat_id, f"⚠️ {exc}")

    # ---- used by the gateway ----

    def notify(self, user_id: int, text: str) -> None:
        self.bot.send_text(user_id, text)


def _safe_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9ÄÖÜäöüéèà._-]+", "-", name).strip("-.")
    return name[:120] or f"beleg-{int(time.time())}.pdf"


# ---------- the gateway with the Telegram entry (Mini App) ----------

def create_app(root: Path, cfg: Config, tg: TelegramBot, feed=None, page_key: str = ""):
    """aeradex-anruf's gateway; calls started from the Mini App report to the caller's chat. With `feed` and
    `page_key` the local call page runs in the same process (one address for ElevenLabs)."""
    async def mini_app_user(request: Request) -> tuple[dict | None, dict, JSONResponse | None]:
        try:
            data = await request.json()
            user = check_init_data(str(data.get("initData", "")), cfg.bot_token)
        except (ValueError, json.JSONDecodeError) as exc:
            return None, {}, JSONResponse({"ok": False, "fehler": str(exc)}, status_code=401)
        if int(user["id"]) not in cfg.allowed_users:
            log.warning("Mini App abgewiesen: %s", user.get("id"))
            return None, data, JSONResponse({"ok": False, "fehler": "Nicht berechtigt"}, status_code=403)
        return user, data, None

    async def app_page(request: Request):
        html = resources.files("aeradex_telegram").joinpath("static/app.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def app_session(request: Request):
        user, _, refused = await mini_app_user(request)
        if refused:
            return refused
        return await gateway.signed_response(cfg, int(user["id"]), {"name": user.get("first_name", "")},
                                             warm=request.app.state.calls.warm)

    async def app_end(request: Request):
        user, data, refused = await mini_app_user(request)
        if refused:
            return refused
        session = str(data.get("session", ""))
        if not session.startswith(f"{int(user['id'])}."):          # only your own call
            return JSONResponse({"ok": False}, status_code=403)
        return JSONResponse({"ok": request.app.state.calls.end(session)})

    return gateway.create_app(root, cfg, feed=feed, page_key=page_key, registry=tg.registry,
                              channel_for=lambda uid: ChatChannel(tg.bot, uid), notify=tg.notify,
                              extra_routes=[Route("/app", app_page), Route("/app/session", app_session, methods=["POST"]),
                                            Route("/app/ende", app_end, methods=["POST"])])

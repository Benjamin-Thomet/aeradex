"""A minimal Telegram Bot API client (long polling, no public URL needed) and Mini App initData check."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

try:
    import httpx2 as httpx          # what the anthropic SDK ships with
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]

API = "https://api.telegram.org"
MAX_MESSAGE = 4000


class TelegramError(RuntimeError):
    pass


class Bot:
    def __init__(self, token: str, client=None):
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(70.0, connect=10.0))

    def _redact(self, exc: Exception) -> TelegramError:
        """HTTP errors carry the request URL, and that contains the bot token."""
        return TelegramError(f"{type(exc).__name__}: {str(exc).replace(self.token, '<token>')}")

    def call(self, method: str, files: dict | None = None, **params):
        url = f"{API}/bot{self.token}/{method}"
        try:
            if files:
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items() if v is not None}
                resp = self.client.post(url, data=data, files=files)
            else:
                resp = self.client.post(url, json={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as exc:
            raise self._redact(exc) from None
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', resp.status_code)}")
        return body["result"]

    # ---- sending ----

    def send_text(self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        text = text.strip() or "…"
        chunks = [text[i:i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)]
        for n, chunk in enumerate(chunks):
            markup = None
            if buttons and n == len(chunks) - 1:
                markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
            self.call("sendMessage", chat_id=chat_id, text=chunk, reply_markup=markup)

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        with open(path, "rb") as fh:
            self.call("sendDocument", files={"document": (path.name, fh)}, chat_id=chat_id, caption=caption[:1000] or None)

    def send_voice(self, chat_id: int, audio: bytes, filename: str = "antwort.ogg") -> None:
        method, field = ("sendVoice", "voice") if filename.endswith(".ogg") else ("sendAudio", "audio")
        self.call(method, files={field: (filename, audio)}, chat_id=chat_id)

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except (TelegramError, httpx.HTTPError):
            pass

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190] or None)
        except (TelegramError, httpx.HTTPError):
            pass

    def drop_buttons(self, chat_id: int, message_id: int, note: str = "") -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        except (TelegramError, httpx.HTTPError):
            pass

    def download(self, file_id: str) -> tuple[bytes, str]:
        info = self.call("getFile", file_id=file_id)
        path = info["file_path"]
        try:
            resp = self.client.get(f"{API}/file/bot{self.token}/{path}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._redact(exc) from None
        return resp.content, path.rsplit("/", 1)[-1]

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])


class ChatChannel:
    """actions.Channel for one Telegram chat."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id

    def send_text(self, text: str) -> None:
        self.bot.send_text(self.chat_id, text)

    def send_document(self, path: Path, caption: str = "") -> None:
        self.bot.send_document(self.chat_id, path, caption)

    def send_card(self, text: str, buttons: list[tuple[str, str]]) -> None:
        self.bot.send_text(self.chat_id, text, buttons)


def check_init_data(init_data: str, bot_token: str, max_age: int = 300, now: float | None = None) -> dict:
    """Validate Telegram Mini App initData (HMAC-SHA256 with key HMAC("WebAppData", bot_token)).
    Returns the parsed user dict; raises ValueError when the data is forged or too old."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", "")
    if not received:
        raise ValueError("initData ohne hash")
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("initData ungültig")
    auth_date = int(pairs.get("auth_date") or 0)
    if (now or time.time()) - auth_date > max_age:
        raise ValueError("initData abgelaufen")
    user = json.loads(pairs.get("user") or "{}")
    if not isinstance(user, dict) or not user.get("id"):
        raise ValueError("initData ohne Benutzer")
    return user

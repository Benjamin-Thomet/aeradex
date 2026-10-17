"""Voice notes to text: ElevenLabs Scribe when its key is set, else the OpenAI-compatible model of the agent
(Google Gemini …), so a bot that runs on Gemini understands voice notes without a second account."""
from __future__ import annotations

import base64
import shutil
import subprocess

from . import elevenlabs
from .config import Config
from .elevenlabs import httpx

PROMPT = ("Schreib diese Sprachnachricht wörtlich ab. Spricht die Person Schweizerdeutsch, schreib es auf "
          "Hochdeutsch (Schweizer Rechtschreibung, kein ß). Zahlen, Beträge und Namen genau so, wie sie gesagt "
          "werden. Gib nur den Text zurück, ohne Kommentar. Ist nichts zu verstehen, antworte nur mit: -")

MIME = {"oga": "audio/ogg", "ogg": "audio/ogg", "opus": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
        "mp4": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav", "flac": "audio/flac"}


class TranscriptionError(RuntimeError):
    pass


def available(cfg: Config) -> bool:
    return bool(cfg.elevenlabs_key) or _openai() is not None


def transcribe(cfg: Config, audio: bytes, filename: str = "voice.ogg") -> str:
    """The spoken text, "" when nothing was understood."""
    if cfg.elevenlabs_key:
        try:
            return elevenlabs.transcribe(cfg, audio, filename)
        except (elevenlabs.ElevenLabsError, httpx.HTTPError) as exc:
            raise TranscriptionError(str(exc)) from None
    settings = _openai()
    if settings is None:
        raise TranscriptionError("Sprachnachrichten brauchen ELEVENLABS_API_KEY oder den Zugang zum Sprachmodell "
                                 "(AERADEX_OPENAI_…, z.B. Gemini)")
    try:
        if "generativelanguage.googleapis.com" in settings["base"]:
            text = _gemini(settings, audio, _mime(filename))
        else:
            text = _openai_compatible(settings, audio, filename)
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"{settings['model']}: {type(exc).__name__}") from None
    text = (text or "").strip().strip("«»\"").strip()
    return "" if text in ("", "-") else text


def _openai() -> dict | None:
    from aeradex.web import chat
    return chat.openai_settings()


def _mime(filename: str) -> str:
    return MIME.get(filename.rsplit(".", 1)[-1].lower(), "audio/ogg")


def _check(resp, model: str) -> dict:
    if resp.status_code >= 400:
        raise TranscriptionError(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _post(url: str, headers: dict, payload: dict, model: str) -> dict:
    with httpx.Client(timeout=120.0) as c:
        return _check(c.post(url, headers=headers, json=payload), model)


def _gemini(settings: dict, audio: bytes, mime: str) -> str:
    """Gemini's own API: it takes Telegram's OGG/Opus as it is (the OpenAI-compatible one wants wav/mp3)."""
    root = settings["base"].rstrip("/").removesuffix("/openai")
    model = settings["model"].removeprefix("models/")
    data = _post(f"{root}/models/{model}:generateContent", {"x-goog-api-key": settings["key"]},
                 {"contents": [{"parts": [{"text": PROMPT},
                                          {"inline_data": {"mime_type": mime,
                                                           "data": base64.b64encode(audio).decode()}}]}],
                  "generationConfig": {"temperature": 0}}, settings["model"])
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def _openai_compatible(settings: dict, audio: bytes, filename: str) -> str:
    fmt = filename.rsplit(".", 1)[-1].lower()
    if fmt not in ("wav", "mp3"):
        audio, fmt = _to_mp3(audio), "mp3"
    data = _post(f"{settings['base']}/chat/completions", {"authorization": f"Bearer {settings['key']}"},
                 {"model": settings["model"], "temperature": 0,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": PROMPT},
                      {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(),
                                                              "format": fmt}}]}]},
                 settings["model"])
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def _to_mp3(audio: bytes) -> bytes:
    if not shutil.which("ffmpeg"):
        raise TranscriptionError("Für Sprachnachrichten mit diesem Modell braucht es ffmpeg")
    proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
                          input=audio, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise TranscriptionError("Sprachnachricht konnte nicht umgewandelt werden")
    return proc.stdout

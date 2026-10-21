"""Voice input: speech to text for the agent's microphone in the browser. The core records nothing and knows no
speech service; a plugin offers one through the hook `aeradex_sprache_zu_text` (e.g. aeradex-anruf: ElevenLabs
Scribe, else the agent's Gemini model)."""
from __future__ import annotations

from . import plugins
from .book import Book

MAX_BYTES = 12 * 1024 * 1024          # a few minutes of compressed speech


class SpeechError(RuntimeError):
    pass


def available(book: Book) -> bool:
    try:
        return plugins.sprache_zu_text(book) is not None
    except Exception:
        return False


def transcribe(book: Book, audio: bytes, filename: str = "sprache.webm") -> str:
    """The spoken text ("" when nothing was understood)."""
    if not audio:
        raise SpeechError("Keine Aufnahme erhalten")
    if len(audio) > MAX_BYTES:
        raise SpeechError("Die Aufnahme ist zu lang")
    fn = plugins.sprache_zu_text(book)
    if fn is None:
        raise SpeechError("Spracheingabe ist nicht eingerichtet (Funktion «Telegram-Agent» mit Sprachdienst)")
    try:
        return (fn(audio, filename) or "").strip()
    except Exception as exc:
        raise SpeechError(f"Sprache konnte nicht erkannt werden: {exc}") from None

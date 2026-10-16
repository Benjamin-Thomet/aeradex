"""ElevenLabs: speech-to-text for voice notes, TTS for spoken replies, signed call URLs, and the call agent as code."""
from __future__ import annotations

import shutil
import subprocess

try:
    import httpx2 as httpx          # what the anthropic SDK ships with
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]

from .config import Config, write_env_value

API = "https://api.elevenlabs.io"
DEFAULT_VOICE = "onwK4e9ZLuTAKqWW03F9"      # "Daniel"; any multilingual voice speaks German with the v2.5 models
TTS_MODEL = "eleven_v4_turbo"             # voice replies in a chat: same voice and model as in the call
CALL_MODEL = "eleven_v4_turbo"            # the call: natural and as fast as v3 conversational (about 0.2 s)

# The call agent's own prompt is only a carrier: the conversation is run by Claude behind the Custom LLM.
# {{session}} is the signed session token from the Mini App; the gateway reads it to know who is calling.
AGENT_PROMPT = "aeradex Buchhaltungsassistent. Sitzung: {{session}}"


class ElevenLabsError(RuntimeError):
    pass


def _client(cfg: Config):
    if not cfg.elevenlabs_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY fehlt")
    return httpx.Client(base_url=API, headers={"xi-api-key": cfg.elevenlabs_key}, timeout=60.0)


def _ok(resp) -> dict:
    if resp.status_code >= 400:
        raise ElevenLabsError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


def transcribe(cfg: Config, audio: bytes, filename: str = "voice.ogg") -> str:
    with _client(cfg) as c:
        data = _ok(c.post("/v1/speech-to-text", data={"model_id": cfg.stt_model, "language_code": "de"},
                          files={"file": (filename, audio)}))
    return (data.get("text") or "").strip()


def speak(cfg: Config, text: str) -> tuple[bytes, str]:
    """TTS → (audio, filename). OGG/Opus when ffmpeg is there (Telegram voice message), else MP3."""
    voice = cfg.elevenlabs_voice_id or DEFAULT_VOICE
    with _client(cfg) as c:
        resp = c.post(f"/v1/text-to-speech/{voice}", params={"output_format": "mp3_44100_128"},
                      json={"text": text[:2500], "model_id": TTS_MODEL})
        if resp.status_code >= 400:
            raise ElevenLabsError(f"ElevenLabs TTS HTTP {resp.status_code}: {resp.text[:200]}")
        mp3 = resp.content
    if shutil.which("ffmpeg"):
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "48k",
                               "-f", "ogg", "pipe:1"], input=mp3, capture_output=True, timeout=60)
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout, "antwort.ogg"
    return mp3, "antwort.mp3"


def signed_url(cfg: Config) -> str:
    with _client(cfg) as c:
        data = _ok(c.get("/v1/convai/conversation/get-signed-url", params={"agent_id": cfg.elevenlabs_agent_id}))
    return data["signed_url"]


def agent_config(cfg: Config, secret_id: str, first_message: str) -> dict:
    return {
        "name": "aeradex Buchhaltung",
        "conversation_config": {
            "agent": {
                "first_message": first_message,
                "language": "de",
                "prompt": {
                    "prompt": AGENT_PROMPT,
                    "llm": "custom-llm",
                    "custom_llm": {"url": f"{cfg.public_url}/llm/v1", "model_id": "aeradex",
                                   "api_key": {"secret_id": secret_id}},
                    "cascade_timeout_seconds": 15,      # longest allowed gap between parts of an answer (web search)
                },
            },
            "turn": {"turn_eagerness": "patient"},       # let the person finish (addresses, names), don't cut in
            "tts": {"voice_id": cfg.elevenlabs_voice_id or DEFAULT_VOICE, "model_id": CALL_MODEL,
                    "stability": 0.4, "similarity_boost": 0.8},   # a little less stable = livelier
        },
        "platform_settings": {
            "auth": {"enable_auth": True},                       # only signed URLs from our gateway can start a call
            "overrides": {"custom_llm_extra_body": True},        # the Mini App passes the session token along
        },
    }


def setup_agent(cfg: Config, first_message: str = "{{begruessung}}") -> dict:
    """Create or update the call agent (Custom LLM → this gateway) and remember its id in the env file."""
    if not cfg.public_url.startswith("https://"):
        raise ElevenLabsError("PUBLIC_URL (https://…, z.B. die Tailscale-Funnel-Adresse) fehlt")
    if len(cfg.llm_secret) < 24:
        raise ElevenLabsError("LLM_SHARED_SECRET fehlt oder ist zu kurz")
    with _client(cfg) as c:
        secret = _ok(c.post("/v1/convai/secrets", json={"type": "new", "name": "aeradex_llm_secret",
                                                          "value": cfg.llm_secret}))
        body = agent_config(cfg, secret["secret_id"], first_message)
        if cfg.elevenlabs_agent_id:
            _ok(c.patch(f"/v1/convai/agents/{cfg.elevenlabs_agent_id}", json=body))
            agent_id = cfg.elevenlabs_agent_id
        else:
            agent_id = _ok(c.post("/v1/convai/agents/create", json=body))["agent_id"]
    write_env_value("ELEVENLABS_AGENT_ID", agent_id)
    return {"ok": True, "agent_id": agent_id, "custom_llm": f"{cfg.public_url}/llm/v1",
            "hinweis": "Post-Call-Webhook im ElevenLabs-Dashboard auf "
                       f"{cfg.public_url}/elevenlabs/webhook setzen und das Secret als ELEVENLABS_WEBHOOK_SECRET eintragen."}

"""Settings from the environment and `~/.config/aeradex/anruf.env` (KEY=value, chmod 600).

Shared by aeradex-anruf and aeradex-telegram (the TELEGRAM_* keys).

Secrets never live in the book: the book is a git repository that may be shared.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = Path(os.environ.get("AERADEX_ANRUF_ENV") or Path.home() / ".config" / "aeradex" / "anruf.env")


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return out


def write_env_value(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set one KEY=value in the env file, keeping everything else (used by elevenlabs-setup)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.strip().removeprefix("export ").startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _git_first_name() -> str:
    import subprocess
    try:
        name = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return name.strip().split(" ")[0] if name.strip() else ""


@dataclass
class Config:
    bot_token: str = ""
    allowed_users: set[int] = field(default_factory=set)
    public_url: str = ""                 # https://… (tunnel), for the Mini App and ElevenLabs
    elevenlabs_key: str = ""
    elevenlabs_agent_id: str = ""
    elevenlabs_voice_id: str = ""        # voice for spoken replies in the chat and for the call agent
    llm_secret: str = ""                 # ElevenLabs → our Custom LLM endpoint (Bearer)
    webhook_secret: str = ""             # ElevenLabs post-call webhook HMAC
    backend: str = "claude-code"          # claude-code (own login) | api (ANTHROPIC_API_KEY) | openai (Gemini …)
    voice_model: str = "claude-haiku-5-5"   # answers in the call: fast (about 1 s)
    expert_model: str = "claude-opus-5-5"   # does the harder, multi-step work for it ("" = no expert)
    tools_mode: str = "alle"                # alle: every aeradex tool, like the MCP chat | entwuerfe: drafts only
    assistant_name: str = "Susi"            # who answers the phone
    boss_name: str = ""                     # who calls; empty = first name of the git user
    chat_model: str = ""                 # empty = aeradex default (AERADEX_MODEL)
    stt_model: str = "scribe_v2"
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def load(cls, path: Path = ENV_FILE) -> "Config":
        env = {**read_env_file(path), **{k: v for k, v in os.environ.items() if v}}
        # API keys may be in the env file only; the clients read them from os.environ.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AERADEX_OPENAI_BASE_URL", "AERADEX_OPENAI_API_KEY",
                    "AERADEX_OPENAI_MODEL"):
            if env.get(key) and not os.environ.get(key):
                os.environ[key] = env[key]
        users = {int(u) for u in env.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(",", " ").split() if u.strip().isdigit()}
        return cls(
            bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            allowed_users=users,
            public_url=env.get("PUBLIC_URL", "").rstrip("/"),
            elevenlabs_key=env.get("ELEVENLABS_API_KEY", ""),
            elevenlabs_agent_id=env.get("ELEVENLABS_AGENT_ID", ""),
            elevenlabs_voice_id=env.get("ELEVENLABS_VOICE_ID", ""),
            llm_secret=env.get("LLM_SHARED_SECRET", ""),
            webhook_secret=env.get("ELEVENLABS_WEBHOOK_SECRET", ""),
            backend=env.get("AERADEX_ANRUF_BACKEND", cls.backend),
            voice_model=env.get("AERADEX_VOICE_MODEL", cls.voice_model),
            expert_model=env.get("AERADEX_EXPERT_MODEL", cls.expert_model),
            tools_mode=env.get("AERADEX_ANRUF_WERKZEUGE", cls.tools_mode),
            assistant_name=env.get("AERADEX_ANRUF_NAME", cls.assistant_name),
            boss_name=env.get("AERADEX_ANRUF_CHEF") or _git_first_name(),
            chat_model=env.get("AERADEX_CHAT_MODEL_REMOTE", ""),
            stt_model=env.get("ELEVENLABS_STT_MODEL", cls.stt_model),
            host=env.get("AERADEX_ANRUF_HOST", cls.host),
            port=int(env.get("AERADEX_ANRUF_PORT", cls.port)),
        )

    def problems_agent(self) -> list[str]:
        """Whether the chosen backend can run (German, shown to the person)."""
        if self.backend == "api":
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                return ["ANTHROPIC_API_KEY fehlt (oder AERADEX_ANRUF_BACKEND=claude-code für dein Claude-Abo)"]
            return []
        if self.backend == "openai":
            from aeradex.web import chat
            if chat.openai_settings() is None:
                return ["AERADEX_OPENAI_BASE_URL, AERADEX_OPENAI_API_KEY und AERADEX_OPENAI_MODEL setzen "
                        "(z.B. Google Gemini), oder AERADEX_ANRUF_BACKEND=claude-code"]
            return []
        if self.backend != "claude-code":
            return [f"AERADEX_ANRUF_BACKEND={self.backend} unbekannt (claude-code, api oder openai)"]
        if not shutil.which("claude"):
            return ["Claude Code (`claude`) nicht gefunden; installieren und anmelden, oder AERADEX_ANRUF_BACKEND=api"]
        return []

    def problems_telegram(self) -> list[str]:
        """What is missing for `telegram serve`."""
        out = []
        if not self.bot_token:
            out.append("TELEGRAM_BOT_TOKEN fehlt (von @BotFather)")
        if not self.allowed_users:
            out.append("TELEGRAM_ALLOWED_USER_IDS fehlt (deine Telegram-ID, z.B. von @userinfobot)")
        if len(self.llm_secret) < 24:
            out.append("LLM_SHARED_SECRET fehlt oder ist zu kurz (mind. 24 Zeichen, z.B. `openssl rand -hex 32`)")
        return out + self.problems_agent()

    def problems_call(self) -> list[str]:
        """What is missing for `anruf serve` (talk on the call page, no Telegram)."""
        out = []
        if len(self.llm_secret) < 24:
            out.append("LLM_SHARED_SECRET fehlt oder ist zu kurz (mind. 24 Zeichen, z.B. `openssl rand -hex 32`)")
        out += self.problems_agent()
        if not self.elevenlabs_key:
            out.append("ELEVENLABS_API_KEY fehlt")
        if not self.public_url.startswith("https://"):
            out.append("PUBLIC_URL fehlt (https://…, z.B. `tailscale funnel --bg 8765`), ElevenLabs muss den Gateway erreichen")
        if not self.elevenlabs_agent_id:
            out.append("ELEVENLABS_AGENT_ID fehlt: `aeradex anruf elevenlabs-setup` ausführen")
        return out

    def voice_ready(self) -> bool:
        return bool(self.public_url.startswith("https://") and self.elevenlabs_key and self.elevenlabs_agent_id)

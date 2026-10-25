import os
import re
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Cartesia "Michelle - Empathetic Voice": gentle and unhurried.
DEFAULT_VOICE_ID = "d7bf7d75-64b7-4c1e-86c0-79d647366587"


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.groq_api_key = _require("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        self.groq_fast_model = os.getenv("GROQ_FAST_MODEL", "").strip() or self.groq_model
        # Groq account limits (defaults: free tier, llama-3.3-70b-versatile).
        self.groq_rpm = int(os.getenv("GROQ_RPM", "30"))
        self.groq_tpm = int(os.getenv("GROQ_TPM", "12000"))
        self.groq_rpd = int(os.getenv("GROQ_RPD", "1000"))
        self.groq_tpd = int(os.getenv("GROQ_TPD", "100000"))
        self.groq_max_wait = float(os.getenv("GROQ_MAX_WAIT_SECONDS", "8"))
        # Per-user limits on model-backed /reframe calls.
        self.user_rpm = int(os.getenv("USER_REFRAME_PER_MINUTE", "6"))
        self.user_rph = int(os.getenv("USER_REFRAME_PER_HOUR", "60"))
        self.auth_per_minute = int(os.getenv("AUTH_PER_MINUTE", "10"))
        self.spacetime_host = _require("SPACETIME_HOST").rstrip("/")
        self.spacetime_db = _require("SPACETIME_DB_NAME")
        self.spacetime_token = _require("SPACETIME_TOKEN")
        self.jwt_secret = _require("JWT_SECRET")
        self.jwt_ttl_hours = int(os.getenv("JWT_TTL_HOURS", "72"))
        # Voice (optional): without a key the extension falls back to the chat.
        self.cartesia_api_key = os.getenv("CARTESIA_API_KEY", "").strip()
        voice = os.getenv("CARTESIA_VOICE_ID", "").strip()
        self.cartesia_voice_id = voice if _UUID.match(voice) else DEFAULT_VOICE_ID
        self.cartesia_tts_model = os.getenv("CARTESIA_TTS_MODEL", "sonic-latest").strip()
        self.cartesia_stt_model = os.getenv("CARTESIA_STT_MODEL", "ink-whisper").strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()

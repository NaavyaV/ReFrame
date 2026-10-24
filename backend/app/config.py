import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.groq_api_key = _require("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
        self.groq_fast_model = os.getenv("GROQ_FAST_MODEL", "").strip() or self.groq_model
        self.spacetime_host = _require("SPACETIME_HOST").rstrip("/")
        self.spacetime_db = _require("SPACETIME_DB_NAME")
        self.spacetime_token = _require("SPACETIME_TOKEN")
        self.jwt_secret = _require("JWT_SECRET")
        self.jwt_ttl_hours = int(os.getenv("JWT_TTL_HOURS", "72"))


@lru_cache
def get_settings() -> Settings:
    return Settings()

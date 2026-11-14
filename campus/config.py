"""Central configuration. Everything can be overridden via .env / environment."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    app_name: str = "Smart Campus Multi-Agent System"
    # server
    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_env("PORT", "8000"))
    db_path: str = _env("CAMPUS_DB", "campus.db")
    # agent-to-agent transport: "inline" (in-process) or "http" (REST)
    transport: str = _env("AGENT_TRANSPORT", "inline")
    # llm provider fallback chain
    provider_order: list = field(default_factory=lambda: [
        p.strip() for p in _env("LLM_PROVIDER_ORDER", "gemini,groq,openrouter").split(",") if p.strip()
    ])
    google_api_key: str = _env("GOOGLE_API_KEY", "")
    groq_api_key: str = _env("GROQ_API_KEY", "")
    openrouter_api_key: str = _env("OPENROUTER_API_KEY", "")
    gemini_model: str = _env("GEMINI_MODEL", "gemini-2.5-flash")
    groq_model: str = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
    openrouter_model: str = _env("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    # policy
    auto_apply_threshold: float = float(_env("AUTO_APPLY_THRESHOLD", "0.75"))
    max_negotiation_rounds: int = int(_env("MAX_NEGOTIATION_ROUNDS", "3"))
    auto_resolve: bool = _env("AUTO_RESOLVE", "1") == "1"
    scan_interval_sec: int = int(_env("SCAN_INTERVAL_SEC", "60"))
    background_tasks: bool = _env("BACKGROUND_TASKS", "1") == "1"
    seed_on_start: bool = _env("SEED_ON_START", "1") == "1"


settings = Settings()

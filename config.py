"""App configuration, loaded from .env via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowlist: list[int] = []

    # LLM (OpenAI-compatible endpoint; defaults target Gemini's compat layer)
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.0-flash-lite"

    # Search defaults
    home_plz: str = "10115"
    default_max_distance_km: float = 50.0

    # Fetching / anti-blocking
    max_requests_per_run: int = 20
    fetch_delay_floor_search_s: float = 2.0
    fetch_delay_floor_detail_s: float = 4.0
    fetch_delay_jitter_s: float = 2.0
    max_consecutive_failures: int = 3
    dry_run: bool = True
    kill_switch: bool = False

    # Paths
    plz_csv_path: str = "data/plz_de.csv"
    cache_dir: str = "data/cache"

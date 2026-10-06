"""App configuration, loaded from .env via pydantic-settings."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowlist: list[int] = []
    telegram_notify_chat_id: int | None = None  # where Gmail-driven finds go; default: first allowlisted user

    # LLM (OpenAI-compatible endpoint; defaults target Gemini's compat layer)
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.5-flash-lite"
    llm_response_format: Literal["json_schema", "json_object"] = "json_schema"
    llm_min_interval_s: float = 4.0  # pause between evaluations, keeps free-tier RPM limits happy

    # Search / evaluation
    home_plz: str = "10115"
    default_radius_km: int | None = 20  # when the buyer gives no distance; blank = any distance
    recommendation_threshold: int = 70

    # Fetching / anti-blocking
    max_requests_per_run: int = 40  # one results page per city + detail pages
    fetch_delay_floor_search_s: float = 2.0
    fetch_delay_floor_detail_s: float = 4.0
    fetch_delay_jitter_s: float = 2.0
    max_consecutive_failures: int = 3
    search_cache_ttl_s: float = 900.0
    dry_run: bool = True
    kill_switch: bool = False

    # Gmail (Kleinanzeigen saved-search alert emails)
    gmail_enabled: bool = False
    gmail_credentials_path: str = "data/gmail_credentials.json"
    gmail_token_path: str = "data/gmail_token.json"
    gmail_query: str = "from:kleinanzeigen.de newer_than:7d"
    gmail_poll_interval_s: int = 600

    # Paths
    db_path: str = "data/offer_finder.db"
    plz_csv_path: str = "data/plz_de.csv"
    cache_dir: str = "data/cache"

    # Logging: terminal + a rotating file. DEBUG adds every fetch and LLM call.
    log_level: str = "INFO"
    log_path: str = "data/logs/offer_finder.log"

    @field_validator("telegram_notify_chat_id", "default_radius_km", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        return None if value == "" else value

    @property
    def notify_chat_id(self) -> int | None:
        if self.telegram_notify_chat_id is not None:
            return self.telegram_notify_chat_id
        return self.telegram_allowlist[0] if self.telegram_allowlist else None

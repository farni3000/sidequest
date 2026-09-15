"""Typed application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "sidequest"
    debug: bool = False

    database_url: str = "sqlite:///./data/sidequest.db"

    # AI / deepseek (OpenAI-compatible)
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Routing providers
    ors_api_key: str | None = None
    ors_base_url: str = "https://api.openrouteservice.org"
    graphhopper_api_key: str | None = None
    graphhopper_base_url: str = "https://graphhopper.com/api/1"
    brouter_url: str = "http://localhost:17777"

    # Geocoding / POI discovery
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_fallback_url: str | None = "https://overpass.kumi.systems/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    geocoding_user_agent: str = "sidequest/0.1 (+https://github.com/anomalyco/sidequest)"

    # Behaviour
    corridor_min_m: int = 500
    corridor_max_m: int = 8000
    max_pois: int = 80
    max_highlights: int = 6
    http_timeout_s: float = 30.0

    @property
    def has_deepseek(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def has_ors(self) -> bool:
        return bool(self.ors_api_key)

    @property
    def has_graphhopper(self) -> bool:
        return bool(self.graphhopper_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

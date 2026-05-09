from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    redbark_api_key: str = Field(default="", description="Redbark API bearer token")
    redbark_base_url: str = Field(default="https://api.redbark.co")
    database_url: str = Field(default="sqlite:///./redbark.db")
    timezone: str = Field(default="Australia/Melbourne")
    poll_interval_minutes: int = Field(default=15, ge=1)
    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

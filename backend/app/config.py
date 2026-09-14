"""Environment-driven settings. Reads `.env` from the repo root (or CWD)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str
    openai_model: str = "gpt-5.5"
    openai_reasoning_effort: str = "medium"  # for gpt-5.x / o-series: minimal | low | medium | high

    gridcoach_api_key: str = "change-me"
    public_base_url: str = "http://localhost:8000"

    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_verify_token: str = "gridcoach-verify"

    google_service_account_file: str = str(REPO_ROOT / "service_account.json")
    # Hosted deploys inject secrets as env vars, not files: the JSON key itself, raw or base64.
    google_service_account_json: str = ""
    database_path: str = str(REPO_ROOT / "backend" / "gridcoach.db")
    default_lookback_days: int = 90

    @field_validator("google_service_account_file", "database_path")
    @classmethod
    def _resolve_relative_to_repo(cls, v: str) -> str:
        """Relative paths in .env are relative to the repo root, not the CWD."""
        path = Path(v).expanduser()
        return str(path if path.is_absolute() else (REPO_ROOT / path).resolve())

    @property
    def strava_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/strava/callback"

    @property
    def strava_webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/webhook/strava"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

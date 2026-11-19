"""Environment-driven application settings.

Secrets are typed as ``SecretStr`` so they are never printed in logs or reprs,
and they are never serialized to the frontend or passed to an LLM.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: AppEnv = AppEnv.DEVELOPMENT
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    config_dir: Path = Path("config")

    # Google OAuth (web application client)
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_oauth_client_secrets_file: Path | None = None
    google_redirect_uri: str = "http://localhost:8000/auth/callback"
    google_oidc_redirect_uri: str = "http://localhost:8000/auth/oidc/callback"

    # Gemini / Vertex AI
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model_name: str = ""
    gemini_use_vertex: bool = False
    google_cloud_project: str = ""
    google_cloud_location: str = "europe-west1"
    prompts_dir: Path = Path("backend/app/prompts")

    # YouTube
    youtube_channel_id: str = ""
    youtube_publishing_enabled: bool = False

    # Security
    encryption_key: SecretStr = SecretStr("")
    websub_secret: SecretStr = SecretStr("")
    dashboard_allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("google_oauth_client_secrets_file", mode="before")
    @classmethod
    def _empty_path_is_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @field_validator("dashboard_allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return [e.strip().lower() for e in value.split(",") if e.strip()]
        return value

    @model_validator(mode="after")
    def _enforce_safety_invariants(self) -> Settings:
        # AGENTS.md: never publish real YouTube content during automated tests.
        if self.app_env is AppEnv.TEST and self.youtube_publishing_enabled:
            raise ValueError("YOUTUBE_PUBLISHING_ENABLED must be false when APP_ENV=test")
        return self

    def production_readiness_errors(self) -> list[str]:
        """Return missing settings that would make a production deploy unsafe."""
        errors: list[str] = []
        if self.app_env is not AppEnv.PRODUCTION:
            return errors
        if self.database_url.startswith("sqlite"):
            errors.append("DATABASE_URL must point to PostgreSQL in production")
        if not self.encryption_key.get_secret_value() and not self.google_cloud_project:
            errors.append("Token encryption requires ENCRYPTION_KEY or Cloud KMS project")
        if not self.websub_secret.get_secret_value():
            errors.append("WEBSUB_SECRET is required to authenticate upload notifications")
        if not self.dashboard_allowed_emails:
            errors.append("DASHBOARD_ALLOWED_EMAILS must list at least one account")
        return errors


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

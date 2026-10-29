from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.settings import AppEnv, Settings


def test_defaults_are_safe() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_env is AppEnv.TEST  # forced by conftest
    assert s.youtube_publishing_enabled is False
    assert s.database_url.startswith("sqlite+aiosqlite")


def test_publishing_cannot_be_enabled_in_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_PUBLISHING_ENABLED", "true")
    with pytest.raises(ValidationError, match="must be false"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_secrets_are_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setenv("GEMINI_API_KEY", "another-secret")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dumped = repr(s) + str(s.model_dump())
    assert "super-secret-value" not in dumped
    assert "another-secret" not in dumped


def test_dashboard_emails_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_ALLOWED_EMAILS", " A@example.com, b@example.com ,")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.dashboard_allowed_emails == ["a@example.com", "b@example.com"]


def test_production_readiness_reports_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    errors = Settings(_env_file=None).production_readiness_errors()  # type: ignore[call-arg]
    joined = " ".join(errors)
    for expected in ("PostgreSQL", "WEBSUB_SECRET", "YOUTUBE_CHANNEL_ID", "DASHBOARD"):
        assert expected in joined

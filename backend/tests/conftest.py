"""Shared test configuration.

Forces a safe environment for every test: APP_ENV=test, publishing disabled,
no real credentials, and outbound network socket connections blocked so no test
can accidentally fall through to the real YouTube API.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def authorize_test_app(app: Any, *, role: str = "owner") -> None:
    """Inject a fixed authenticated test principal for legacy workflow integration tests."""
    from app.api.dependencies import CurrentIdentity, require_current_principal
    from app.core.tenancy import (
        LEGACY_ORGANIZATION_ID,
        bind_organization_id,
        reset_organization_id,
    )

    async def test_principal() -> AsyncIterator[CurrentIdentity]:
        token = bind_organization_id(LEGACY_ORGANIZATION_ID)
        try:
            yield CurrentIdentity(
                user_id="test-user",
                email="test-owner@example.test",
                session_id=1,
                csrf_token="test-csrf-token",
                organization_id=LEGACY_ORGANIZATION_ID,
                organization_name="Legacy Workspace",
                role=role,
            )
        finally:
            reset_organization_id(token)

    app.dependency_overrides[require_current_principal] = test_principal

_SENSITIVE_ENV = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_SECRETS_FILE",
    "GEMINI_API_KEY",
    "ENCRYPTION_KEY",
    "WEBSUB_SECRET",
    "YOUTUBE_CHANNEL_ID",
)

_ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def safe_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("YOUTUBE_PUBLISHING_ENABLED", "false")
    for name in _SENSITIVE_ENV:
        monkeypatch.delenv(name, raising=False)

    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _guarded_create_connection(
        address: tuple[str, int], *args: Any, **kwargs: Any
    ) -> socket.socket:
        host = address[0] if isinstance(address, tuple) and address else ""
        if host not in _ALLOWED_LOCAL_HOSTS:
            raise RuntimeError(
                f"Outbound network connection to {address!r} is forbidden during automated tests."
            )
        return real_create_connection(address, *args, **kwargs)

    def _guarded_getaddrinfo(host: str | bytes | None, *args: Any, **kwargs: Any) -> list[Any]:
        host_str = host.decode() if isinstance(host, bytes) else (host or "")
        if host_str not in _ALLOWED_LOCAL_HOSTS:
            raise RuntimeError(
                f"Outbound network connection to {host_str!r} is forbidden during automated tests."
            )
        return list(real_getaddrinfo(host, *args, **kwargs))

    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)

    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def config_dir() -> Path:
    return CONFIG_DIR

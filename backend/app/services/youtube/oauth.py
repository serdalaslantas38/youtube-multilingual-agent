"""Google OAuth 2.0 Web Flow with CSRF state and PKCE for YouTube channel authorization.

Security guarantees:
- Least-privilege scope: `https://www.googleapis.com/auth/youtube.force-ssl`
  (required for `captions.download`, `captions.insert`, and `videos.update`).
- PKCE (RFC 7636 `S256` code challenge) + cryptographic `state` nonce with bounded TTL.
- Uses `access_type="offline"` and `prompt="consent"` so a refresh token is
  issued reliably in development/testing mode.
- Client secrets and tokens are never logged or returned in API responses.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.settings import Settings

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
DEFAULT_YOUTUBE_SCOPES: tuple[str, ...] = (YOUTUBE_FORCE_SSL_SCOPE,)


class OAuthConfigurationError(Exception):
    """Raised when Google OAuth client credentials are missing or invalid."""


class OAuthStateError(Exception):
    """Raised when the OAuth `state` parameter is missing, unknown, or expired."""


class OAuthTokenExchangeError(Exception):
    """Raised when Google's token endpoint rejects an authorization code or refresh token."""


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    """Resolved Google OAuth 2.0 Web client configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    auth_uri: str = GOOGLE_AUTH_ENDPOINT
    token_uri: str = GOOGLE_TOKEN_ENDPOINT


def resolve_oauth_client_config(settings: Settings) -> OAuthClientConfig:
    """Load OAuth client ID and secret from `Settings` or `GOOGLE_OAUTH_CLIENT_SECRETS_FILE`.

    Never prints or exposes the secret in error messages.
    """
    client_id = settings.google_client_id.strip()
    client_secret = settings.google_client_secret.get_secret_value().strip()
    auth_uri = GOOGLE_AUTH_ENDPOINT
    token_uri = GOOGLE_TOKEN_ENDPOINT

    if (not client_id or not client_secret) and settings.google_oauth_client_secrets_file:
        path = settings.google_oauth_client_secrets_file.expanduser()
        if not path.is_file():
            raise OAuthConfigurationError(
                f"Configured GOOGLE_OAUTH_CLIENT_SECRETS_FILE does not exist: {path.name}"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        block = raw.get("web") or raw.get("installed")
        if not isinstance(block, dict):
            raise OAuthConfigurationError(
                "OAuth client secrets JSON must contain a 'web' or 'installed' object."
            )
        client_id = str(block.get("client_id") or "").strip()
        client_secret = str(block.get("client_secret") or "").strip()
        auth_uri = str(block.get("auth_uri") or auth_uri)
        token_uri = str(block.get("token_uri") or token_uri)

    if not client_id or not client_secret:
        raise OAuthConfigurationError(
            "Google OAuth credentials are not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env (or GOOGLE_OAUTH_CLIENT_SECRETS_FILE)."
        )

    return OAuthClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=settings.google_redirect_uri,
        auth_uri=auth_uri,
        token_uri=token_uri,
    )


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a `(code_verifier, code_challenge)` pair for PKCE `S256`."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass(frozen=True, slots=True)
class PendingOAuthSession:
    state: str
    code_verifier: str
    redirect_uri: str
    scopes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime


@dataclass
class OAuthStateStore:
    """Bounded in-memory store of pending OAuth authorization handshakes."""

    ttl_seconds: int = 600
    _pending: dict[str, PendingOAuthSession] = field(default_factory=dict)

    def create_session(
        self,
        *,
        redirect_uri: str,
        scopes: tuple[str, ...] = DEFAULT_YOUTUBE_SCOPES,
        now: datetime | None = None,
    ) -> tuple[PendingOAuthSession, str]:
        """Create and store a pending OAuth session, returning `(session, code_challenge)`."""
        current = now or datetime.now(UTC)
        self._prune_expired(current)
        state = secrets.token_urlsafe(32)
        verifier, challenge = generate_pkce_pair()
        session = PendingOAuthSession(
            state=state,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            scopes=scopes,
            created_at=current,
            expires_at=current + timedelta(seconds=self.ttl_seconds),
        )
        self._pending[state] = session
        return session, challenge

    def consume_session(
        self,
        state: str,
        *,
        now: datetime | None = None,
    ) -> PendingOAuthSession:
        """Validate and single-use consume a pending OAuth session by `state`."""
        current = now or datetime.now(UTC)
        self._prune_expired(current)
        if not state or state not in self._pending:
            raise OAuthStateError("Invalid or expired OAuth state parameter.")
        session = self._pending.pop(state)
        if current > session.expires_at:
            raise OAuthStateError("OAuth authorization session has expired.")
        return session

    def _prune_expired(self, current: datetime) -> None:
        expired_keys = [k for k, v in self._pending.items() if current > v.expires_at]
        for k in expired_keys:
            self._pending.pop(k, None)


def build_authorization_url(
    client_cfg: OAuthClientConfig,
    *,
    state: str,
    code_challenge: str,
    scopes: tuple[str, ...] = DEFAULT_YOUTUBE_SCOPES,
) -> str:
    """Build the Google OAuth 2.0 authorization URL.

    Note on Brand Accounts:
    Using `prompt="consent"` (without `select_account` or `include_granted_scopes`)
    ensures Google's YouTube OAuth flow presents the secondary Brand Account /
    YouTube channel selector screen after the user chooses their Google email.
    """
    query = {
        "client_id": client_cfg.client_id,
        "redirect_uri": client_cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{client_cfg.auth_uri}?{urlencode(query)}"


@dataclass(frozen=True, slots=True)
class OAuthTokenBundle:
    """Sanitized representation of exchanged OAuth tokens (never serialized to responses)."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    scopes: tuple[str, ...]
    token_type: str = "Bearer"  # noqa: S105


class OAuthTokenClient(Protocol):
    """Protocol for exchanging authorization codes and refreshing access tokens."""

    async def exchange_code(
        self,
        client_cfg: OAuthClientConfig,
        *,
        code: str,
        code_verifier: str,
    ) -> OAuthTokenBundle:
        """Exchange an authorization code + PKCE verifier for tokens."""

    async def refresh_access_token(
        self,
        client_cfg: OAuthClientConfig,
        *,
        refresh_token: str,
    ) -> OAuthTokenBundle:
        """Mint a fresh short-lived access token from a stored refresh token."""


class GoogleHttpOAuthTokenClient:
    """Async httpx implementation of `OAuthTokenClient` against Google's token endpoint."""

    async def exchange_code(
        self,
        client_cfg: OAuthClientConfig,
        *,
        code: str,
        code_verifier: str,
    ) -> OAuthTokenBundle:
        form = {
            "client_id": client_cfg.client_id,
            "client_secret": client_cfg.client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": client_cfg.redirect_uri,
        }
        data = await self._post_token_form(client_cfg.token_uri, form)
        return _parse_token_bundle(data)

    async def refresh_access_token(
        self,
        client_cfg: OAuthClientConfig,
        *,
        refresh_token: str,
    ) -> OAuthTokenBundle:
        form = {
            "client_id": client_cfg.client_id,
            "client_secret": client_cfg.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        data = await self._post_token_form(client_cfg.token_uri, form)
        return _parse_token_bundle(data, fallback_refresh_token=refresh_token)

    async def _post_token_form(self, token_uri: str, form: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(token_uri, data=form)
        if resp.status_code != 200:
            err_code = "token_exchange_failed"
            try:
                body = resp.json()
                if isinstance(body, dict) and isinstance(body.get("error"), str):
                    err_code = body["error"]
            except ValueError:
                pass
            raise OAuthTokenExchangeError(
                f"Google OAuth token endpoint returned HTTP {resp.status_code} ({err_code})."
            )
        parsed = resp.json()
        if not isinstance(parsed, dict) or not isinstance(parsed.get("access_token"), str):
            raise OAuthTokenExchangeError(
                "Google OAuth token response did not include a valid access_token."
            )
        return parsed


def _parse_token_bundle(
    payload: dict[str, Any],
    *,
    fallback_refresh_token: str | None = None,
) -> OAuthTokenBundle:
    raw_scope = str(payload.get("scope") or "")
    scopes = tuple(s for s in raw_scope.split() if s) or DEFAULT_YOUTUBE_SCOPES
    refresh = payload.get("refresh_token")
    resolved_refresh = (
        str(refresh).strip()
        if isinstance(refresh, str) and refresh.strip()
        else fallback_refresh_token
    )
    return OAuthTokenBundle(
        access_token=str(payload["access_token"]),
        refresh_token=resolved_refresh,
        expires_in=int(payload.get("expires_in") or 3600),
        scopes=scopes,
        token_type=str(payload.get("token_type") or "Bearer"),
    )

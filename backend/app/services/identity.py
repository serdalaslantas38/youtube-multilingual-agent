"""Google OIDC authorization code exchange and signed identity-token verification."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.services.youtube.oauth import OAuthClientConfig, OAuthTokenExchangeError

OIDC_SCOPES = ("openid", "email", "profile")
OIDC_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class OidcVerifier(Protocol):
    async def verify(self, token: str, *, audience: str) -> Mapping[str, Any]:
        """Verify signature, issuer, expiry, and audience, returning validated claims."""


class GoogleOidcVerifier:
    async def verify(self, token: str, *, audience: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._verify_sync, token, audience)

    @staticmethod
    def _verify_sync(token: str, audience: str) -> Mapping[str, Any]:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience)


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_oidc_authorization_url(
    client: OAuthClientConfig,
    *,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    params = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(OIDC_SCOPES),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{client.auth_uri}?{urlencode(params)}"


async def exchange_oidc_code(
    client: OAuthClientConfig,
    *,
    code: str,
    code_verifier: str,
) -> str:
    form = {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": client.redirect_uri,
    }
    async with httpx.AsyncClient(timeout=20.0) as http:
        response = await http.post(client.token_uri, data=form)
    if response.status_code != 200:
        raise OAuthTokenExchangeError(
            f"OIDC token endpoint returned HTTP {response.status_code}."
        )
    payload = response.json()
    token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise OAuthTokenExchangeError("OIDC token response did not include an ID token.")
    return token

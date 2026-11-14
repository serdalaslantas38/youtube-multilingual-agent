"""Verified SaaS OIDC login, server sessions, CSRF, and organization selection."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import (
    SESSION_COOKIE,
    AuthenticatedIdentity,
    _load_identity,
)
from app.core.settings import AppEnv, Settings
from app.core.tenancy import LEGACY_ORGANIZATION_ID
from app.models.entities import (
    OidcLoginAttempt,
    Organization,
    OrganizationMember,
    User,
    UserSession,
)
from app.services.identity import (
    OIDC_ISSUERS,
    GoogleOidcVerifier,
    OidcVerifier,
    build_oidc_authorization_url,
    exchange_oidc_code,
    new_pkce_pair,
)
from app.services.youtube.oauth import (
    OAuthClientConfig,
    OAuthConfigurationError,
    resolve_oauth_client_config,
)

router = APIRouter(tags=["identity"])
OIDC_STATE_COOKIE = "yma_oidc_state"
SESSION_TTL = timedelta(hours=8)
OIDC_ATTEMPT_TTL = timedelta(minutes=10)


class OrganizationMembershipItem(BaseModel):
    id: str
    name: str
    role: str


class AuthMeResponse(BaseModel):
    authenticated: bool
    user_id: str = ""
    email: str = ""
    organization_id: str | None = None
    organization_name: str | None = None
    role: str | None = None
    csrf_token: str | None = None
    organizations: list[OrganizationMembershipItem] = Field(default_factory=list)


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def _secure_cookie(settings: Settings) -> bool:
    return settings.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _client(request: Request) -> OAuthClientConfig:
    settings: Settings = request.app.state.settings
    try:
        config = resolve_oauth_client_config(settings)
    except OAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail="OIDC is not configured.") from exc
    return replace(config, redirect_uri=settings.google_oidc_redirect_uri)


def _verifier(request: Request) -> OidcVerifier:
    verifier: OidcVerifier | None = getattr(request.app.state, "oidc_verifier", None)
    if verifier is None:
        verifier = GoogleOidcVerifier()
        request.app.state.oidc_verifier = verifier
    return verifier


@router.get("/auth/oidc/start")
async def oidc_login_start(request: Request) -> RedirectResponse:
    """Start a Google OIDC flow with browser-bound state, nonce, and PKCE."""
    client = _client(request)
    now = datetime.now(UTC)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = new_pkce_pair()
    async with _factory(request)() as db:
        await db.execute(delete(OidcLoginAttempt).where(OidcLoginAttempt.expires_at < now))
        db.add(
            OidcLoginAttempt(
                state_hash=_hash(state),
                nonce=nonce,
                code_verifier=verifier,
                expires_at=now + OIDC_ATTEMPT_TTL,
                created_at=now,
            )
        )
        await db.commit()

    settings: Settings = request.app.state.settings
    response = RedirectResponse(
        build_oidc_authorization_url(
            client,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        ),
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        OIDC_STATE_COOKIE,
        state,
        httponly=True,
        secure=_secure_cookie(settings),
        samesite="lax",
        max_age=int(OIDC_ATTEMPT_TTL.total_seconds()),
        path="/",
    )
    return response


async def _consume_attempt(request: Request, raw_state: str) -> tuple[str, str]:
    cookie_state = request.cookies.get(OIDC_STATE_COOKIE, "")
    if not cookie_state or not hmac.compare_digest(cookie_state, raw_state):
        raise HTTPException(status_code=400, detail="OIDC state validation failed.")
    now = datetime.now(UTC)
    async with _factory(request)() as db:
        attempt = (
            await db.execute(
                select(OidcLoginAttempt)
                .where(
                    OidcLoginAttempt.state_hash == _hash(raw_state),
                    OidcLoginAttempt.consumed_at.is_(None),
                    OidcLoginAttempt.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise HTTPException(status_code=400, detail="OIDC state is expired or already used.")
        attempt.consumed_at = now
        nonce, verifier = attempt.nonce, attempt.code_verifier
        await db.commit()
        return nonce, verifier


async def _upsert_identity_and_session(
    request: Request,
    claims: dict[str, Any],
) -> tuple[str, str, str | None]:
    issuer = claims.get("iss")
    subject = claims.get("sub")
    email = claims.get("email")
    email_verified = claims.get("email_verified")
    if issuer not in OIDC_ISSUERS or not isinstance(subject, str) or not subject:
        raise HTTPException(status_code=401, detail="OIDC identity claims are invalid.")
    if not isinstance(email, str) or not email.strip() or email_verified is not True:
        raise HTTPException(status_code=403, detail="A verified email address is required.")
    normalized_email = email.strip().lower()
    now = datetime.now(UTC)
    settings: Settings = request.app.state.settings
    async with _factory(request)() as db:
        user = (
            await db.execute(
                select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
            )
        ).scalar_one_or_none()
        if user is None:
            allowlisted = {item.strip().lower() for item in settings.dashboard_allowed_emails}
            if normalized_email not in allowlisted:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "This account has no organization membership. "
                        "Contact an organization owner."
                    ),
                )
            user = User(
                oidc_issuer=issuer,
                oidc_subject=subject,
                email=normalized_email,
                email_verified=True,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(user)
            await db.flush()

        if not user.is_active:
            raise HTTPException(status_code=403, detail="This account is disabled.")
        user.email = normalized_email
        user.email_verified = True
        user.updated_at = now

        memberships = list(
            (
                await db.execute(
                    select(OrganizationMember, Organization)
                    .join(Organization, OrganizationMember.organization_id == Organization.id)
                    .where(
                        OrganizationMember.user_id == user.id,
                        OrganizationMember.is_active.is_(True),
                        Organization.is_active.is_(True),
                    )
                )
            ).all()
        )
        if not memberships:
            allowlisted = {item.strip().lower() for item in settings.dashboard_allowed_emails}
            if normalized_email not in allowlisted:
                raise HTTPException(
                    status_code=403,
                    detail="This account has no active organization membership.",
                )
            legacy_org = await db.get(Organization, LEGACY_ORGANIZATION_ID)
            if legacy_org is None or not legacy_org.is_active:
                raise HTTPException(status_code=503, detail="Legacy workspace is unavailable.")
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == legacy_org.id,
                        OrganizationMember.is_active.is_(True),
                    )
                )
            ).scalar_one()
            membership = OrganizationMember(
                organization_id=legacy_org.id,
                user_id=user.id,
                role="owner" if count == 0 else "admin",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(membership)
            await db.flush()
            memberships = [(membership, legacy_org)]

        active_org_id = memberships[0][0].organization_id if len(memberships) == 1 else None
        raw_session = secrets.token_urlsafe(32)
        csrf_token = secrets.token_hex(32)
        db.add(
            UserSession(
                user_id=user.id,
                token_hash=_hash(raw_session),
                csrf_token=csrf_token,
                active_organization_id=active_org_id,
                expires_at=now + SESSION_TTL,
                created_at=now,
                last_seen_at=now,
            )
        )
        await db.commit()
        return raw_session, csrf_token, active_org_id


@router.get("/auth/oidc/callback")
async def oidc_login_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error or not code or not state:
        raise HTTPException(
            status_code=400,
            detail="OIDC authorization was cancelled or incomplete.",
        )
    nonce, verifier = await _consume_attempt(request, state)
    config = _client(request)
    try:
        token = await exchange_oidc_code(config, code=code, code_verifier=verifier)
        claims = dict(await _verifier(request).verify(token, audience=config.client_id))
    except Exception as exc:
        # Do not echo token endpoint or verifier details into an HTTP response.
        raise HTTPException(status_code=401, detail="OIDC token verification failed.") from exc
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise HTTPException(status_code=401, detail="OIDC nonce validation failed.")

    raw_session, _csrf, _org_id = await _upsert_identity_and_session(request, claims)
    settings: Settings = request.app.state.settings
    result = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    result.set_cookie(
        SESSION_COOKIE,
        raw_session,
        httponly=True,
        secure=_secure_cookie(settings),
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )
    result.delete_cookie(OIDC_STATE_COOKIE, path="/")
    return result


@router.get("/api/auth/me", response_model=AuthMeResponse)
async def auth_me(request: Request) -> AuthMeResponse:
    try:
        identity = await _load_identity(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return AuthMeResponse(authenticated=False)
        raise
    async with _factory(request)() as db:
        rows = list(
            (
                await db.execute(
                    select(OrganizationMember, Organization)
                    .join(Organization, OrganizationMember.organization_id == Organization.id)
                    .where(
                        OrganizationMember.user_id == identity.user_id,
                        OrganizationMember.is_active.is_(True),
                        Organization.is_active.is_(True),
                    )
                    .order_by(Organization.name.asc())
                )
            ).all()
        )
    return AuthMeResponse(
        authenticated=True,
        user_id=identity.user_id,
        email=identity.email,
        organization_id=identity.organization_id,
        organization_name=identity.organization_name,
        role=identity.role,
        csrf_token=identity.csrf_token,
        organizations=[
            OrganizationMembershipItem(id=org.id, name=org.name, role=membership.role)
            for membership, org in rows
        ],
    )


@router.get("/api/organizations", response_model=list[OrganizationMembershipItem])
async def list_organizations(
    request: Request,
    identity: AuthenticatedIdentity,
) -> list[OrganizationMembershipItem]:
    async with _factory(request)() as db:
        rows = list(
            (
                await db.execute(
                    select(OrganizationMember, Organization)
                    .join(Organization, OrganizationMember.organization_id == Organization.id)
                    .where(
                        OrganizationMember.user_id == identity.user_id,
                        OrganizationMember.is_active.is_(True),
                        Organization.is_active.is_(True),
                    )
                    .order_by(Organization.name.asc())
                )
            ).all()
        )
    return [
        OrganizationMembershipItem(id=org.id, name=org.name, role=membership.role)
        for membership, org in rows
    ]


@router.post("/api/organizations/{organization_id}/activate", response_model=AuthMeResponse)
async def activate_organization(
    organization_id: str,
    request: Request,
    identity: AuthenticatedIdentity,
) -> AuthMeResponse:
    async with _factory(request)() as db:
        membership = (
            await db.execute(
                select(OrganizationMember).where(
                    OrganizationMember.user_id == identity.user_id,
                    OrganizationMember.organization_id == organization_id,
                    OrganizationMember.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        organization = await db.get(Organization, organization_id)
        if membership is None or organization is None or not organization.is_active:
            raise HTTPException(status_code=404, detail="Organization membership not found.")
        session = await db.get(UserSession, identity.session_id)
        if session is None or session.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Session is invalid.")
        session.active_organization_id = organization_id
        await db.commit()
    return await auth_me(request)


@router.post("/api/auth/logout")
async def logout(request: Request, identity: AuthenticatedIdentity) -> Response:
    async with _factory(request)() as db:
        session = await db.get(UserSession, identity.session_id)
        if session is not None and session.revoked_at is None:
            session.revoked_at = datetime.now(UTC)
            await db.commit()
    settings: Settings = request.app.state.settings
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_secure_cookie(settings),
        httponly=True,
        samesite="lax",
    )
    return response

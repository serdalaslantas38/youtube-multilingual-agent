"""Authenticated organization context and role dependencies for customer API routes."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import bind_organization_id, reset_organization_id
from app.models.entities import Organization, OrganizationMember, User, UserSession

SESSION_COOKIE = "yma_session"


@dataclass(frozen=True, slots=True)
class CurrentIdentity:
    user_id: str
    email: str
    session_id: int
    csrf_token: str
    organization_id: str | None
    organization_name: str | None
    role: str | None


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _csrf_ok(request: Request, session: UserSession) -> bool:
    supplied = request.headers.get("X-CSRF-Token", "")
    return bool(supplied) and hmac.compare_digest(supplied, session.csrf_token)


async def _load_identity(request: Request) -> CurrentIdentity:
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    factory = _session_factory(request)
    now = datetime.now(UTC)
    async with factory() as db:
        session = (
            await db.execute(
                select(UserSession).where(
                    UserSession.token_hash == token_hash,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is invalid or expired."
            )
        user = await db.get(User, session.user_id)
        if user is None or not user.is_active or not user.email_verified or not user.email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User is inactive.",
            )
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_ok(
            request, session
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="CSRF validation failed.",
            )

        org_id = session.active_organization_id
        org_name: str | None = None
        role: str | None = None
        if org_id:
            membership = (
                await db.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.user_id == user.id,
                        OrganizationMember.organization_id == org_id,
                        OrganizationMember.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            organization = await db.get(Organization, org_id)
            if membership is None or organization is None or not organization.is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Active workspace membership is required.",
                )
            org_name = organization.name
            role = membership.role
        session.last_seen_at = now
        await db.commit()

        identity = CurrentIdentity(
            user_id=user.id,
            email=user.email,
            session_id=session.id,
            csrf_token=session.csrf_token,
            organization_id=org_id,
            organization_name=org_name,
            role=role,
        )

    return identity


async def require_authenticated_identity(request: Request) -> CurrentIdentity:
    """Authenticate a user session; an organization may still need to be selected."""
    return await _load_identity(request)


async def require_current_principal(request: Request) -> AsyncIterator[CurrentIdentity]:
    """Require an active organization, set its request-local scope, and restore it afterward."""
    identity = await _load_identity(request)
    if not identity.organization_id or not identity.role:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Select an organization before using this endpoint.",
        )
    token = bind_organization_id(identity.organization_id)
    try:
        yield identity
    finally:
        reset_organization_id(token)


def require_roles(*roles: str) -> Callable[..., AsyncIterator[CurrentIdentity]]:
    """Build a dependency that allows only the requested organization roles."""

    async def dependency(
        identity: Annotated[CurrentIdentity, Depends(require_current_principal)],
    ) -> AsyncIterator[CurrentIdentity]:
        if identity.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your organization role cannot perform this action.",
            )
        yield identity

    return dependency


AuthenticatedIdentity = Annotated[CurrentIdentity, Depends(require_authenticated_identity)]
ViewerAccess = require_roles("owner", "admin", "editor", "viewer")
EditorAccess = require_roles("owner", "admin", "editor")
AdminAccess = require_roles("owner", "admin")
OwnerAccess = require_roles("owner")

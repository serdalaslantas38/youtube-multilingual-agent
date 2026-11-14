"""FastAPI router for Stage 6 dashboard auth, hash-bound approvals, editing, and policy settings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.app_config import AppConfig
from app.api.dependencies import AdminAccess, CurrentIdentity, EditorAccess, ViewerAccess
from app.core.settings import Settings
from app.core.tenancy import current_organization_id
from app.models.entities import AuditEvent
from app.schemas.approvals import (
    ApproveAssetRequest,
    AssetDetailResponse,
    AuditEventItem,
    EditAssetRequest,
    PolicySettingsResponse,
    RejectAssetRequest,
    UpdateGlobalPolicyRequest,
    UpdateLanguagePolicyRequest,
)
from app.services.approvals.service import (
    approve_language_asset,
    build_asset_detail_response,
    build_policy_settings_response,
    edit_language_asset,
    reject_language_asset,
    resolve_effective_app_config,
    update_global_policy_in_db,
    update_language_policy_in_db,
)

router = APIRouter(tags=["approvals-and-settings"])


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


@router.get("/api/assets/{asset_id}", response_model=AssetDetailResponse)
async def get_asset_detail_endpoint(
    request: Request,
    asset_id: int,
    _identity: Annotated[CurrentIdentity, Depends(ViewerAccess)],
) -> AssetDetailResponse:
    """Return full side-by-side detail for a localized metadata or caption asset."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        try:
            return await build_asset_detail_response(
                session,
                asset_id=asset_id,
                app_config=effective_cfg,
                settings=settings,
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc


@router.put("/api/assets/{asset_id}", response_model=AssetDetailResponse)
async def edit_asset_endpoint(
    request: Request,
    asset_id: int,
    body: EditAssetRequest,
    identity: Annotated[CurrentIdentity, Depends(EditorAccess)],
) -> AssetDetailResponse:
    """Edit a localized asset, re-run deterministic validation, and invalidate prior approvals."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        try:
            detail = await edit_language_asset(
                session,
                asset_id=asset_id,
                actor=identity.email,
                app_config=effective_cfg,
                settings=settings,
                translated_title=body.translated_title,
                translated_description=body.translated_description,
                caption_content=body.caption_content,
            )
            await session.commit()
            return detail
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc


@router.post("/api/assets/{asset_id}/approve", response_model=AssetDetailResponse)
async def approve_asset_endpoint(
    request: Request,
    asset_id: int,
    body: ApproveAssetRequest,
    identity: Annotated[CurrentIdentity, Depends(EditorAccess)],
) -> AssetDetailResponse:
    """Approve an asset bound to its exact SHA-256 content hash and audit the approval."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        try:
            detail = await approve_language_asset(
                session,
                asset_id=asset_id,
                actor=identity.email,
                expected_content_hash=body.expected_content_hash,
                notes=body.notes,
                override_review_issues=body.override_review_issues,
                app_config=effective_cfg,
                settings=settings,
            )
            await session.commit()
            return detail
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc


@router.post("/api/assets/{asset_id}/reject", response_model=AssetDetailResponse)
async def reject_asset_endpoint(
    request: Request,
    asset_id: int,
    body: RejectAssetRequest,
    identity: Annotated[CurrentIdentity, Depends(EditorAccess)],
) -> AssetDetailResponse:
    """Reject a localized asset, invalidate active approvals, and mark it `needs_attention`."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        try:
            detail = await reject_language_asset(
                session,
                asset_id=asset_id,
                actor=identity.email,
                reason=body.reason,
                app_config=effective_cfg,
                settings=settings,
            )
            await session.commit()
            return detail
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/api/settings/policy", response_model=PolicySettingsResponse)
async def get_policy_settings_endpoint(
    request: Request,
    _identity: Annotated[CurrentIdentity, Depends(ViewerAccess)],
) -> PolicySettingsResponse:
    """Return effective per-language and global publishing policy settings."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        return build_policy_settings_response(effective_cfg, settings)


@router.put(
    "/api/settings/policy/languages/{language_code}",
    response_model=PolicySettingsResponse,
)
async def update_language_policy_endpoint(
    request: Request,
    language_code: str,
    body: UpdateLanguagePolicyRequest,
    identity: Annotated[CurrentIdentity, Depends(AdminAccess)],
) -> PolicySettingsResponse:
    """Update a language's publishing mode or enabled status in the DB with audit logging."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        try:
            updated_cfg = await update_language_policy_in_db(
                session,
                seed_config=seed_cfg,
                actor=identity.email,
                language_code=language_code,
                mode=body.mode,
                enabled=body.enabled,
                name=body.name,
                native_name=body.native_name,
                caption_tlang=body.caption_tlang,
                rtl=body.rtl,
            )
            await session.commit()
            return build_policy_settings_response(updated_cfg, settings)
        except (ValueError, ValidationError) as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc


@router.put("/api/settings/policy/global", response_model=PolicySettingsResponse)
async def update_global_policy_endpoint(
    request: Request,
    body: Annotated[UpdateGlobalPolicyRequest, Body()],
    identity: Annotated[CurrentIdentity, Depends(AdminAccess)],
) -> PolicySettingsResponse:
    """Update the global `auto_publish_enabled` switch in the DB with audit logging."""
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        updated_cfg = await update_global_policy_in_db(
            session,
            seed_config=seed_cfg,
            actor=identity.email,
            auto_publish_enabled=body.auto_publish_enabled,
            reason=body.reason,
        )
        await session.commit()
        return build_policy_settings_response(updated_cfg, settings)


@router.get("/api/audit-events", response_model=list[AuditEventItem])
async def list_audit_events_endpoint(
    request: Request,
    _identity: Annotated[CurrentIdentity, Depends(ViewerAccess)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEventItem]:
    """Return recent immutable audit log events ordered newest first."""
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.organization_id == current_organization_id())
            .order_by(AuditEvent.id.desc())
            .limit(limit)
        )
        events = list((await session.execute(stmt)).scalars().all())
        return [
            AuditEventItem(
                id=ev.id,
                actor=ev.actor,
                action=ev.action,
                target_type=ev.target_type,
                target_id=ev.target_id,
                details=ev.details,
                created_at=ev.created_at,
            )
            for ev in events
        ]

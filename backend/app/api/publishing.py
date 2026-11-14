"""FastAPI router for Stage 7 controlled publishing, reconciliation, and publish history.

All YouTube write operations are strictly delegated to `app/services/publishing/`
and gated by the deterministic policy engine (`evaluate_publish_decision`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import AdminAccess, CurrentIdentity, ViewerAccess
from app.core.app_config import AppConfig
from app.core.settings import Settings
from app.schemas.publishing import (
    BatchPublishVideoRequest,
    BatchPublishVideoResponse,
    PublishAssetRequest,
    PublishAssetResponse,
    ReconcileAssetRequest,
    VideoPublishHistoryItem,
)
from app.services.approvals.service import resolve_effective_app_config
from app.services.publishing.service import (
    PublishPolicyBlockedError,
    list_video_publish_history,
    publish_language_asset,
    publish_ready_assets_for_video,
    reconcile_uncertain_asset,
)
from app.services.youtube.discovery import (
    ChannelNotConnectedError,
    mint_access_token_for_channel,
)
from app.services.youtube.oauth import OAuthTokenClient
from app.services.youtube.read_service import HttpYouTubeDataClient, YouTubeReadOnlyDataClient

router = APIRouter(tags=["publishing"])


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _get_token_client(request: Request) -> OAuthTokenClient:
    client: OAuthTokenClient = request.app.state.oauth_token_client
    return client


async def _resolve_yt_client(
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> YouTubeReadOnlyDataClient:
    injected: YouTubeReadOnlyDataClient | None = getattr(
        request.app.state, "youtube_data_client", None
    )
    if injected is not None:
        return injected

    factory: Callable[[str], YouTubeReadOnlyDataClient] | None = getattr(
        request.app.state, "youtube_client_factory", None
    )
    try:
        _, access_token = await mint_access_token_for_channel(
            session,
            settings=settings,
            token_client=_get_token_client(request),
        )
    except ChannelNotConnectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    if factory is not None:
        return factory(access_token)
    return HttpYouTubeDataClient(access_token)


@router.post("/api/assets/{asset_id}/publish", response_model=PublishAssetResponse)
async def publish_asset_endpoint(
    request: Request,
    asset_id: int,
    identity: Annotated[CurrentIdentity, Depends(AdminAccess)],
    body: Annotated[PublishAssetRequest | None, Body()] = None,
) -> PublishAssetResponse:
    """Publish a single approved/eligible asset to YouTube with post-write verification."""
    req = body or PublishAssetRequest()
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)

    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        yt_client = await _resolve_yt_client(request, session, settings)
        try:
            result = await publish_language_asset(
                session,
                asset_id=asset_id,
                actor=identity.email,
                app_config=effective_cfg,
                settings=settings,
                yt_client=yt_client,
                expected_content_hash=req.expected_content_hash,
                is_draft=req.is_draft,
                allow_set_default_language=req.allow_set_default_language,
                explicit_overwrite_authorized=req.explicit_overwrite_authorized,
            )
            await session.commit()
            return result
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except PublishPolicyBlockedError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": exc.decision.reason_code,
                    "effective_mode": exc.decision.effective_mode.value,
                    "message": exc.decision.message,
                },
            ) from exc
        except ValueError as exc:
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc


@router.post("/api/assets/{asset_id}/reconcile", response_model=PublishAssetResponse)
async def reconcile_asset_endpoint(
    request: Request,
    asset_id: int,
    identity: Annotated[CurrentIdentity, Depends(AdminAccess)],
    body: Annotated[ReconcileAssetRequest | None, Body()] = None,
) -> PublishAssetResponse:
    """Reconcile an uncertain publish outcome by reading live YouTube state before any retry."""
    req = body or ReconcileAssetRequest()
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)

    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        yt_client = await _resolve_yt_client(request, session, settings)
        try:
            result = await reconcile_uncertain_asset(
                session,
                asset_id=asset_id,
                actor=identity.email,
                app_config=effective_cfg,
                settings=settings,
                yt_client=yt_client,
                notes=req.notes,
            )
            await session.commit()
            return result
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc


@router.post(
    "/api/videos/{youtube_video_id}/publish-ready",
    response_model=BatchPublishVideoResponse,
)
async def publish_ready_assets_for_video_endpoint(
    request: Request,
    youtube_video_id: str,
    identity: Annotated[CurrentIdentity, Depends(AdminAccess)],
    body: Annotated[BatchPublishVideoRequest | None, Body()] = None,
) -> BatchPublishVideoResponse:
    """Publish all `ready_to_publish` assets for a video."""
    req = body or BatchPublishVideoRequest()
    settings: Settings = request.app.state.settings
    seed_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)

    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, seed_cfg)
        yt_client = await _resolve_yt_client(request, session, settings)
        try:
            result = await publish_ready_assets_for_video(
                session,
                youtube_video_id=youtube_video_id,
                actor=identity.email,
                app_config=effective_cfg,
                settings=settings,
                yt_client=yt_client,
                languages=req.languages,
                include_metadata=req.include_metadata,
                include_captions=req.include_captions,
                is_draft=req.is_draft,
                allow_set_default_language=req.allow_set_default_language,
                force=req.force,
            )
            await session.commit()
            return result
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc


@router.get(
    "/api/videos/{youtube_video_id}/publish-attempts",
    response_model=list[VideoPublishHistoryItem],
)
async def list_video_publish_attempts_endpoint(
    request: Request,
    youtube_video_id: str,
    _identity: Annotated[CurrentIdentity, Depends(ViewerAccess)],
) -> list[VideoPublishHistoryItem]:
    """Return chronological publish attempt history for a video."""
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        return await list_video_publish_history(session, youtube_video_id=youtube_video_id)

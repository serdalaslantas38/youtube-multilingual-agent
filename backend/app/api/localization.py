"""FastAPI endpoints for Stage 5 localization, fallback translation, validation, and review.

All endpoints in this router are read-only with respect to YouTube writes:
they localize metadata and captions into the local database (`LanguageAsset`)
and never call `videos.update` or `captions.insert`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.app_config import AppConfig, aload_app_config
from app.core.settings import Settings
from app.core.tenancy import LEGACY_ORGANIZATION_ID
from app.models.entities import LanguageAsset, Video
from app.orchestration.localization import _asset_to_summary, localize_video_assets
from app.schemas.localization import (
    LanguageAssetSummary,
    LocalizeVideoRequest,
    VideoLocalizationResponse,
)
from app.services.approvals.service import resolve_effective_app_config
from app.services.translation.providers import LLMStructuredClient
from app.services.validation.validators import (
    SourceCaptionCandidate,
    evaluate_source_caption_tracks,
)
from app.services.youtube.captions_client import YouTubeApiError, YouTubeCaptionClient
from app.services.youtube.discovery import (
    ChannelNotConnectedError,
    inspect_and_persist_video_captions,
    mint_access_token_for_channel,
)
from app.services.youtube.oauth import OAuthTokenClient
from app.services.youtube.read_service import HttpYouTubeDataClient, YouTubeReadOnlyDataClient

router = APIRouter(tags=["localization"])


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _get_token_client(request: Request) -> OAuthTokenClient:
    client: OAuthTokenClient = request.app.state.oauth_token_client
    return client


def _build_yt_client(request: Request, access_token: str) -> YouTubeReadOnlyDataClient:
    factory: Callable[[str], YouTubeReadOnlyDataClient] | None = getattr(
        request.app.state, "youtube_client_factory", None
    )
    if factory is not None:
        return factory(access_token)
    return HttpYouTubeDataClient(access_token)


@router.get("/api/videos/{youtube_video_id}/assets", response_model=VideoLocalizationResponse)
async def get_video_localized_assets(
    request: Request,
    youtube_video_id: str,
) -> VideoLocalizationResponse:
    """Return all localized metadata and caption assets for a discovered video."""
    app_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        stmt = (
            select(Video)
            .options(selectinload(Video.source_captions), selectinload(Video.language_assets))
            .where(
                Video.organization_id == LEGACY_ORGANIZATION_ID,
                Video.youtube_video_id == youtube_video_id,
            )
        )
        video = (await session.execute(stmt)).scalar_one_or_none()
        if video is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Video {youtube_video_id!r} not found in database.",
            )

        candidates = [
            SourceCaptionCandidate(
                youtube_caption_id=t.youtube_caption_id,
                language=t.language,
                track_kind=t.track_kind,
                processing_status=t.processing_status,
                is_draft=t.is_draft,
                failure_reason=t.failure_reason,
            )
            for t in video.source_captions
        ]
        diagnosis = evaluate_source_caption_tracks(
            candidates, source_language=app_cfg.languages.source_language.code
        )
        assets_stmt = (
            select(LanguageAsset)
            .where(LanguageAsset.video_id == video.id)
            .order_by(LanguageAsset.language.asc(), LanguageAsset.kind.asc())
        )
        assets = list((await session.execute(assets_stmt)).scalars().all())
        summaries: list[LanguageAssetSummary] = [
            _asset_to_summary(a, video.youtube_video_id) for a in assets
        ]

        return VideoLocalizationResponse(
            youtube_video_id=video.youtube_video_id,
            source_title=video.source_title,
            workflow_state=video.workflow_state,
            source_caption_ready=diagnosis.ready,
            selected_source_caption_id=(
                diagnosis.selected_track.youtube_caption_id if diagnosis.selected_track else None
            ),
            selected_source_track_kind=(
                diagnosis.selected_track.track_kind.value if diagnosis.selected_track else None
            ),
            assets=summaries,
            quota_units_used_this_run=0,
        )


@router.post("/api/videos/{youtube_video_id}/localize", response_model=VideoLocalizationResponse)
async def localize_video_endpoint(
    request: Request,
    youtube_video_id: str,
    body: Annotated[LocalizeVideoRequest | None, Body()] = None,
) -> VideoLocalizationResponse:
    """Run Stage 5 localization, fallback caption translation, validation, and review."""
    req = body or LocalizeVideoRequest()
    settings: Settings = request.app.state.settings
    llm_client: LLMStructuredClient | None = getattr(request.app.state, "llm_client", None)
    if llm_client is None:
        request.app.state.app_config = await aload_app_config(settings.config_dir)
    app_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    injected_caption_client: YouTubeCaptionClient | None = getattr(
        request.app.state, "caption_client", None
    )

    async with session_factory() as session:
        effective_cfg = await resolve_effective_app_config(session, app_cfg)
        caption_client: YouTubeCaptionClient | None = injected_caption_client
        if req.include_captions and caption_client is None:
            try:
                _, access_token = await mint_access_token_for_channel(
                    session,
                    settings=settings,
                    token_client=_get_token_client(request),
                )
                yt_ro_client = _build_yt_client(request, access_token)
                caption_client = yt_ro_client
                vid_stmt = (
                    select(Video)
                    .options(selectinload(Video.source_captions))
                    .where(
                        Video.organization_id == LEGACY_ORGANIZATION_ID,
                        Video.youtube_video_id == youtube_video_id,
                    )
                )
                vid_row = (await session.execute(vid_stmt)).scalar_one_or_none()
                if vid_row is not None and not vid_row.source_captions:
                    try:
                        await inspect_and_persist_video_captions(
                            session,
                            yt_client=yt_ro_client,
                            youtube_video_id=youtube_video_id,
                            source_language=effective_cfg.languages.source_language.code,
                        )
                        await session.flush()
                        # Force the localizer's selectinload to pick up newly inspected tracks.
                        session.expire(vid_row, ["source_captions"])
                    except YouTubeApiError:
                        pass
            except ChannelNotConnectedError:
                caption_client = None

        try:
            response = await localize_video_assets(
                session,
                youtube_video_id=youtube_video_id,
                app_config=effective_cfg,
                settings=settings,
                llm_client=llm_client,
                caption_client=caption_client,
                target_language_codes=req.languages,
                include_metadata=req.include_metadata,
                include_captions=req.include_captions,
                force_regenerate=req.force_regenerate,
            )
            await session.commit()
            return response
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

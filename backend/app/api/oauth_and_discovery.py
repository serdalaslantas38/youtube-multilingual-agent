"""FastAPI routers for Google OAuth 2.0, channel identity, video discovery, and caption inspection.

All endpoints are strictly read-only with respect to YouTube resources.
OAuth tokens and secrets are encrypted at rest and never returned in responses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.app_config import AppConfig
from app.core.crypto import TokenEncryptionError
from app.core.settings import Settings
from app.models.entities import SourceCaptionTrack, Video
from app.models.enums import ChannelConnectionStatus
from app.schemas.youtube import (
    ChannelStatusResponse,
    DiscoveredVideoSummary,
    OAuthStartResponse,
    SourceCaptionTrackSummary,
    VideoCaptionInspectionResponse,
    VideoDiscoveryResponse,
)
from app.services.validation.validators import (
    SourceCaptionCandidate,
    evaluate_source_caption_tracks,
)
from app.services.youtube.captions_client import YouTubeApiError
from app.services.youtube.discovery import (
    ChannelBindingMismatchError,
    ChannelNotConnectedError,
    discover_channel_videos,
    get_active_channel_connection,
    import_video_by_url_or_id,
    inspect_and_persist_video_captions,
    mint_access_token_for_channel,
    run_and_persist_caption_probe,
    upsert_authorized_channel_connection,
)
from app.services.youtube.oauth import (
    DEFAULT_YOUTUBE_SCOPES,
    OAuthConfigurationError,
    OAuthStateError,
    OAuthStateStore,
    OAuthTokenClient,
    OAuthTokenExchangeError,
    build_authorization_url,
    resolve_oauth_client_config,
)
from app.services.youtube.read_service import (
    HttpYouTubeDataClient,
    YouTubeReadOnlyDataClient,
    get_today_quota_used,
)

router = APIRouter(tags=["oauth-and-discovery"])


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _get_oauth_state_store(request: Request) -> OAuthStateStore:
    store: OAuthStateStore = request.app.state.oauth_state_store
    return store


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


def _to_video_summary(video: Video) -> DiscoveredVideoSummary:
    return DiscoveredVideoSummary(
        youtube_video_id=video.youtube_video_id,
        youtube_channel_id=video.youtube_channel_id,
        source_language=video.source_language,
        default_audio_language=video.default_audio_language,
        source_title=video.source_title,
        source_description=video.source_description,
        privacy_status=video.privacy_status.value,
        scheduled_publish_at=video.scheduled_publish_at,
        published_at=video.published_at,
        workflow_state=video.workflow_state.value,
        existing_localization_languages=sorted(video.existing_localizations.keys()),
        discovered_at=video.discovered_at,
        updated_at=video.updated_at,
    )


def _to_track_summary(track: SourceCaptionTrack) -> SourceCaptionTrackSummary:
    return SourceCaptionTrackSummary(
        youtube_caption_id=track.youtube_caption_id,
        language=track.language,
        name=track.name,
        track_kind=track.track_kind.value,
        processing_status=track.processing_status.value,
        failure_reason=track.failure_reason,
        is_draft=track.is_draft,
        is_auto_synced=track.is_auto_synced,
        is_downloadable=track.is_downloadable,
        download_error=track.download_error,
        cue_count=track.cue_count,
        tlang_diagnostics=track.tlang_diagnostics,
        inspected_at=track.inspected_at,
    )


@router.get("/auth/google/start", response_model=OAuthStartResponse)
async def oauth_google_start(
    request: Request,
    redirect: bool = Query(
        default=False,
        description="If true, issue an HTTP 302 redirect directly to Google's consent screen.",
    ),
) -> Any:
    """Start the Google OAuth 2.0 Web flow with CSRF `state` and PKCE (`S256`)."""
    settings: Settings = request.app.state.settings
    try:
        client_cfg = resolve_oauth_client_config(settings)
    except OAuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    state_store = _get_oauth_state_store(request)
    pending, code_challenge = state_store.create_session(
        redirect_uri=client_cfg.redirect_uri,
        scopes=DEFAULT_YOUTUBE_SCOPES,
    )
    auth_url = build_authorization_url(
        client_cfg,
        state=pending.state,
        code_challenge=code_challenge,
        scopes=pending.scopes,
    )
    if redirect:
        return RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)

    return OAuthStartResponse(
        authorization_url=auth_url,
        state=pending.state,
        scopes=list(pending.scopes),
        redirect_uri=client_cfg.redirect_uri,
    )


@router.get("/auth/callback", response_model=ChannelStatusResponse)
async def oauth_google_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Any:
    """Handle Google's OAuth 2.0 callback, verify channel identity, and store encrypted token."""
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google OAuth authorization was denied or failed: {error}",
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OAuth 'code' or 'state' query parameter.",
        )

    settings: Settings = request.app.state.settings
    state_store = _get_oauth_state_store(request)
    try:
        pending = state_store.consume_session(state)
    except OAuthStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        client_cfg = resolve_oauth_client_config(settings)
        token_client = _get_token_client(request)
        bundle = await token_client.exchange_code(
            client_cfg,
            code=code,
            code_verifier=pending.code_verifier,
        )
    except (OAuthConfigurationError, OAuthTokenExchangeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    if not bundle.refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Google OAuth did not return a refresh_token. Re-run /auth/google/start "
                "with prompt=consent."
            ),
        )

    yt_client = _build_yt_client(request, bundle.access_token)
    try:
        identity = await yt_client.get_authenticated_channel()
    except YouTubeApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch authenticated YouTube channel: {exc}",
        ) from exc

    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        try:
            conn = await upsert_authorized_channel_connection(
                session,
                settings=settings,
                identity=identity,
                refresh_token=bundle.refresh_token,
                scopes=bundle.scopes,
            )
            quota_today = await get_today_quota_used(session)
            await session.commit()
        except ChannelBindingMismatchError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except TokenEncryptionError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc

        accept_header = request.headers.get("accept", "")
        if "text/html" in accept_header:
            return RedirectResponse(
                url="http://localhost:5173/?oauth=reconnected",
                status_code=status.HTTP_302_FOUND,
            )

        return ChannelStatusResponse(
            connected=conn.status is ChannelConnectionStatus.CONNECTED,
            configured_channel_id=settings.youtube_channel_id or None,
            youtube_channel_id=conn.youtube_channel_id,
            channel_title=conn.channel_title,
            custom_url=conn.custom_url,
            uploads_playlist_id=conn.uploads_playlist_id,
            status=conn.status.value,
            scopes=list(conn.scopes),
            token_warning=conn.token_WARNING,
            connected_at=conn.connected_at,
            last_verified_at=conn.last_verified_at,
            today_quota_units_used=quota_today,
        )


@router.get("/api/channel/status", response_model=ChannelStatusResponse)
async def get_channel_status(request: Request) -> ChannelStatusResponse:
    """Return the connected YouTube channel identity and daily quota usage (no tokens exposed)."""
    settings: Settings = request.app.state.settings
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        conn = await get_active_channel_connection(
            session, expected_channel_id=settings.youtube_channel_id
        )
        quota_today = await get_today_quota_used(session)
        if conn is None:
            return ChannelStatusResponse(
                connected=False,
                configured_channel_id=settings.youtube_channel_id or None,
                today_quota_units_used=quota_today,
            )
        return ChannelStatusResponse(
            connected=conn.status is ChannelConnectionStatus.CONNECTED,
            configured_channel_id=settings.youtube_channel_id or None,
            youtube_channel_id=conn.youtube_channel_id,
            channel_title=conn.channel_title,
            custom_url=conn.custom_url,
            uploads_playlist_id=conn.uploads_playlist_id,
            status=conn.status.value,
            scopes=list(conn.scopes),
            token_warning=conn.token_WARNING,
            connected_at=conn.connected_at,
            last_verified_at=conn.last_verified_at,
            today_quota_units_used=quota_today,
        )


class ImportVideoRequest(BaseModel):
    video_url_or_id: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="YouTube video URL (watch, shorts, youtu.be, studio) or 11-char video ID.",
    )


@router.post("/api/videos/discover", response_model=VideoDiscoveryResponse)
async def discover_videos_endpoint(
    request: Request,
    max_results: int = Query(default=50, ge=1, le=500),
) -> VideoDiscoveryResponse:
    """Discover recent videos from the connected channel's uploads playlist (read-only)."""
    settings: Settings = request.app.state.settings
    app_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    token_client = _get_token_client(request)

    async with session_factory() as session:
        try:
            conn, access_token = await mint_access_token_for_channel(
                session, settings=settings, token_client=token_client
            )
        except ChannelNotConnectedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (OAuthTokenExchangeError, TokenEncryptionError) as exc:
            await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        if not conn.uploads_playlist_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connected channel does not have an uploads_playlist_id recorded.",
            )

        yt_client = _build_yt_client(request, access_token)
        try:
            videos = await discover_channel_videos(
                session,
                yt_client=yt_client,
                uploads_playlist_id=conn.uploads_playlist_id,
                source_language=app_cfg.languages.source_language.code,
                max_results=max_results,
            )
            quota_today = await get_today_quota_used(session)
            await session.commit()
        except YouTubeApiError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        return VideoDiscoveryResponse(
            channel_id=conn.youtube_channel_id,
            uploads_playlist_id=conn.uploads_playlist_id,
            discovered_count=len(videos),
            videos=[_to_video_summary(v) for v in videos],
            today_quota_units_used=quota_today,
        )


@router.post("/api/videos/import", response_model=DiscoveredVideoSummary)
async def import_video_endpoint(
    body: ImportVideoRequest,
    request: Request,
) -> DiscoveredVideoSummary:
    """Import any older video from the connected channel by its YouTube URL or 11-char ID."""
    settings: Settings = request.app.state.settings
    app_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    token_client = _get_token_client(request)

    async with session_factory() as session:
        try:
            conn, access_token = await mint_access_token_for_channel(
                session, settings=settings, token_client=token_client
            )
        except ChannelNotConnectedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (OAuthTokenExchangeError, TokenEncryptionError) as exc:
            await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        yt_client = _build_yt_client(request, access_token)
        try:
            video = await import_video_by_url_or_id(
                session,
                yt_client=yt_client,
                url_or_id=body.video_url_or_id,
                expected_channel_id=conn.youtube_channel_id,
                source_language=app_cfg.languages.source_language.code,
            )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ChannelBindingMismatchError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except YouTubeApiError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        return _to_video_summary(video)


@router.get("/api/videos", response_model=list[DiscoveredVideoSummary])
async def list_stored_videos(request: Request) -> list[DiscoveredVideoSummary]:
    """List videos already discovered and stored in the database."""
    session_factory = _get_session_factory(request)
    async with session_factory() as session:
        stmt = select(Video).order_by(Video.discovered_at.desc())
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_video_summary(v) for v in rows]


@router.post(
    "/api/videos/{youtube_video_id}/captions/inspect",
    response_model=VideoCaptionInspectionResponse,
)
async def inspect_video_captions_endpoint(
    youtube_video_id: str,
    request: Request,
    probe_translations: bool = Query(
        default=False,
        description=(
            "If true, also run the Stage 3b read-only captions.download + tlang probe "
            "(costs 200 units per downloaded track)."
        ),
    ),
    languages: str = Query(
        default="",
        description="Optional comma-separated target languages when probe_translations=true.",
    ),
) -> VideoCaptionInspectionResponse:
    """Inspect a video's caption tracks on YouTube (read-only) and persist diagnostics."""
    settings: Settings = request.app.state.settings
    app_cfg: AppConfig = request.app.state.app_config
    session_factory = _get_session_factory(request)
    token_client = _get_token_client(request)
    source_lang = app_cfg.languages.source_language.code

    async with session_factory() as session:
        try:
            _conn, access_token = await mint_access_token_for_channel(
                session, settings=settings, token_client=token_client
            )
        except ChannelNotConnectedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (OAuthTokenExchangeError, TokenEncryptionError) as exc:
            await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        yt_client = _build_yt_client(request, access_token)
        try:
            video, tracks = await inspect_and_persist_video_captions(
                session,
                yt_client=yt_client,
                youtube_video_id=youtube_video_id,
                source_language=source_lang,
            )
            if probe_translations:
                target_codes = (
                    [c.strip() for c in languages.split(",") if c.strip()]
                    if languages.strip()
                    else None
                )
                await run_and_persist_caption_probe(
                    session,
                    yt_client=yt_client,
                    youtube_video_id=youtube_video_id,
                    languages_cfg=app_cfg.languages,
                    target_language_codes=target_codes,
                )
            quota_today = await get_today_quota_used(session)
            await session.commit()
        except LookupError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except YouTubeApiError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        candidates = [
            SourceCaptionCandidate(
                youtube_caption_id=t.youtube_caption_id,
                language=t.language,
                track_kind=t.track_kind,
                processing_status=t.processing_status,
                is_draft=t.is_draft,
                failure_reason=t.failure_reason,
            )
            for t in tracks
        ]
        diagnosis = evaluate_source_caption_tracks(candidates, source_language=source_lang)

        return VideoCaptionInspectionResponse(
            youtube_video_id=video.youtube_video_id,
            workflow_state=video.workflow_state.value,
            source_ready=diagnosis.ready,
            retry_later=diagnosis.retry_later,
            diagnosis_reason_code=diagnosis.reason_code,
            diagnosis_message=diagnosis.message,
            tracks=[_to_track_summary(t) for t in tracks],
            today_quota_units_used=quota_today,
        )

"""Channel connection persistence, video discovery, and caption-track inspection service.

Enforces:
- Channel binding: if `YOUTUBE_CHANNEL_ID` is configured, connecting any other
  channel is rejected with `ChannelBindingMismatchError`.
- Encrypted refresh token storage (`encrypt_secret` / `decrypt_secret`).
- Idempotent upsert of discovered `Video` and `SourceCaptionTrack` records
  without modifying any YouTube resources.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_config import LanguagesConfig
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.settings import Settings
from app.models.entities import ChannelConnection, SourceCaptionTrack, Video
from app.models.enums import ChannelConnectionStatus, VideoWorkflowState
from app.services.validation.validators import (
    SourceCaptionCandidate,
    evaluate_source_caption_tracks,
)
from app.services.youtube.captions_client import (
    QUOTA_CAPTIONS_LIST,
    CaptionListParams,
    YouTubeCaptionTrackResource,
)
from app.services.youtube.oauth import (
    OAuthTokenClient,
    resolve_oauth_client_config,
)
from app.services.youtube.read_service import (
    QUOTA_CHANNELS_LIST,
    QUOTA_PLAYLIST_ITEMS_LIST,
    QUOTA_VIDEOS_LIST,
    YouTubeChannelIdentity,
    YouTubeReadOnlyDataClient,
    YouTubeVideoResource,
    record_quota_usage,
)

if TYPE_CHECKING:
    from app.services.captions.diagnostics import CaptionTrackDiagnosticReport


class ChannelBindingMismatchError(PermissionError):
    """Raised when the OAuth-authenticated channel does not match `YOUTUBE_CHANNEL_ID`."""


class ChannelNotConnectedError(RuntimeError):
    """Raised when an operation requires an active `ChannelConnection` but none exists."""


async def upsert_authorized_channel_connection(
    session: AsyncSession,
    *,
    settings: Settings,
    identity: YouTubeChannelIdentity,
    refresh_token: str,
    scopes: tuple[str, ...],
    authorized_email: str | None = None,
    now: datetime | None = None,
) -> ChannelConnection:
    """Verify channel binding, encrypt the refresh token, and upsert `ChannelConnection`."""
    expected_channel = settings.youtube_channel_id.strip()
    if expected_channel and identity.channel_id != expected_channel:
        raise ChannelBindingMismatchError(
            f"Authenticated YouTube channel {identity.channel_id!r} ({identity.title!r}) "
            f"does not match configured YOUTUBE_CHANNEL_ID {expected_channel!r}. "
            "If your channel is managed via a Brand Account, select the Brand Account "
            "channel during Google OAuth consent."
        )

    encrypted_token = encrypt_secret(refresh_token, settings.encryption_key)
    current = now or datetime.now(UTC)

    stmt = select(ChannelConnection).where(
        ChannelConnection.youtube_channel_id == identity.channel_id
    )
    conn = (await session.execute(stmt)).scalar_one_or_none()
    warning = (
        "OAuth app in Testing mode expires refresh tokens after 7 days; "
        "re-authorize if status changes to token_expired."
    )
    if conn is None:
        conn = ChannelConnection(
            youtube_channel_id=identity.channel_id,
            channel_title=identity.title,
            custom_url=identity.custom_url,
            uploads_playlist_id=identity.uploads_playlist_id,
            authorized_google_email=authorized_email,
            encrypted_refresh_token=encrypted_token,
            scopes=list(scopes),
            status=ChannelConnectionStatus.CONNECTED,
            token_WARNING=warning,
            connected_at=current,
            last_verified_at=current,
            updated_at=current,
        )
        session.add(conn)
    else:
        conn.channel_title = identity.title
        conn.custom_url = identity.custom_url
        conn.uploads_playlist_id = identity.uploads_playlist_id
        if authorized_email:
            conn.authorized_google_email = authorized_email
        conn.encrypted_refresh_token = encrypted_token
        conn.scopes = list(scopes)
        conn.status = ChannelConnectionStatus.CONNECTED
        conn.token_WARNING = warning
        conn.last_verified_at = current
        conn.updated_at = current

    await record_quota_usage(
        session,
        api_method="channels.list",
        units=QUOTA_CHANNELS_LIST,
        now=current,
    )
    await session.flush()
    return conn


async def get_active_channel_connection(
    session: AsyncSession,
    *,
    expected_channel_id: str = "",
) -> ChannelConnection | None:
    """Return the stored `ChannelConnection` (filtered by `expected_channel_id` if given)."""
    stmt = select(ChannelConnection).order_by(ChannelConnection.updated_at.desc())
    if expected_channel_id.strip():
        stmt = stmt.where(ChannelConnection.youtube_channel_id == expected_channel_id.strip())
    return (await session.execute(stmt)).scalars().first()


async def mint_access_token_for_channel(
    session: AsyncSession,
    *,
    settings: Settings,
    token_client: OAuthTokenClient,
) -> tuple[ChannelConnection, str]:
    """Decrypt the stored refresh token and mint a fresh OAuth access token."""
    conn = await get_active_channel_connection(
        session, expected_channel_id=settings.youtube_channel_id
    )
    if conn is None:
        raise ChannelNotConnectedError(
            "No YouTube channel is connected yet. Visit /auth/google/start to authorize."
        )
    refresh_token = decrypt_secret(conn.encrypted_refresh_token, settings.encryption_key)
    oauth_cfg = resolve_oauth_client_config(settings)
    try:
        bundle = await token_client.refresh_access_token(oauth_cfg, refresh_token=refresh_token)
    except Exception as exc:
        conn.status = ChannelConnectionStatus.TOKEN_EXPIRED
        conn.token_WARNING = f"Refresh token failed: {exc}"
        conn.updated_at = datetime.now(UTC)
        await session.flush()
        raise

    conn.status = ChannelConnectionStatus.CONNECTED
    conn.last_verified_at = datetime.now(UTC)
    conn.updated_at = conn.last_verified_at
    await session.flush()
    return conn, bundle.access_token


async def upsert_video_from_youtube_resource(
    session: AsyncSession,
    resource: YouTubeVideoResource,
    *,
    source_language: str = "tr",
    now: datetime | None = None,
) -> Video:
    """Idempotently create or update a `Video` row from a `YouTubeVideoResource`."""
    current = now or datetime.now(UTC)
    stmt = select(Video).where(Video.youtube_video_id == resource.video_id)
    video = (await session.execute(stmt)).scalar_one_or_none()
    content_hash = resource.compute_source_content_hash()

    if video is None:
        video = Video(
            youtube_video_id=resource.video_id,
            youtube_channel_id=resource.channel_id,
            source_language=source_language,
            default_audio_language=resource.default_audio_language,
            source_title=resource.title[:200],
            source_description=resource.description,
            source_category_id=resource.category_id,
            source_content_hash=content_hash,
            privacy_status=resource.privacy_status,
            scheduled_publish_at=resource.scheduled_publish_at,
            published_at=resource.published_at,
            workflow_state=VideoWorkflowState.DISCOVERED,
            existing_localizations=resource.localizations,
            discovered_at=current,
            updated_at=current,
        )
        session.add(video)
    else:
        video.source_title = resource.title[:200]
        video.source_description = resource.description
        video.source_category_id = resource.category_id
        video.default_audio_language = resource.default_audio_language
        video.source_content_hash = content_hash
        # Read-only snapshot of YouTube's privacy & schedule (never mutated on YouTube)
        video.privacy_status = resource.privacy_status
        video.scheduled_publish_at = resource.scheduled_publish_at
        video.published_at = resource.published_at
        video.existing_localizations = resource.localizations
        video.updated_at = current

    await session.flush()
    return video


async def discover_channel_videos(
    session: AsyncSession,
    *,
    yt_client: YouTubeReadOnlyDataClient,
    uploads_playlist_id: str,
    source_language: str = "tr",
    max_results: int = 20,
    now: datetime | None = None,
) -> list[Video]:
    """Discover recent videos from the channel's uploads playlist and persist snapshots."""
    current = now or datetime.now(UTC)
    video_ids = await yt_client.list_upload_video_ids(uploads_playlist_id, max_results=max_results)
    await record_quota_usage(
        session,
        api_method="playlistItems.list",
        units=QUOTA_PLAYLIST_ITEMS_LIST,
        now=current,
    )
    if not video_ids:
        return []

    resources = await yt_client.get_videos(video_ids)
    await record_quota_usage(
        session,
        api_method="videos.list",
        units=QUOTA_VIDEOS_LIST,
        now=current,
    )

    persisted: list[Video] = []
    for res in resources:
        video = await upsert_video_from_youtube_resource(
            session,
            res,
            source_language=source_language,
            now=current,
        )
        persisted.append(video)
    return persisted


async def inspect_and_persist_video_captions(
    session: AsyncSession,
    *,
    yt_client: YouTubeReadOnlyDataClient,
    youtube_video_id: str,
    source_language: str = "tr",
    now: datetime | None = None,
) -> tuple[Video, list[SourceCaptionTrack]]:
    """Fetch video metadata (if needed) and inspect its caption tracks via `captions.list`."""
    current = now or datetime.now(UTC)
    stmt = select(Video).where(Video.youtube_video_id == youtube_video_id)
    video = (await session.execute(stmt)).scalar_one_or_none()
    if video is None:
        resources = await yt_client.get_videos([youtube_video_id])
        await record_quota_usage(
            session,
            api_method="videos.list",
            units=QUOTA_VIDEOS_LIST,
            video_id=youtube_video_id,
            now=current,
        )
        if not resources:
            raise LookupError(f"Video {youtube_video_id!r} was not found on YouTube.")
        video = await upsert_video_from_youtube_resource(
            session, resources[0], source_language=source_language, now=current
        )

    remote_tracks: list[YouTubeCaptionTrackResource] = await yt_client.list_captions(
        CaptionListParams(video_id=youtube_video_id)
    )
    await record_quota_usage(
        session,
        api_method="captions.list",
        units=QUOTA_CAPTIONS_LIST,
        video_id=youtube_video_id,
        now=current,
    )

    persisted_tracks: list[SourceCaptionTrack] = []
    candidates: list[SourceCaptionCandidate] = []

    for item in remote_tracks:
        track_stmt = select(SourceCaptionTrack).where(
            SourceCaptionTrack.video_id == video.id,
            SourceCaptionTrack.youtube_caption_id == item.caption_id,
        )
        row = (await session.execute(track_stmt)).scalar_one_or_none()
        if row is None:
            row = SourceCaptionTrack(
                video_id=video.id,
                youtube_caption_id=item.caption_id,
                language=item.language,
                name=item.name,
                track_kind=item.track_kind,
                processing_status=item.status,
                failure_reason=item.failure_reason,
                is_draft=item.is_draft,
                is_auto_synced=item.is_auto_synced,
                last_updated_on_youtube=item.last_updated,
                inspected_at=current,
            )
            session.add(row)
        else:
            row.language = item.language
            row.name = item.name
            row.track_kind = item.track_kind
            row.processing_status = item.status
            row.failure_reason = item.failure_reason
            row.is_draft = item.is_draft
            row.is_auto_synced = item.is_auto_synced
            row.last_updated_on_youtube = item.last_updated
            row.inspected_at = current

        persisted_tracks.append(row)
        candidates.append(
            SourceCaptionCandidate(
                youtube_caption_id=item.caption_id,
                language=item.language,
                track_kind=item.track_kind,
                processing_status=item.status,
                is_draft=item.is_draft,
                failure_reason=item.failure_reason,
            )
        )

    diagnosis = evaluate_source_caption_tracks(candidates, source_language=source_language)
    if diagnosis.ready:
        video.workflow_state = VideoWorkflowState.SOURCE_READY
        video.last_error = None
    elif diagnosis.retry_later:
        video.workflow_state = VideoWorkflowState.AWAITING_SOURCE
        video.last_error = diagnosis.message
    else:
        video.workflow_state = VideoWorkflowState.NEEDS_ATTENTION
        video.last_error = diagnosis.message
    video.updated_at = current

    await session.flush()
    return video, persisted_tracks


async def run_and_persist_caption_probe(
    session: AsyncSession,
    *,
    yt_client: YouTubeReadOnlyDataClient,
    youtube_video_id: str,
    languages_cfg: LanguagesConfig,
    target_language_codes: list[str] | None = None,
    now: datetime | None = None,
) -> CaptionTrackDiagnosticReport:
    """Run the Stage 3b read-only caption + `tlang` probe and persist diagnostics on the track."""
    from app.services.captions.diagnostics import run_caption_diagnostics_probe

    current = now or datetime.now(UTC)
    report = await run_caption_diagnostics_probe(
        yt_client,
        video_id=youtube_video_id,
        languages_cfg=languages_cfg,
        target_language_codes=target_language_codes,
    )
    await record_quota_usage(
        session,
        api_method="captions.probe",
        units=report.quota_units_estimated,
        video_id=youtube_video_id,
        now=current,
    )

    if report.selected_caption_id:
        stmt = select(SourceCaptionTrack).where(
            SourceCaptionTrack.youtube_caption_id == report.selected_caption_id
        )
        track = (await session.execute(stmt)).scalars().first()
        if track is not None:
            track.is_downloadable = report.source_download_succeeded
            track.download_error = report.source_error_reason
            track.cue_count = report.source_cue_count
            track.tlang_diagnostics = {p.language: p.to_safe_dict() for p in report.target_probes}
            track.inspected_at = current
            await session.flush()

    return report

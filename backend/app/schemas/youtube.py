"""Pydantic schemas for OAuth status, channel identity, video discovery, and caption inspection.

Safety invariant:
- Never includes OAuth access tokens, refresh tokens, client secrets, or encryption keys.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OAuthStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_url: str
    state: str
    scopes: list[str]
    redirect_uri: str


class ChannelStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connected: bool
    configured_channel_id: str | None = None
    youtube_channel_id: str | None = None
    channel_title: str | None = None
    custom_url: str | None = None
    uploads_playlist_id: str | None = None
    status: str | None = None
    scopes: list[str] = Field(default_factory=list)
    token_warning: str | None = None
    connected_at: datetime | None = None
    last_verified_at: datetime | None = None
    today_quota_units_used: int = 0
    daily_quota_limit_estimate: int = 10000


class DiscoveredVideoSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    youtube_video_id: str
    youtube_channel_id: str
    source_language: str
    default_audio_language: str | None
    source_title: str
    source_description: str
    privacy_status: str
    scheduled_publish_at: datetime | None
    published_at: datetime | None
    workflow_state: str
    existing_localization_languages: list[str]
    discovered_at: datetime
    updated_at: datetime


class VideoDiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    uploads_playlist_id: str
    discovered_count: int
    videos: list[DiscoveredVideoSummary]
    today_quota_units_used: int


class SourceCaptionTrackSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    youtube_caption_id: str
    language: str
    name: str
    track_kind: str
    processing_status: str
    failure_reason: str | None
    is_draft: bool
    is_auto_synced: bool
    is_downloadable: bool | None
    download_error: str | None
    cue_count: int | None
    tlang_diagnostics: dict[str, Any]
    inspected_at: datetime


class VideoCaptionInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    youtube_video_id: str
    workflow_state: str
    source_ready: bool
    retry_later: bool
    diagnosis_reason_code: str
    diagnosis_message: str
    tracks: list[SourceCaptionTrackSummary]
    today_quota_units_used: int
